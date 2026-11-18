# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
LT-3 concurrent submitter: N distinct jobs, each submitted D times concurrently to
force duplicate delivery. Deterministic bodies so duplicates hash identically. Short
generations (correctness is length independent) to stay within the cost budget.

Env: SUBMIT_URL, N_JOBS, DUP, RUN_TAG, MAX_TOKENS, CONCURRENCY.
Prints the submitted count. The verdict is analyze.py lt3 (worker_claims_total delta
and DynamoDB complete count), not this script.
"""
import concurrent.futures as cf
import os
import requests

SUBMIT_URL = os.environ["SUBMIT_URL"]
N = int(os.environ.get("N_JOBS", "1000"))
D = int(os.environ.get("DUP", "3"))
RUN_TAG = os.environ["RUN_TAG"]
MAX_TOKENS = int(os.environ.get("MAX_TOKENS", "8"))
CONC = int(os.environ.get("CONCURRENCY", "60"))
MODEL = os.environ.get("LOADTEST_MODEL", "NousResearch/Meta-Llama-3.1-8B-Instruct")


def body(i: int) -> dict:
    # Deterministic per distinct job i; the D copies are byte-identical so they
    # hash to the same job_id and must dedup to one GPU run.
    return {"client_id": RUN_TAG,
            "payload": {"model": MODEL,
                        "messages": [{"role": "user", "content": f"lt3 job {i}"}],
                        "max_tokens": MAX_TOKENS, "temperature": 0.0}}


def submit_one(args):
    i, _ = args
    try:
        r = requests.post(SUBMIT_URL, json=body(i), timeout=30)
        return 1 if r.status_code == 202 else 0
    except Exception:
        return 0


def main():
    # (i, copy) for every distinct job i, D copies each. Concurrency forces overlap.
    work = [(i, c) for i in range(N) for c in range(D)]
    ok = 0
    with cf.ThreadPoolExecutor(max_workers=CONC) as ex:
        for r in ex.map(submit_one, work):
            ok += r
    print(f"submitted ok={ok} of {N*D} ({N} distinct x {D})")


if __name__ == "__main__":
    main()
