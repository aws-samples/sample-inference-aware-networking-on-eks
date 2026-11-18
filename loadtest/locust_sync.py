# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Locust user for the SYNCHRONOUS path (LT-1, LT-2, LT-4, LT-7).

Drives a chosen workload through the gateway and measures time-to-first-token
(TTFT) from the streamed response, plus end-to-end latency. Reports both as named
Locust request events so percentiles come out of Locust's own stats.

Env:
  GATEWAY   e.g. http://<elb-hostname>   (required)
  WORKLOAD  interactive-short | prefix-heavy | reasoning-long  (default interactive-short)
  TIER      interactive | batch  (default interactive; batch adds the objective header)
  CORPUS_N  size of the fixed request set to cycle through (default 2000)
  ROUTER    free label recorded in results (epp | round-robin), does not change traffic

Run (distributed): see loadtest/README.md and the make loadtest-* targets.
"""
import os
import time

from locust import HttpUser, task, between, events

import workloads

GATEWAY = os.environ.get("GATEWAY", "")
WORKLOAD = os.environ.get("WORKLOAD", "interactive-short")
TIER = os.environ.get("TIER", "interactive")
CORPUS_N = int(os.environ.get("CORPUS_N", "2000"))
ROUTER = os.environ.get("ROUTER", "epp")
_gen = workloads.WORKLOADS[WORKLOAD]


def _record(name, start, exception=None, length=0):
    events.request.fire(
        request_type="STREAM", name=name, response_time=(time.time() - start) * 1000,
        response_length=length, exception=exception,
    )


class SyncUser(HttpUser):
    wait_time = between(0.0, 0.05)
    host = GATEWAY

    @task
    def generate(self):
        i = self.environment.runner.user_count  # cheap spread; corpus indexed below
        idx = (id(self) + i) % CORPUS_N
        body = _gen(idx, stream=True)
        headers = {"content-type": "application/json"}
        if TIER == "batch":
            headers["x-gateway-inference-objective"] = "batch"
        t0 = time.time()
        first = None
        tokens = 0
        try:
            with self.client.post("/v1/chat/completions", json=body, headers=headers,
                                  stream=True, catch_response=True,
                                  name=f"{WORKLOAD}|{TIER}|{ROUTER}") as r:
                if r.status_code != 200:
                    r.failure(f"http {r.status_code}")
                    _record(f"TTFT|{WORKLOAD}|{TIER}|{ROUTER}", t0, exception=f"http {r.status_code}")
                    return
                for line in r.iter_lines():
                    if not line:
                        continue
                    if first is None:
                        first = time.time()
                        _record(f"TTFT|{WORKLOAD}|{TIER}|{ROUTER}", t0)
                    tokens += 1
                r.success()
        except Exception as e:  # noqa: BLE001
            _record(f"TTFT|{WORKLOAD}|{TIER}|{ROUTER}", t0, exception=str(e))
            return
        _record(f"E2E|{WORKLOAD}|{TIER}|{ROUTER}", t0, length=tokens)
