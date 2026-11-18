# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Locust user for the ASYNCHRONOUS path (LT-1 batch, LT-3 idempotency, LT-6 durability).

Submits jobs to the Submit API and (optionally) polls DynamoDB for completion. For
the idempotency scenario each distinct job is submitted D times concurrently to force
duplicate delivery. Correctness is asserted OUT OF BAND by analyze.py against
worker_claims_total and the DynamoDB complete count, not here; this user only drives load.

Env:
  SUBMIT_URL   e.g. http://<submit-api>/submit   (required)
  N_JOBS       number of DISTINCT jobs in the run (default 5000)
  DUP          duplicate submissions per distinct job (default 3)
  RUN_TAG      label written into client_id so a run's jobs are isolatable
"""
import os
import time

from locust import User, task, constant, events

import workloads

SUBMIT_URL = os.environ.get("SUBMIT_URL", "")
N_JOBS = int(os.environ.get("N_JOBS", "5000"))
DUP = int(os.environ.get("DUP", "3"))
RUN_TAG = os.environ.get("RUN_TAG", "run")

import requests

# A module-level cursor so the fleet of users collectively submits distinct jobs
# 0..N_JOBS-1, each DUP times. Locust runs users concurrently, which supplies the
# concurrency that forces duplicate delivery.
_state = {"next": 0}


def _next_index():
    i = _state["next"]
    _state["next"] = i + 1
    return i // DUP  # DUP consecutive submissions map to the same distinct job


class AsyncSubmitUser(User):
    wait_time = constant(0)

    @task
    def submit(self):
        idx = _next_index()
        if idx >= N_JOBS:
            self.environment.runner.quit()
            return
        job = workloads.async_job(idx)
        job["client_id"] = f"{RUN_TAG}"           # stable so duplicates hash identically
        t0 = time.time()
        try:
            r = requests.post(SUBMIT_URL, json=job, timeout=30)
            ok = r.status_code == 202
            events.request.fire(
                request_type="SUBMIT", name=f"submit|{RUN_TAG}",
                response_time=(time.time() - t0) * 1000, response_length=0,
                exception=None if ok else f"http {r.status_code}",
            )
        except Exception as e:  # noqa: BLE001
            events.request.fire(request_type="SUBMIT", name=f"submit|{RUN_TAG}",
                                response_time=(time.time() - t0) * 1000,
                                response_length=0, exception=str(e))
