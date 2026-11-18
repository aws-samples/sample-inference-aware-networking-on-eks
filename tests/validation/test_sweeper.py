# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Local unit proof for the orphan sweeper (DESIGN.md section 3.4).

Runs against mocked DynamoDB + SQS (moto), so it needs no cluster or AWS credentials.
This is THE proof that the sweeper closes the LT-6 durability gap without opening a
double-run path for healthy jobs. GPU-free by design.

Assertions (matching the fix-pass Phase B spec):
  1. Recovers an orphan (processing, expired lease, stored payload, no message).
  2. Completes after recovery (a worker consumes the re-enqueued message, re-claims via
     the stale-claim steal path, runs, and marks complete; claim_count reflects the run).
  3. Idempotent against itself (two sweeps in a row enqueue only once; cooldown suppresses).
  4. Leaves healthy items alone (future-lease processing, and complete, untouched).
  5. Respects the grace period (an orphan stuck less than grace is not enqueued yet).
  6. Does not regress exactly-once (a duplicate of a HEALTHY job still runs zero extra
     times, and the sweeper never touches it).

Run:  ./.venv/bin/python tests/validation/test_sweeper.py
"""
import json
import os
import sys

os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")
os.environ.setdefault("GATEWAY_ENDPOINT", "http://gateway.inference.svc.cluster.local/v1")
os.environ.setdefault("DDB_TABLE", "inference-jobs")
os.environ.setdefault("CLAIM_TTL_SECONDS", "25")
os.environ.setdefault("RETENTION_SECONDS", "21600")
os.environ.setdefault("SWEEP_GRACE_SECONDS", "90")     # explicit grace for the test
os.environ.setdefault("SWEEP_COOLDOWN_SECONDS", "300")

import boto3
from moto import mock_aws

TABLE = os.environ["DDB_TABLE"]


def _seed(jobstore, key, client_id, payload, status, lease_delta, claimed_delta,
          claim_count=1, extra=None):
    """Put an item directly, bypassing try_claim, to set up a precise scenario."""
    now = jobstore.now_epoch()
    item = {
        "job_id": {"S": key},
        "status": {"S": status},
        "claim_token": {"S": "seed-token"},
        "lease_expires": {"N": str(now + lease_delta)},
        "claimed_at": {"N": str(now + claimed_delta)},
        "ttl": {"N": str(now + 21600)},
        "claim_count": {"N": str(claim_count)},
        "updated_at": {"S": jobstore.iso_now()},
    }
    if client_id is not None:
        item["client_id"] = {"S": client_id}
    if payload is not None:
        item["payload"] = {"S": json.dumps(payload, separators=(",", ":"))}
    if extra:
        item.update(extra)
    jobstore.ddb.put_item(TableName=TABLE, Item=item)


def _purge_table(jobstore):
    for it in jobstore.ddb.scan(TableName=TABLE).get("Items", []):
        jobstore.ddb.delete_item(TableName=TABLE, Key={"job_id": {"S": it["job_id"]["S"]}})


def _drain_queue(jobstore):
    """Receive and delete everything, returning the list of message bodies (dicts)."""
    bodies = []
    while True:
        resp = jobstore.sqs.receive_message(
            QueueUrl=jobstore.SQS_QUEUE_URL, MaxNumberOfMessages=10, WaitTimeSeconds=0
        )
        msgs = resp.get("Messages", [])
        if not msgs:
            break
        for m in msgs:
            bodies.append(json.loads(m["Body"]))
            jobstore.sqs.delete_message(
                QueueUrl=jobstore.SQS_QUEUE_URL, ReceiptHandle=m["ReceiptHandle"]
            )
    return bodies


def _receive_one(jobstore):
    resp = jobstore.sqs.receive_message(
        QueueUrl=jobstore.SQS_QUEUE_URL, MaxNumberOfMessages=1, WaitTimeSeconds=0
    )
    msgs = resp.get("Messages", [])
    return msgs[0] if msgs else None


@mock_aws
def main() -> int:
    # Resources first, then set SQS_QUEUE_URL, THEN import the modules (their
    # module-level clients and config bind at import, under the mock).
    boto3.client("dynamodb").create_table(
        TableName=TABLE,
        AttributeDefinitions=[{"AttributeName": "job_id", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "job_id", "KeyType": "HASH"}],
        BillingMode="PAY_PER_REQUEST",
    )
    qurl = boto3.client("sqs").create_queue(QueueName="inference")["QueueUrl"]
    os.environ["SQS_QUEUE_URL"] = qurl

    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "async-worker"))
    import importlib
    jobstore = importlib.import_module("jobstore")
    sweeper = importlib.import_module("sweeper")
    worker = importlib.import_module("worker")
    # Never make a real HTTP call in a unit test. This test is about the sweeper's orphan
    # reconciliation, not routing, so the stub ignores every routing coordinate it is handed.
    # `**_` rather than a named `path=`/`capability=`: this stub exists to avoid a real HTTP
    # call, not to police run_inference's signature. Re-encoding the signature in every stub
    # means every new call-site keyword breaks unrelated suites (it did: capability, 2026-09-21).
    # The wire contract has ONE enforcement point, test_promotion.test_the_capability_REACHES_THE_WIRE.
    worker.run_inference = lambda payload, correlation_id, **_: (
        {"choices": [{"message": {"content": "ok"}}]}, "10.0.0.1:8000")

    def get(key):
        return jobstore.get_item(key)

    # ---- 1. Recovers an orphan ----------------------------------------------
    _purge_table(jobstore); _drain_queue(jobstore)
    p1 = {"model": "m", "messages": [{"role": "user", "content": "one"}]}
    k1 = jobstore.job_key("c1", p1)
    _seed(jobstore, k1, "c1", p1, "processing", lease_delta=-100, claimed_delta=-200)
    n = sweeper.sweep_once()
    assert n["enqueued"] == 1, f"orphan should be enqueued exactly once, got {n}"
    bodies = _drain_queue(jobstore)
    assert len(bodies) == 1, f"exactly one message on the queue, got {len(bodies)}"
    assert bodies[0]["client_id"] == "c1" and bodies[0]["payload"] == p1, "payload preserved"
    assert get(k1).get("requeued_at"), "requeued_at must be stamped"
    print("OK  1 recovers an orphan: 1 message enqueued with the correct payload")

    # ---- 2. Completes after recovery ----------------------------------------
    _purge_table(jobstore); _drain_queue(jobstore)
    p2 = {"model": "m", "messages": [{"role": "user", "content": "two"}]}
    k2 = jobstore.job_key("c2", p2)
    _seed(jobstore, k2, "c2", p2, "processing", lease_delta=-100, claimed_delta=-200, claim_count=1)
    assert sweeper.sweep_once()["enqueued"] == 1
    msg = _receive_one(jobstore)
    assert msg is not None, "the re-enqueued message must be receivable"
    worker.handle(msg)  # worker re-claims via steal, runs, marks complete, deletes msg
    item = get(k2)
    assert item["status"]["S"] == "complete", "job must reach complete after recovery"
    assert item["claim_count"]["N"] == "2", "claim_count reflects the extra (re-claim) run"
    assert _receive_one(jobstore) is None, "worker must have deleted the message on complete"
    print("OK  2 completes after recovery: re-claim ran, marked complete, claim_count=2")

    # ---- 3. Idempotent against itself ---------------------------------------
    _purge_table(jobstore); _drain_queue(jobstore)
    p3 = {"model": "m", "messages": [{"role": "user", "content": "three"}]}
    k3 = jobstore.job_key("c3", p3)
    _seed(jobstore, k3, "c3", p3, "processing", lease_delta=-100, claimed_delta=-200)
    first = sweeper.sweep_once()
    second = sweeper.sweep_once()  # before any worker processes the message
    assert first["enqueued"] == 1, f"first sweep enqueues, got {first}"
    assert second["enqueued"] == 0 and second["cooldown"] == 1, f"second sweep suppressed, got {second}"
    assert len(_drain_queue(jobstore)) == 1, "only one message despite two sweeps"
    print("OK  3 idempotent: two sweeps in a row enqueue only once (cooldown)")

    # ---- 4. Leaves healthy items alone --------------------------------------
    _purge_table(jobstore); _drain_queue(jobstore)
    p4a = {"model": "m", "messages": [{"role": "user", "content": "live"}]}
    k4a = jobstore.job_key("c4a", p4a)
    _seed(jobstore, k4a, "c4a", p4a, "processing", lease_delta=+300, claimed_delta=-5)  # live worker
    p4b = {"model": "m", "messages": [{"role": "user", "content": "done"}]}
    k4b = jobstore.job_key("c4b", p4b)
    _seed(jobstore, k4b, "c4b", p4b, "complete", lease_delta=-1000, claimed_delta=-2000)
    n = sweeper.sweep_once()
    assert n["orphans"] == 0 and n["enqueued"] == 0, f"no healthy item is an orphan, got {n}"
    assert len(_drain_queue(jobstore)) == 0, "nothing enqueued for healthy items"
    assert not get(k4a).get("requeued_at"), "live-worker item must be untouched"
    assert not get(k4b).get("requeued_at"), "complete item must be untouched"
    print("OK  4 leaves healthy alone: future-lease and complete items untouched")

    # ---- 5. Respects the grace period ---------------------------------------
    _purge_table(jobstore); _drain_queue(jobstore)
    p5 = {"model": "m", "messages": [{"role": "user", "content": "recent"}]}
    k5 = jobstore.job_key("c5", p5)
    # lease expired, but claimed only 40s ago (< grace 90): SQS might still redeliver.
    _seed(jobstore, k5, "c5", p5, "processing", lease_delta=-10, claimed_delta=-40)
    n = sweeper.sweep_once()
    assert n["orphans"] == 0 and n["enqueued"] == 0, f"within grace, must not act, got {n}"
    assert len(_drain_queue(jobstore)) == 0, "nothing enqueued within grace"
    print("OK  5 respects grace: an orphan stuck less than grace is not enqueued yet")

    # ---- 6. Does not regress exactly-once -----------------------------------
    _purge_table(jobstore); _drain_queue(jobstore)
    p6 = {"model": "m", "messages": [{"role": "user", "content": "healthy-dup"}]}
    k6 = jobstore.job_key("c6", p6)
    _seed(jobstore, k6, "c6", p6, "processing", lease_delta=+300, claimed_delta=-5, claim_count=1)
    dup = jobstore.try_claim(k6, "c6", p6)  # a duplicate submission of a HEALTHY job
    assert dup is None, "duplicate of a live job must be refused (exactly-once holds)"
    n = sweeper.sweep_once()
    assert n["enqueued"] == 0, f"sweeper must not touch a healthy job, got {n}"
    assert get(k6)["claim_count"]["N"] == "1", "no extra run for a healthy duplicate"
    assert len(_drain_queue(jobstore)) == 0, "no message enqueued for a healthy job"
    print("OK  6 no exactly-once regression: healthy duplicate ran zero extra times")

    print("PASS: orphan sweeper is correct (recovers orphans, idempotent, healthy-safe)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
