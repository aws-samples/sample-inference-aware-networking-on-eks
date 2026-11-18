# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Headline validation test for Layer 2.

Submits the SAME logical job twice, concurrently, and asserts EXACTLY ONE GPU
inference job runs. This proves the DynamoDB conditional-claim dedup in
async-worker/worker.py holds even though SQS is at-least-once.

Pass criteria:
  1. Both submissions accept (two 202s, both referencing the same job_id).
  2. The DynamoDB item for that job_id transitions to `complete` exactly once.
  3. The GPU-side count (worker "claimed job_id=" log lines for this job) is ONE.

Run (from the repo root, with the submit-api port-forwarded):
  kubectl -n inference port-forward svc/submit-api 8080:8080 &
  SUBMIT_URL=http://localhost:8080/submit DDB_TABLE=inference-jobs \
    AWS_PROFILE=eksinference AWS_DEFAULT_REGION=us-west-2 \
    python tests/validation/test_idempotency.py
"""
import concurrent.futures as cf
import hashlib
import json
import os
import subprocess
import time
import uuid

import boto3
import requests

SUBMIT_URL = os.environ["SUBMIT_URL"]        # async submit endpoint (enqueues to SQS)
DDB_TABLE = os.environ.get("DDB_TABLE", "inference-jobs")
NAMESPACE = os.environ.get("NAMESPACE", "inference")
CLIENT_ID = "idempotency-test"
# MUST MATCH THE SERVED MODEL, and it silently did not between the spec-002 swap and
# 2026-09-21: the default still named Llama-3.1 while the cluster served Qwen3-8B. MODEL
# feeds build_payload and therefore the job_key hash, so the headline exactly-once test
# would have exercised a model the cluster does not serve.
MODEL = os.environ.get("MODEL", "Qwen/Qwen3-8B")  # must match the served standard model
ddb = boto3.client("dynamodb")


def build_payload(nonce: str) -> dict:
    """The inference payload. A unique nonce in the prompt makes each test run's
    job_id unique, so the GPU-run count is unambiguous."""
    return {"model": MODEL, "messages": [{"role": "user", "content": f"marker {nonce}"}]}


def job_key(payload: dict) -> str:
    """Byte-identical to worker.py.job_key() / submit_api.py.job_key()."""
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return f"{CLIENT_ID}#{hashlib.sha256(body).hexdigest()[:32]}"


def submit(nonce: str) -> str:
    r = requests.post(
        SUBMIT_URL,
        json={"client_id": CLIENT_ID, "payload": build_payload(nonce)},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["job_id"]


def count_gpu_jobs(nonce: str) -> int:
    """GPU-side proof: count how many times a worker actually CLAIMED and therefore
    ran this job. worker.py logs 'claimed job_id=<key>' exactly once per real run
    (the line immediately precedes run_inference). Counting it across all worker
    replicas is a faithful count of GPU invocations for this job."""
    key = job_key(build_payload(nonce))
    # Fixed kubectl argv: list form, no shell.
    out = subprocess.run(  # nosec B603
        ["kubectl", "-n", NAMESPACE, "logs", "-l", "app=async-worker",
         "--tail=-1", "--prefix"],
        capture_output=True, text=True, check=True,
    ).stdout
    return sum(1 for line in out.splitlines() if f"claimed job_id={key}" in line)


def main() -> int:
    nonce = uuid.uuid4().hex
    # Fire the identical job twice, concurrently.
    with cf.ThreadPoolExecutor(max_workers=2) as ex:
        job_ids = list(ex.map(submit, [nonce, nonce]))

    assert job_ids[0] == job_ids[1], f"job_ids differ: {job_ids} (identity hash wrong)"
    job_id = job_ids[0]
    print(f"both submissions map to one job_id={job_id}")

    # Wait for completion.
    deadline = time.time() + 300
    status = None
    while time.time() < deadline:
        item = ddb.get_item(TableName=DDB_TABLE, Key={"job_id": {"S": job_id}}).get("Item")
        status = item["status"]["S"] if item else None
        if status == "complete":
            break
        # Deliberate fixed wait in a measurement or test script: poll interval until the job record is complete.
        time.sleep(3)  # nosemgrep
    assert status == "complete", f"job did not complete, status={status}"

    # GPU-side proof.
    n = count_gpu_jobs(nonce)
    assert n == 1, f"FAIL: job ran {n} times on GPU, expected 1 (dedup broken)"
    print("PASS: duplicate submission ran exactly one GPU job")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
