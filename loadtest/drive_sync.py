# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Simple, robust concurrent sync driver for LT-1/LT-2/LT-4/LT-7. Off-cluster client.
Drives a seeded workload through the gateway for a fixed duration and reports
client-side TTFT percentiles + error rate. The authoritative latency for the
assertions is the SERVER-SIDE vllm:time_to_first_token_seconds histogram in
Prometheus (analyze.py ttft), which is independent of client RTT.

Env: GATEWAY, WORKLOAD, TIER (interactive|batch), ROUTER (label), CONCURRENCY,
     DURATION (s), CORPUS_N.
Prints: requests, errors, client TTFT p50/p90/p99 (ms), tokens/s (client view).
"""
import os
import sys
import time
import threading
import statistics

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import workloads

GATEWAY = os.environ["GATEWAY"].rstrip("/")
WORKLOAD = os.environ.get("WORKLOAD", "interactive-short")
TIER = os.environ.get("TIER", "interactive")
ROUTER = os.environ.get("ROUTER", "epp")
CONC = int(os.environ.get("CONCURRENCY", "40"))
DURATION = float(os.environ.get("DURATION", "180"))
CORPUS_N = int(os.environ.get("CORPUS_N", "2000"))
_gen = workloads.WORKLOADS[WORKLOAD]

_stop = threading.Event()
_ttft = []
_tokens = [0]
_reqs = [0]
_errs = [0]
_lock = threading.Lock()


def worker(wid: int):
    i = wid
    while not _stop.is_set():
        i = (i + CONC) % CORPUS_N
        body = _gen(i, stream=True)
        headers = {"content-type": "application/json"}
        if TIER == "batch":
            headers["x-gateway-inference-objective"] = "batch"
        t0 = time.time()
        first = None
        toks = 0
        try:
            r = requests.post(f"{GATEWAY}/v1/chat/completions", json=body, headers=headers,
                              stream=True, timeout=120)
            if r.status_code != 200:
                with _lock:
                    _errs[0] += 1; _reqs[0] += 1
                continue
            for line in r.iter_lines():
                if not line:
                    continue
                if first is None:
                    first = (time.time() - t0) * 1000
                toks += 1
            with _lock:
                if first is not None:
                    _ttft.append(first)
                _tokens[0] += toks
                _reqs[0] += 1
        except Exception:
            with _lock:
                _errs[0] += 1; _reqs[0] += 1


def main():
    threads = [threading.Thread(target=worker, args=(w,), daemon=True) for w in range(CONC)]
    t_start = time.time()
    for t in threads:
        t.start()
    time.sleep(DURATION)
    _stop.set()
    elapsed = time.time() - t_start
    with _lock:
        n = len(_ttft)
        p = (statistics.quantiles(_ttft, n=100) if n >= 100 else None)
        p50 = statistics.median(_ttft) if n else float("nan")
        p90 = (p[89] if p else (max(_ttft) if n else float("nan")))
        p99 = (p[98] if p else (max(_ttft) if n else float("nan")))
        print(f"router={ROUTER} workload={WORKLOAD} tier={TIER} conc={CONC} dur={elapsed:.0f}s")
        print(f"  requests={_reqs[0]} errors={_errs[0]} err_rate={(_errs[0]/max(_reqs[0],1)*100):.2f}%")
        print(f"  client_TTFT_ms p50={p50:.0f} p90={p90:.0f} p99={p99:.0f}")
        print(f"  client_tokens_per_s={_tokens[0]/elapsed:.0f}")


if __name__ == "__main__":
    main()
