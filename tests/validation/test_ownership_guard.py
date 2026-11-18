# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Local unit proof for the async-worker claim-token ownership guard (Phase 0.2).

Runs against a mocked DynamoDB (moto), so it needs no cluster or AWS credentials.
It proves the two properties that matter:

  1. Claim-time dedup: a second claim on an already-owned job is refused.
  2. Ownership guard: if a worker stalls past the claim TTL and a second worker
     steals the job, the first worker's later "complete" MUST NOT overwrite the
     new owner's state (mark returns False; the item keeps the new owner's token).

The cluster-scale idempotency test (make test-idempotency) is the complementary
end-to-end proof and still runs against the live cluster.

Run:  ./.venv/bin/python tests/validation/test_ownership_guard.py
"""
import os

os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")
os.environ.setdefault("SQS_QUEUE_URL", "https://sqs.us-west-2.amazonaws.com/000/x")
os.environ.setdefault("GATEWAY_ENDPOINT", "http://gateway.inference.svc.cluster.local/v1")
os.environ.setdefault("DDB_TABLE", "inference-jobs")
os.environ.setdefault("CLAIM_TTL_SECONDS", "600")

import sys

import boto3
from moto import mock_aws

TABLE = os.environ["DDB_TABLE"]


def _get(ddb, key):
    return ddb.get_item(TableName=TABLE, Key={"job_id": {"S": key}}).get("Item")


@mock_aws
def main() -> int:
    # Mocked table with the real key schema.
    boto3.client("dynamodb").create_table(
        TableName=TABLE,
        AttributeDefinitions=[{"AttributeName": "job_id", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "job_id", "KeyType": "HASH"}],
        BillingMode="PAY_PER_REQUEST",
    )
    # Import AFTER the mock + table exist: worker's module-level client is mocked.
    import importlib
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "async-worker"))
    worker = importlib.import_module("worker")
    ddb = worker.ddb

    # ---- 1. claim-time dedup -------------------------------------------------
    k1 = "client#aaa"
    tok1 = worker.try_claim(k1)
    assert tok1 is not None, "first claim should win"
    dup = worker.try_claim(k1)
    assert dup is None, "second claim on a live job must be refused (dedup)"
    print("OK  dedup: second concurrent claim refused")

    # ---- 2. ownership guard under a stolen (stale) claim ---------------------
    k2 = "client#bbb"
    tok_a = worker.try_claim(k2)
    assert tok_a is not None
    assert _get(ddb, k2)["claim_count"]["N"] == "1", "first claim sets claim_count=1"
    # Simulate worker A stalling PAST the claim lease: age lease_expires so the
    # stale-steal condition in try_claim matches. (Steal keys on the lease, not on
    # the DynamoDB retention ttl, which stays far in the future.)
    ddb.update_item(
        TableName=TABLE, Key={"job_id": {"S": k2}},
        UpdateExpression="SET lease_expires = :old",
        ExpressionAttributeValues={":old": {"N": str(worker.now_epoch() - 10_000)}},
    )
    # Worker B steals it.
    tok_b = worker.try_claim(k2)
    assert tok_b is not None and tok_b != tok_a, "B should steal the stale claim with a new token"
    item = _get(ddb, k2)
    assert item["claim_token"]["S"] == tok_b, "item should now carry B's token"
    assert item["claim_count"]["N"] == "2", "durable claim_count must increment on the re-claim"
    # Retention ttl must be far in the future, independent of the short lease.
    assert int(item["ttl"]["N"]) > worker.now_epoch() + 3600, "retention ttl decoupled from lease"

    # Worker A revives and tries to complete with its OLD token: must be refused.
    a_wrote = worker.mark(k2, "complete", tok_a, {"result": "A-late-result"})
    assert a_wrote is False, "superseded worker A must NOT overwrite"
    item = _get(ddb, k2)
    assert item["claim_token"]["S"] == tok_b, "A must not change ownership"
    assert item["status"]["S"] == "processing", "A must not flip status to complete"
    assert "result" not in item, "A must not write its result"
    print("OK  ownership guard: superseded worker A did not overwrite B")

    # Worker B (the real owner) completes: must succeed.
    b_wrote = worker.mark(k2, "complete", tok_b, {"result": "B-result"})
    assert b_wrote is True, "current owner B must be able to complete"
    item = _get(ddb, k2)
    assert item["status"]["S"] == "complete" and item["result"]["S"] == "B-result"
    print("OK  owner B completed normally")

    print("PASS: claim-token ownership guard holds")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
