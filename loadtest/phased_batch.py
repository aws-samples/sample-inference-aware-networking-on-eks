# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Phase D batch-tier driver: submit long async jobs through the submit API. Jobs are grouped
into a few prefix-groups; every job in a group shares a long common prefix, so the batch
Endpoint Picker's prefix-cache scorer has a reason to co-locate the group on one backend
pod. This is the input for the inference-aware-placement check (D.2): same-prefix jobs land
on the same pod, visible as a shared upstream in the trace and a rising prefix-cache hit
rate. The correlation id for each job is its deterministic job_id (client_id#hash).

Env: SUBMIT_URL, TAG (client_id), N (default 30), GROUPS (default 3), MAX_TOKENS (default 256).

MODE selects the admission pattern (LT-8):
  interleaved (default): all groups admitted at once (byte-identical to the original behaviour).
  grouped:               groups admitted one at a time; group g+1 is not submitted until every
                         job in group g has reached a terminal state, detected from the DURABLE
                         DynamoDB job records (not queue depth). Needs DDB_TABLE and AWS creds.
"""
import concurrent.futures as cf
import os
import requests

SUBMIT_URL = os.environ["SUBMIT_URL"]
TAG = os.environ["TAG"]
N = int(os.environ.get("N", "30"))
GROUPS = int(os.environ.get("GROUPS", "3"))
MAX_TOKENS = int(os.environ.get("MAX_TOKENS", "256"))
MODEL = os.environ.get("LOADTEST_MODEL", "NousResearch/Meta-Llama-3.1-8B-Instruct")
MODE = os.environ.get("MODE", "interleaved")          # interleaved | grouped
DDB_TABLE = os.environ.get("DDB_TABLE", "inference-jobs")
DRAIN_TIMEOUT = int(os.environ.get("DRAIN_TIMEOUT", "1800"))  # per-group drain ceiling, seconds
# Controls the shared-prefix length L (repetitions of the base sentence). Default 8 keeps the
# original behaviour byte-identical; LT-8 sizes L from the batch cache derivation.
PREFIX_REPS = int(os.environ.get("PREFIX_REPS", "8"))
# Salts the prefix CONTENT so each LT-8 cell uses brand-new prefixes and starts cold WITHOUT
# restarting the replicas (a documented alternative to the rollout-restart cache reset: unique
# prefixes are never in cache, so the per-cell hit rate is that cell's own cold-start behaviour).
# Empty by default, which keeps the prefix string byte-identical to the original.
SALT = os.environ.get("SALT", "")

# A long shared prefix per group. Long enough to matter for prefix caching; the batch EPP
# co-locates jobs that share it.
PREFIXES = [
    "SharedContext" + SALT + "Group{g}: " + ("The following is a long standing instruction set "
    "that all requests in this group share verbatim so that the prefix cache is populated once "
    "and reused. ") * PREFIX_REPS
    for g in range(GROUPS)
]


def body(i: int) -> dict:
    g = i % GROUPS
    # client_id carries the prefix-group so the trace can check co-location per group
    # (all jobs of a group share a prefix; the batch Endpoint Picker should place them on
    # one pod). The correlation id is client_id#hash, so group g jobs are TAG-g{g}#...
    return {"client_id": f"{TAG}-g{g}",
            "payload": {"model": MODEL,
                        "messages": [{"role": "user",
                                      "content": PREFIXES[g].format(g=g) + f" Now answer distinct question number {i}."}],
                        "max_tokens": MAX_TOKENS, "temperature": 0.0}}


def submit_one(i: int) -> int:
    try:
        r = requests.post(SUBMIT_URL, json=body(i), timeout=30)
        return 1 if r.status_code == 202 else 0
    except Exception:
        return 0


def main():
    ok = 0
    with cf.ThreadPoolExecutor(max_workers=20) as ex:
        for r in ex.map(submit_one, range(N)):
            ok += r
    print(f"batch jobs submitted ok={ok} of {N} ({GROUPS} prefix-groups, tag={TAG})")


def _submit_collect(i: int):
    """Submit one job and return its durable job_id (from the 202 response), or None."""
    try:
        r = requests.post(SUBMIT_URL, json=body(i), timeout=30)
        if r.status_code == 202:
            return r.json().get("job_id")
    except Exception:
        return None
    return None


def grouped_main():
    """GROUPED admission: submit one prefix group, wait for it to fully drain (all its jobs in a
    terminal DynamoDB state), then submit the next. Drain is read from the durable job records, so
    it is robust to SQS eventual consistency and to a job whose message is briefly invisible."""
    import time
    import boto3
    ddb = boto3.client("dynamodb")

    def is_terminal(job_id: str) -> bool:
        item = ddb.get_item(TableName=DDB_TABLE, Key={"job_id": {"S": job_id}}).get("Item")
        if not item:
            return False  # not yet claimed/written
        return item.get("status", {}).get("S") in ("complete", "failed")

    total = 0
    for g in range(GROUPS):
        idxs = [i for i in range(N) if i % GROUPS == g]
        job_ids = []
        with cf.ThreadPoolExecutor(max_workers=20) as ex:
            for jid in ex.map(_submit_collect, idxs):
                if jid:
                    job_ids.append(jid)
        print(f"group {g}: submitted {len(job_ids)} of {len(idxs)}; waiting for drain (<= {DRAIN_TIMEOUT}s)")
        start = time.time()
        pending = list(job_ids)
        while pending and time.time() - start < DRAIN_TIMEOUT:
            pending = [j for j in job_ids if not is_terminal(j)]
            if pending:
                # Deliberate fixed wait in a measurement or test script: poll interval while draining jobs, bounded by DRAIN_TIMEOUT.
                time.sleep(5)  # nosemgrep
        if pending:
            print(f"group {g}: DRAIN TIMEOUT, {len(pending)} of {len(job_ids)} still pending (validity gate: investigate before trusting this cell)")
        else:
            print(f"group {g}: drained ({len(job_ids)} jobs terminal) in {int(time.time() - start)}s")
        total += len(job_ids)
    print(f"batch jobs submitted (grouped) ok={total} of {N} ({GROUPS} prefix-groups, tag={TAG})")


if __name__ == "__main__":
    if MODE == "grouped":
        grouped_main()
    else:
        main()
