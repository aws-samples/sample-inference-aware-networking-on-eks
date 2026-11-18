# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Shared job-state store for the async front door.

This module is the single home of the DynamoDB claim, key, and state-transition
logic so the worker (async-worker/worker.py) and the orphan sweeper
(async-worker/sweeper.py) use exactly the same primitives instead of duplicating
them. It deliberately has NO vLLM or inference dependency, so the sweeper can
import it without carrying the worker's runtime configuration.

The correctness guarantee (proven by tests/validation/test_idempotency.py and
test_ownership_guard.py): a duplicate submission of the same logical job never runs
inference twice on a GPU, even though Amazon SQS standard queues are at-least-once.
Deduplication happens with a DynamoDB conditional write at claim time.

Job identity is the primary key client_id#request_hash. Every access is by that
primary key, so the WORKER needs only GetItem, PutItem, UpdateItem. The SWEEPER is
the one component that scans for abandoned jobs, so it (and only it) additionally
needs Scan or Query. See async-worker/iam/.
"""

import json
import os
import time
import uuid
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError

# The ONE definition of job identity, shared with the producer (submit_api.py) so the
# two can never drift. Re-exported here so `from jobstore import job_key` keeps working
# for worker.py and sweeper.py.
from jobkey import job_key  # noqa: F401

DDB_TABLE = os.environ.get("DDB_TABLE", "inference-jobs")
SQS_QUEUE_URL = os.environ.get("SQS_QUEUE_URL", "")
CLAIM_TTL_SECONDS = int(os.environ.get("CLAIM_TTL_SECONDS", "600"))
# DynamoDB item retention (the table TTL keys on the `ttl` attribute). This is
# DECOUPLED from the claim lease: the lease governs stale-claim stealing, retention
# governs when DynamoDB deletes the row. Keeping them separate stops a short claim
# lease from evicting completed items before a test can count them (the LT-6 bug).
RETENTION_SECONDS = int(os.environ.get("RETENTION_SECONDS", "21600"))  # 6h

ddb = boto3.client("dynamodb")
sqs = boto3.client("sqs")


def now_epoch() -> int:
    return int(time.time())


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_item(key: str) -> dict | None:
    return ddb.get_item(TableName=DDB_TABLE, Key={"job_id": {"S": key}}).get("Item")


def try_claim(key: str, client_id: str | None = None, payload: dict | None = None,
              routing: dict | None = None) -> str | None:
    """Atomically claim a job for processing.

    Returns a unique claim_token (a uuid) if this worker won the claim and should
    run inference. Returns None if the job is already claimed or complete (a
    duplicate).

    The claim is a conditional UpdateItem that succeeds only if no live item exists
    for this key. "Live" means: no item at all, OR an item whose LEASE has expired
    (stale, from a crashed worker) OR a prior failed attempt past its lease. This is
    what makes the dedup correct under SQS at-least-once delivery and worker crashes.

    Payload persistence (required by the orphan sweeper): when client_id and payload
    are supplied, the claim write ALSO stores them on the item (client_id as an
    attribute, payload as a JSON string). Because ANY item in `processing` was
    necessarily claimed, the payload is then always present on the item, so the
    sweeper can reconstruct and re-enqueue an orphan whose SQS message is gone. For
    the test workload (short requests) storing the payload inline is fine; in
    production a large payload is written to S3 and only the S3 URI is stored here,
    to stay under the DynamoDB 400 KB item-size limit.

    Two independent timers on the item:
      lease_expires = now + CLAIM_TTL_SECONDS  (used ONLY in the steal condition)
      ttl           = now + RETENTION_SECONDS  (DynamoDB item retention; the table TTL
                      keys on this, so a short lease does not evict completed rows)

    claim_count is a DURABLE per-job counter incremented on every successful claim
    (ADD). It survives worker-pod death (unlike a per-pod Prometheus counter), so a
    durability test can count real GPU runs as sum(claim_count).

    claim_token is the OWNERSHIP proof: a steal writes a NEW token, so a stalled
    worker's later complete/fail (guarded on the old token) is refused.
    """
    token = str(uuid.uuid4())
    now = now_epoch()
    set_expr = (
        "SET #s = :proc, claim_token = :tok, claimed_at = :now_s, "
        "updated_at = :u, lease_expires = :lease, #ttl = :ret"
    )
    names = {"#s": "status", "#ttl": "ttl"}
    values = {
        ":proc": {"S": "processing"},
        ":tok": {"S": token},
        ":now_s": {"N": str(now)},
        ":u": {"S": iso_now()},
        ":lease": {"N": str(now + CLAIM_TTL_SECONDS)},
        ":ret": {"N": str(now + RETENTION_SECONDS)},
        ":one": {"N": "1"},
        ":failed": {"S": "failed"},
        ":processing": {"S": "processing"},
        ":now_n": {"N": str(now)},
    }
    # Persist the request so a message-less orphan can be reconstructed by the sweeper.
    if client_id is not None:
        set_expr += ", client_id = :cid"
        values[":cid"] = {"S": client_id}
    if payload is not None:
        set_expr += ", payload = :pl"
        values[":pl"] = {"S": json.dumps(payload, separators=(",", ":"))}
    # THE ROUTING SIBLINGS TOO (message_contract.py), or the sweeper re-drives an orphan to the wrong
    # pool and the wrong route: it can only restore what the claim stored. Added 2026-09-22.
    if routing:
        set_expr += ", routing = :rt"
        values[":rt"] = {"S": json.dumps(routing, separators=(",", ":"))}
    try:
        ddb.update_item(
            TableName=DDB_TABLE,
            Key={"job_id": {"S": key}},
            UpdateExpression=set_expr + " ADD claim_count :one",
            # Win the claim only if there is no live record for this key.
            # attribute_not_exists(job_id): no record at all (first claim, upsert).
            # OR status=failed with an expired lease: a retryable prior failure.
            # OR status=processing with an expired lease: a stale claim from a
            #    crashed worker, safe to steal.
            ConditionExpression=(
                "attribute_not_exists(job_id) "
                "OR (#s = :failed AND lease_expires < :now_n) "
                "OR (#s = :processing AND lease_expires < :now_n)"
            ),
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )
        return token
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            # Someone else owns a live claim, or the job is already complete.
            # This is the dedup path. Not an error.
            return None
        raise


def mark(key: str, status: str, token: str, extra: dict | None = None) -> bool:
    """Update status, guarded by claim ownership.

    The update applies ONLY if the item's claim_token still equals this worker's
    token. Returns True if written, False if the worker has been superseded (its
    claim was stolen after the TTL lapsed) in which case it must NOT overwrite the
    new owner's state. Any other error propagates.
    """
    names = {"#s": "status"}
    values = {
        ":s": {"S": status},
        ":u": {"S": iso_now()},
        ":tok": {"S": token},
    }
    expr = "SET #s = :s, updated_at = :u"
    if extra:
        for i, (k, v) in enumerate(extra.items()):
            names[f"#k{i}"] = k
            values[f":v{i}"] = {"S": str(v)}
            expr += f", #k{i} = :v{i}"
    try:
        ddb.update_item(
            TableName=DDB_TABLE,
            Key={"job_id": {"S": key}},
            UpdateExpression=expr,
            ConditionExpression="claim_token = :tok",
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise


def release(key: str, token: str) -> bool:
    """Release a claim so the job is immediately re-claimable, guarded by ownership.

    Used when a worker cannot process a job for an INFRASTRUCTURE reason (a gateway 5xx from
    an Endpoint Picker outage, or a connection error), which is not the job's fault. The job
    must NOT be marked failed and must stay retryable, and the worker must not keep holding
    the lease. This expires the lease (sets lease_expires to 0), so try_claim's steal
    condition (status processing AND lease_expires < now) matches on the next delivery and a
    healthy worker re-claims. Status is left unchanged. Returns True if released, False if the
    worker was superseded (another worker already owns the claim).

    Durable accounting (B.2): this also increments infra_rejections (ADD), a per-job durable
    counter of infra-rejected attempts. claim_count increments at claim time, BEFORE inference,
    so it over-counts real GPU runs by exactly the number of infra rejections (a rejected attempt
    won a claim but never reached a GPU). Real GPU runs therefore equal
    sum(claim_count) - sum(infra_rejections). A run that quotes claim_count as GPU runs must
    subtract infra_rejections (or assert it is zero). Both survive a worker-pod death."""
    try:
        ddb.update_item(
            TableName=DDB_TABLE,
            Key={"job_id": {"S": key}},
            UpdateExpression="SET lease_expires = :zero, updated_at = :u ADD infra_rejections :one",
            ConditionExpression="claim_token = :tok",
            ExpressionAttributeValues={
                ":zero": {"N": "0"}, ":u": {"S": iso_now()}, ":tok": {"S": token}, ":one": {"N": "1"},
            },
        )
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise
