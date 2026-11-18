# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Proves the worker's infrastructure-rejection backoff (ARCHITECTURE-REVIEW.md finding F1).

When the gateway rejects a queued job for an infrastructure reason (a 5xx from a batch Endpoint
Picker outage, or an unreachable gateway), the worker must NOT mark the job failed (which would
push a healthy job to the DLQ after maxReceiveCount infra-rejected redeliveries) and must back
off. When the gateway recovers, the job must complete exactly once (as an interrupted-then-
recovered job). A genuine 4xx bad request must still be marked failed.

Runs against mocked SQS + DynamoDB (moto), GPU-free.

Run:  ./.venv/bin/python tests/validation/test_infra_backoff.py
"""
import json
import os
import sys

os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")
os.environ.setdefault("GATEWAY_ENDPOINT", "http://gateway.inference.svc.cluster.local/v1")
os.environ.setdefault("DDB_TABLE", "inference-jobs")

import boto3
import requests
from moto import mock_aws

TABLE = os.environ["DDB_TABLE"]
# The fixture queue MUST use the PRODUCTION visibility (setup-async-infra.sh VIS_TIMEOUT=900),
# not the SQS default of 30s. This is the exact parameter that hid defect B.1: at 30s a 120s
# ChangeMessageVisibility looks like an extension, while against the real 900s queue it SHORTENS
# the message and burns it toward the DLQ. The test now runs at production visibility so the
# direction is the production direction.
PROD_VISIBILITY_TIMEOUT = "900"


def _enqueue_and_receive(jobstore, body: dict):
    jobstore.sqs.send_message(QueueUrl=jobstore.SQS_QUEUE_URL, MessageBody=json.dumps(body))
    r = jobstore.sqs.receive_message(QueueUrl=jobstore.SQS_QUEUE_URL, MaxNumberOfMessages=1, WaitTimeSeconds=0)
    return r["Messages"][0]


@mock_aws
def main() -> int:
    boto3.client("dynamodb").create_table(
        TableName=TABLE,
        AttributeDefinitions=[{"AttributeName": "job_id", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "job_id", "KeyType": "HASH"}],
        BillingMode="PAY_PER_REQUEST",
    )
    qurl = boto3.client("sqs").create_queue(
        QueueName="inference",
        Attributes={"VisibilityTimeout": PROD_VISIBILITY_TIMEOUT},
    )["QueueUrl"]
    os.environ["SQS_QUEUE_URL"] = qurl

    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "async-worker"))
    import importlib
    jobstore = importlib.import_module("jobstore")
    worker = importlib.import_module("worker")

    def get(key):
        return jobstore.get_item(key)

    payload = {"model": "m", "messages": [{"role": "user", "content": "infra probe"}]}
    body = {"client_id": "infra", "payload": payload}
    key = jobstore.job_key("infra", payload)

    # Confirm the fixture really is at production visibility, so the direction below is the
    # production direction and not the 30s-default artifact that hid B.1.
    vis = jobstore.sqs.get_queue_attributes(
        QueueUrl=qurl, AttributeNames=["VisibilityTimeout"])["Attributes"]["VisibilityTimeout"]
    assert vis == PROD_VISIBILITY_TIMEOUT, f"fixture must use production visibility, got {vis}"

    # ---- 1. infra rejection: NOT marked failed, claim released, message LEFT UNTOUCHED ----
    # Spy on ChangeMessageVisibility. The B.1 fix is that the worker no longer calls it: against a
    # 900s queue any value it could pass (the old 120s) would SHORTEN the message, not extend it,
    # cutting the DLQ horizon from ~60-75 min to ~8-10. The correct backoff is to touch nothing and
    # let the message keep the queue's full 900s. So the regression assertion is: zero calls.
    cmv_calls = {"n": 0}
    _orig_cmv = jobstore.sqs.change_message_visibility
    def _spy_cmv(**kw):
        cmv_calls["n"] += 1
        return _orig_cmv(**kw)
    jobstore.sqs.change_message_visibility = _spy_cmv

    # `**_` rather than a named `path=`/`capability=`: this stub exists to avoid a real HTTP
    # call, not to police run_inference's signature. Re-encoding the signature in every stub
    # means every new call-site keyword breaks unrelated suites (it did: capability, 2026-09-21).
    # The wire contract has ONE enforcement point, test_promotion.test_the_capability_REACHES_THE_WIRE.
    worker.run_inference = lambda payload, correlation_id, **_: (_ for _ in ()).throw(
        worker.InfraRejection("gateway 503"))
    msg = _enqueue_and_receive(jobstore, body)
    try:
        worker.handle(msg)
        raised = False
    except worker.BackoffSignal:
        raised = True
    jobstore.sqs.change_message_visibility = _orig_cmv  # restore for the recovery scaffolding below
    assert raised, "handle() must raise BackoffSignal on an infra rejection"
    assert cmv_calls["n"] == 0, (
        "worker must NOT call ChangeMessageVisibility on an infra rejection: against the 900s "
        "production queue that would shorten the message and burn it toward the DLQ (defect B.1)")
    item = get(key)
    assert item["status"]["S"] == "processing", "infra rejection must NOT mark the job failed"
    assert "error" not in item, "infra rejection must NOT write an error (that is for genuine failures)"
    assert item["lease_expires"]["N"] == "0", "claim must be released (lease expired) so the job is re-claimable"
    assert item["infra_rejections"]["N"] == "1", "the infra rejection must be recorded durably (B.2)"
    print("OK  1 infra rejection: job not failed, claim released, message untouched (no visibility change), infra_rejections=1")

    # ---- 2. recovery: a healthy delivery re-claims and completes exactly once ----
    worker.run_inference = lambda payload, correlation_id, **_: ({"choices": [{"message": {"content": "ok"}}]}, "10.0.0.2:8000")
    # make the backed-off message visible again for the test
    jobstore.sqs.change_message_visibility(QueueUrl=qurl, ReceiptHandle=msg["ReceiptHandle"], VisibilityTimeout=0)
    msg2 = jobstore.sqs.receive_message(QueueUrl=qurl, MaxNumberOfMessages=1, WaitTimeSeconds=0)["Messages"][0]
    worker.handle(msg2)
    item = get(key)
    assert item["status"]["S"] == "complete", "the job must complete on recovery"
    assert item["claim_count"]["N"] == "2", "one released infra attempt + one successful claim = claim_count 2"
    assert item["infra_rejections"]["N"] == "1", "exactly one of those two claims was infra-rejected"
    # B.2: real GPU runs = claim_count - infra_rejections = 2 - 1 = 1. claim_count alone over-counts.
    real_runs = int(item["claim_count"]["N"]) - int(item["infra_rejections"]["N"])
    assert real_runs == 1, f"exactly one real GPU run, got claim_count - infra_rejections = {real_runs}"
    print("OK  2 recovery: completed exactly once (claim_count=2, infra_rejections=1, real GPU runs=1)")

    # ---- 3. a genuine 4xx bad request IS marked failed (not treated as infra) ----
    p2 = {"model": "bad", "messages": [{"role": "user", "content": "genuinely bad"}]}
    b2 = {"client_id": "infra", "payload": p2}
    k2 = jobstore.job_key("infra", p2)

    def _bad(payload, correlation_id, **_):
        resp = requests.models.Response()
        resp.status_code = 400
        raise requests.exceptions.HTTPError("400 Bad Request", response=resp)

    worker.run_inference = _bad
    msg3 = _enqueue_and_receive(jobstore, b2)
    try:
        worker.handle(msg3)
    except worker.BackoffSignal:
        assert False, "a 4xx must NOT be treated as an infra rejection"
    except requests.exceptions.HTTPError:
        pass  # handle marks failed then re-raises the genuine failure
    item = get(k2)
    assert item["status"]["S"] == "failed", "a genuine 4xx must be marked failed"
    print("OK  3 genuine 4xx bad request is marked failed (distinct from an infra rejection)")

    print("PASS: worker distinguishes infra rejection (backoff, not failed) from job failure")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
