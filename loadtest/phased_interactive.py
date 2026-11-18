# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Phase D interactive-tier driver: send short synchronous requests through the gateway with a
correlation-id header and NO tier objective header, so they route to the interactive pool.
Concurrent with the batch async load, this proves that interactive traffic lands on the
interactive tier while batch traffic lands on the batch tier (zero cross-tier), all traced
by correlation id in the Envoy access log.

Env: GATEWAY, TAG, N (default 40), CONCURRENCY (default 8), MAX_TOKENS (default 16).
"""
import concurrent.futures as cf
import os
import requests

GATEWAY = os.environ["GATEWAY"].rstrip("/")
TAG = os.environ["TAG"]
N = int(os.environ.get("N", "40"))
CONC = int(os.environ.get("CONCURRENCY", "8"))
MAX_TOKENS = int(os.environ.get("MAX_TOKENS", "16"))
MODEL = os.environ.get("LOADTEST_MODEL", "NousResearch/Meta-Llama-3.1-8B-Instruct")


def one(i: int) -> int:
    cid = f"{TAG}-int-{i}"
    try:
        r = requests.post(
            f"{GATEWAY}/v1/chat/completions",
            json={"model": MODEL,
                  "messages": [{"role": "user", "content": f"interactive short probe {i}"}],
                  "max_tokens": MAX_TOKENS, "temperature": 0.0},
            headers={"x-correlation-id": cid},  # NO x-gateway-inference-objective -> interactive
            timeout=120,
        )
        return 1 if r.status_code == 200 else 0
    except Exception:
        return 0


def main():
    ok = 0
    with cf.ThreadPoolExecutor(max_workers=CONC) as ex:
        for r in ex.map(one, range(N)):
            ok += r
    print(f"interactive traced requests ok={ok} of {N} (tag={TAG})")


if __name__ == "__main__":
    main()
