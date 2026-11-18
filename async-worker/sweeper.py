# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Orphan sweeper (DESIGN.md section 3.4).

The async worker recovers an interrupted job when its SQS message redelivers after
the visibility timeout: the redelivery re-claims the job through the stale-claim steal
path in jobstore.try_claim. That covers almost every worker-death case.

One case it does NOT cover: a job whose SQS message is GONE while the job is still
stuck in `processing`. This happens on a dedup-or-redelivery race with a killed
worker, where a redelivered copy is deleted as a duplicate at the same moment the
owning worker dies, so no further redelivery is coming to re-drive the job. LT-6
surfaced exactly one such orphan per 300-job run. This sweeper closes that gap.

The sweeper is a RECONCILER, not a queue consumer. Per run it:
  1. Finds orphans: items with status=processing whose lease has expired AND that have
     been stuck long enough (past a grace period) that SQS redelivery, if it were
     coming, would already have happened.
  2. For each orphan not re-enqueued within a cooldown window, re-enqueues an SQS
     message carrying the stored payload, guarded by a conditional write on requeued_at
     so only one sweeper wins.
  3. Does NOT change the item's status or lease. The worker's stale-claim steal path
     does the actual re-claim when the re-enqueued message is received.

Correctness invariants (asserted in tests/validation/test_sweeper.py):
  - Only touches abandoned jobs. A `complete` item, and a `processing` item whose lease
    is still in the future (a live worker), are left untouched by construction.
  - Does not race SQS. The grace period exceeds the queue's visibility timeout, so the
    sweeper only acts once redelivery would already have fired. If the sweeper and SQS
    both drive the same job, the worker claim logic dedups it safely; the grace period
    keeps that redundant work rare.
  - At-least-once for interrupted jobs. A swept job runs at least once and completes.
    If the original worker finished inference but died before writing `complete`, the
    re-run is a second GPU run. This is the SAME at-least-once property interrupted jobs
    already have, and it does NOT weaken the exactly-once guarantee for non-interrupted
    jobs (LT-3), which the sweeper never touches (they are never orphans).
  - Idempotent against itself. A cooldown plus a conditional requeued_at write stop two
    sweeper runs (or two concurrent sweeper pods) from re-enqueueing the same orphan
    twice in quick succession.

Least privilege: this is the only component that scans for abandoned jobs, so it (and
only it) needs dynamodb:Scan/Query plus UpdateItem, sqs:SendMessage, and
sqs:GetQueueAttributes (to read the visibility timeout). See async-worker/iam/sweeper-*.

Environment:
    SQS_QUEUE_URL           the job queue (re-enqueue target)
    DDB_TABLE               the job-state table (default inference-jobs)
    SWEEP_GRACE_SECONDS     override the grace period. If unset (default), grace is
                            derived from the queue's VisibilityTimeout + SWEEP_MARGIN,
                            so it always exceeds the current visibility.
    SWEEP_MARGIN_SECONDS    margin added to the visibility timeout (default 60)
    SWEEP_COOLDOWN_SECONDS  minimum gap between two re-enqueues of the same orphan (300)
    SWEEP_MAX_ITEMS         safety cap on items examined per run (bounded scan; 5000)
"""

import json
import logging
import os

from botocore.exceptions import ClientError

import jobstore
from jobstore import DDB_TABLE, SQS_QUEUE_URL, ddb, iso_now, now_epoch, sqs

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("sweeper")

SWEEP_MARGIN_SECONDS = int(os.environ.get("SWEEP_MARGIN_SECONDS", "60"))
SWEEP_COOLDOWN_SECONDS = int(os.environ.get("SWEEP_COOLDOWN_SECONDS", "300"))
SWEEP_MAX_ITEMS = int(os.environ.get("SWEEP_MAX_ITEMS", "5000"))
# When set (> 0), overrides the derived grace. Otherwise grace is derived from the
# queue's own visibility timeout so it is always > visibility (the correctness model).
_GRACE_OVERRIDE = int(os.environ.get("SWEEP_GRACE_SECONDS", "0"))


def grace_seconds() -> int:
    """Grace period the orphan must have been stuck for before we act. MUST exceed the
    SQS visibility timeout so we never race the queue's own redelivery. Derived from the
    live queue attributes unless SWEEP_GRACE_SECONDS overrides it."""
    if _GRACE_OVERRIDE > 0:
        return _GRACE_OVERRIDE
    vis = int(
        sqs.get_queue_attributes(
            QueueUrl=SQS_QUEUE_URL, AttributeNames=["VisibilityTimeout"]
        )["Attributes"]["VisibilityTimeout"]
    )
    return vis + SWEEP_MARGIN_SECONDS


def find_orphans(grace: int, now: int) -> list[dict]:
    """Items that are genuinely abandoned: status=processing, lease expired (no live
    worker holds the claim), and claimed long enough ago that SQS redelivery would
    already have fired. Complete items and future-lease (live-worker) items are
    excluded by the filter, so the sweeper cannot touch a healthy job.

    Uses a bounded Scan with a server-side filter. Production wants a GSI on `status`
    to avoid a full-table scan; noted, not required at this scale.
    """
    cutoff = now - grace
    orphans: list[dict] = []
    scanned = 0
    start_key = None
    while True:
        kwargs = {
            "TableName": DDB_TABLE,
            # status == processing AND lease already expired AND claimed before cutoff.
            # The lease guard is essential: it excludes any live worker even if grace
            # were ever misconfigured below the lease TTL.
            "FilterExpression": "#s = :proc AND lease_expires < :now AND claimed_at < :cutoff",
            "ExpressionAttributeNames": {"#s": "status"},
            "ExpressionAttributeValues": {
                ":proc": {"S": "processing"},
                ":now": {"N": str(now)},
                ":cutoff": {"N": str(cutoff)},
            },
        }
        if start_key:
            kwargs["ExclusiveStartKey"] = start_key
        resp = ddb.scan(**kwargs)
        orphans.extend(resp.get("Items", []))
        scanned += resp.get("ScannedCount", 0)
        start_key = resp.get("LastEvaluatedKey")
        if not start_key or scanned >= SWEEP_MAX_ITEMS:
            if start_key:
                log.warning("scan cap %d reached; deferring the rest to the next run", SWEEP_MAX_ITEMS)
            break
    return orphans


def requeue_orphan(item: dict, now: int) -> str:
    """Re-enqueue one orphan so a worker can re-claim it. Returns the outcome:
    'enqueued', 'cooldown' (re-enqueued too recently), 'lost-race' (another sweeper
    won the conditional write), or 'no-payload' (item lacks a stored payload).

    The requeued_at write is conditional so only one sweeper (of possibly many) wins
    and enqueues; the loser skips. requeued_at is set BEFORE the send, so a failed send
    at worst defers recovery to the next run after the cooldown, never a double-enqueue.
    """
    key = item["job_id"]["S"]
    payload_s = item.get("payload", {}).get("S")
    client_id = item.get("client_id", {}).get("S")
    if not payload_s or client_id is None:
        # No stored request: cannot reconstruct the message. This can only happen for
        # an item written before payload persistence shipped; log and skip.
        log.warning("orphan job_id=%s has no stored payload; cannot re-drive", key)
        return "no-payload"

    prev = item.get("requeued_at", {}).get("N")
    if prev is not None and int(prev) > now - SWEEP_COOLDOWN_SECONDS:
        return "cooldown"

    # Conditionally stamp requeued_at so exactly one sweeper wins.
    values = {":now": {"N": str(now)}, ":u": {"S": iso_now()}}
    if prev is None:
        cond = "attribute_not_exists(requeued_at)"
    else:
        cond = "requeued_at = :prev"
        values[":prev"] = {"N": prev}
    try:
        ddb.update_item(
            TableName=DDB_TABLE,
            Key={"job_id": {"S": key}},
            UpdateExpression="SET requeued_at = :now, updated_at = :u",
            ConditionExpression=cond,
            ExpressionAttributeValues=values,
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return "lost-race"  # another sweeper stamped it first
        raise

    # Re-enqueue with the SAME message body shape the submit API produces, so the
    # worker's handle() reconstructs the identical job_key and re-claims via the steal
    # path. Status and lease are deliberately left untouched.
    # WITH THE ROUTING SIBLINGS the claim persisted (message_contract.py). Before 2026-09-22 this sent
    # client_id and payload only, so an orphaned promoted premium job came back on the standard pool
    # and a /v1/completions job came back as chat. Absent on items claimed before this change, in
    # which case the message is exactly what it always was.
    routing = json.loads(item.get("routing", {}).get("S") or "{}")
    sqs.send_message(
        QueueUrl=SQS_QUEUE_URL,
        MessageBody=json.dumps({"client_id": client_id, "payload": json.loads(payload_s), **routing}),
    )
    log.info("re-enqueued orphan job_id=%s", key)
    return "enqueued"


def sweep_once() -> dict:
    """One reconciler pass. Returns a summary of what it found and did."""
    now = now_epoch()
    grace = grace_seconds()
    orphans = find_orphans(grace, now)
    counts = {"orphans": len(orphans), "enqueued": 0, "cooldown": 0, "lost-race": 0, "no-payload": 0}
    for item in orphans:
        counts[requeue_orphan(item, now)] += 1
    log.info(
        "sweep done grace=%ds orphans=%d enqueued=%d cooldown=%d lost_race=%d no_payload=%d",
        grace, counts["orphans"], counts["enqueued"], counts["cooldown"],
        counts["lost-race"], counts["no-payload"],
    )
    return counts


if __name__ == "__main__":
    sweep_once()
