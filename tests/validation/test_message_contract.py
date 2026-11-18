#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
EVERY QUEUE PRODUCER'S ROUTING REACHES THE WORKER'S WIRE CALL. Offline (moto), no cluster.

WHY THIS EXISTS. test_promotion's "capability reaches the wire" test built its queue message BY
HAND and grepped the worker's source, so it proved a message shape nobody produced. The real
producers disagreed: submit_api wrote "capability", relay_server's promotion wrote
"capability_class", the worker read "capability", and the sweeper wrote neither (nor the route).
So a promoted interactive+premium job, the one case the capability axis exists for, would have been
served by the standard pool. Found 2026-09-22 in the review before the premium relay's wire proof.

SO EVERY LEG HERE RUNS THE REAL CODE and ends at the arguments the worker passes to its HTTP call
(run_inference), which is the last point before the headers are built:

  1. PROMOTION: relay_server._enqueue_promoted, the real producer, onto a real (moto) queue.
  2. SUBMIT API: the real HTTP handler over a real socket.
  3. SWEEPER: a worker claims the promoted job and DIES mid-inference (the claim is left in
     processing, exactly what a killed pod leaves); the lease expires; the real sweeper re-drives
     it from what the claim persisted. The capability and the route must survive the orphaning.
  4. ONE SPELLING: no async-worker module other than message_contract.py spells a routing key.
"""
from __future__ import annotations

import http.client
import json
import os
import re
import sys
import threading
import time

os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")
os.environ.setdefault("GATEWAY_ENDPOINT", "http://gateway.inference.svc.cluster.local/v1")
os.environ.setdefault("DDB_TABLE", "inference-jobs")
os.environ.setdefault("CLAIM_TTL_SECONDS", "25")
os.environ.setdefault("RETENTION_SECONDS", "21600")
os.environ.setdefault("SWEEP_GRACE_SECONDS", "90")
os.environ.setdefault("SWEEP_COOLDOWN_SECONDS", "300")
for k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
    os.environ[k] = "testing"

import boto3  # noqa: E402
from moto import mock_aws  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
AW = os.path.join(ROOT, "async-worker")
TABLE = os.environ["DDB_TABLE"]
PAYLOAD = {"model": "Qwen/Qwen3-32B-AWQ", "prompt": "Write a long essay.", "max_tokens": 4096}


class Died(BaseException):
    """A worker killed mid-inference: not an Exception, so handle() cannot catch it and mark failed."""


@mock_aws
def main() -> int:
    boto3.client("dynamodb").create_table(
        TableName=TABLE, BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[{"AttributeName": "job_id", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "job_id", "KeyType": "HASH"}])
    os.environ["SQS_QUEUE_URL"] = boto3.client("sqs").create_queue(QueueName="inference")["QueueUrl"]

    sys.path.insert(0, AW)
    import importlib
    mc = importlib.import_module("message_contract")
    jobstore = importlib.import_module("jobstore")
    worker = importlib.import_module("worker")
    sweeper = importlib.import_module("sweeper")
    promo = importlib.import_module("promotion")
    relay_server = importlib.import_module("relay_server")
    submit_api = importlib.import_module("submit_api")
    job_key = importlib.import_module("jobkey").job_key

    seen = []

    def capture(payload, correlation_id, path=None, capability=None, **_):
        seen.append({"path": path, "capability": capability})
        return {"choices": [{"text": "ok"}]}, "10.0.0.9:8000"

    def receive():
        r = jobstore.sqs.receive_message(QueueUrl=jobstore.SQS_QUEUE_URL, MaxNumberOfMessages=1,
                                         WaitTimeSeconds=0).get("Messages", [])
        assert r, "the producer put NOTHING on the queue"
        return r[0]

    # ---- 1. PROMOTION, the producer that actually creates premium on the queue ------------------
    caller = "acme"
    jid = job_key(caller, PAYLOAD)
    sla, cap = promo.promoted_pair("interactive", "premium")
    rec = promo.promotion_record(jid, "interactive", "premium", caller, 121.0)
    assert relay_server._enqueue_promoted(jid, json.dumps(PAYLOAD), rec, None, "premium", caller,
                                          path="/v1/completions"), "promotion enqueue returned False"
    msg = receive()
    body = json.loads(msg["Body"])
    assert job_key(body["client_id"], body["payload"]) == jid, "the routing siblings leaked into the dedup hash"
    worker.run_inference = capture
    worker.handle(msg)
    assert seen[-1] == {"path": "/v1/completions", "capability": "premium"}, (
        f"PROMOTION: the worker called the gateway with {seen[-1]}, so a promoted premium job would be "
        "served by the standard pool (defect 3 on the path it was meant to close)")
    print(f"OK  1 promotion ({sla}+{cap}): the real producer's capability and route reach the worker's wire call")

    # ---- 2. SUBMIT API, over a real socket -------------------------------------------------------
    srv = submit_api.ThreadingHTTPServer(("127.0.0.1", 0), submit_api.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    p2 = dict(PAYLOAD, prompt="a directly submitted job")
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
    conn.request("POST", "/submit", json.dumps({"client_id": "beta", "payload": p2, "capability": "standard"}),
                 {"content-type": "application/json"})
    assert conn.getresponse().status == 202
    srv.shutdown()
    worker.handle(receive())
    assert seen[-1]["capability"] == "standard", f"SUBMIT API: worker saw {seen[-1]}"
    print("OK  2 submit API: the declared capability reaches the worker's wire call")

    # ---- 3. SWEEPER, after a worker dies holding a promoted premium claim -------------------------
    p3 = dict(PAYLOAD, prompt="a promoted job whose worker is killed")
    jid3 = job_key(caller, p3)
    rec3 = promo.promotion_record(jid3, "interactive", "premium", caller, 121.0)
    assert relay_server._enqueue_promoted(jid3, json.dumps(p3), rec3, None, "premium", caller,
                                          path="/v1/completions")
    m3 = receive()

    def die(*a, **k):
        raise Died()
    worker.run_inference = die
    try:
        worker.handle(m3)
        raise AssertionError("the simulated worker death did not happen")
    except Died:
        pass
    # The pod is gone and so is its message: delete it, as a lost visibility window would.
    jobstore.sqs.delete_message(QueueUrl=jobstore.SQS_QUEUE_URL, ReceiptHandle=m3["ReceiptHandle"])
    item = jobstore.ddb.get_item(TableName=TABLE, Key={"job_id": {"S": jid3}})["Item"]
    assert item["status"]["S"] == "processing", "the death must leave the claim in processing"
    assert "routing" in item, "the claim did not persist the routing siblings, so the sweeper cannot restore them"
    now = int(time.time())
    jobstore.ddb.update_item(TableName=TABLE, Key={"job_id": {"S": jid3}},
                             UpdateExpression="SET lease_expires = :l, claimed_at = :c",
                             ExpressionAttributeValues={":l": {"N": str(now - 100)}, ":c": {"N": str(now - 200)}})
    assert sweeper.sweep_once()["enqueued"] == 1, "the sweeper did not re-drive the orphan"
    worker.run_inference = capture
    worker.handle(receive())
    assert seen[-1] == {"path": "/v1/completions", "capability": "premium"}, (
        f"SWEEPER: the re-driven orphan reached the gateway as {seen[-1]}; it lost what the promotion carried")
    print("OK  3 sweeper: an orphaned promoted premium job is re-driven with its capability AND its route")

    # ---- 4. ONE SPELLING ---------------------------------------------------------------------------
    offenders = []
    for fn in sorted(os.listdir(AW)):
        if not fn.endswith(".py") or fn == "message_contract.py":
            continue
        src = open(os.path.join(AW, fn)).read()
        for lit in ('"capability_class"', '"sla_class"'):
            if lit in src:
                offenders.append(f"{fn} spells {lit}")
        for m in re.finditer(r'body\.get\("(path|capability)"\)', src):
            if not (fn == "submit_api.py" and m.group(1) == "capability"):   # the public API INPUT field
                offenders.append(f"{fn} reads body.get(\"{m.group(1)}\")")
    assert not offenders, "routing keys spelled outside message_contract.py: " + "; ".join(offenders)
    print("OK  4 one spelling: no async-worker module spells a routing key except message_contract.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
