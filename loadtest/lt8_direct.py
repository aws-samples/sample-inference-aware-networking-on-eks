# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
LT-8 direct driver: run and measure ONE cell of the 2x2 by driving the gateway directly.

Why direct (not through the async worker): the substitutes-vs-complements question is about admission
COMPOSITION (what set of prefixes is in flight at once), which this driver controls precisely by the
order it sends requests. Both arms enter the SAME gateway and differ only in the x-pub-arm header
(epp -> InferencePool + Endpoint Picker; rr -> plain Service, cache-blind), exactly as the publication
arms did (pub_routing.py). The async worker exercises the queue as the operational control surface and
is validated separately (LT-9); it showed intermittent latency under sustained load, so it is not used
to DRIVE this measurement. Uses urllib (proven reliable here), never pre-commits a direction.

Admission:
  interleaved: all P prefixes' requests issued mixed together (the whole set in flight).
  grouped:     one prefix's R requests at a time; the next prefix starts only after the current
               prefix's requests all return (instantaneous in-flight set = one prefix).

Measures (batch tier vLLM counters, token-level): aggregate prefix-cache hit rate, per-replica request
share (co-location), effective R = requests / P (from counters, never back-solved from the hit rate).

Usage:
  ./.venv/bin/python loadtest/lt8_direct.py --cell A --arm epp --mode grouped \
    --selector app=vllm-llama3,tier=batch --gateway http://<gw> --groups 52 --reqs 12 \
    --prefix-reps 214 --salt lt8-A-r1 --out loadtest/results/lt8/cell_A_r1.json
"""
import argparse, json, os, subprocess, time, urllib.request
import concurrent.futures as cf

import cluster_probe   # single source for pod scraping / attribution (same directory)


def _urlopen_http(target, *args, **kwargs):
    """urllib.request.urlopen restricted to http and https URLs (bandit B310)."""
    import urllib.request
    url = getattr(target, "full_url", target)
    if not str(url).lower().startswith(("http://", "https://")):
        raise ValueError("refusing a non-HTTP URL: %r" % (url,))
    return urllib.request.urlopen(target, *args, **kwargs)  # nosec B310 - scheme checked above


MODEL = os.environ.get("LOADTEST_MODEL", "NousResearch/Meta-Llama-3.1-8B-Instruct")
NS = "inference"
COUNTERS = ("vllm:prefix_cache_hits_total", "vllm:prefix_cache_queries_total", "vllm:request_success_total")
# Below this success rate the cell is not a measurement. Overridable for a deliberate
# fault-injection run, where a low success rate is the point.
MIN_OK_RATE = float(os.environ.get("LT8_MIN_OK_RATE", "0.95"))


def pods(selector):
    """Delegates to cluster_probe so the copies in lt8_cell.py and pub_routing.py stay
    identical to this one."""
    return cluster_probe.running_pods(selector)


def pod_metrics(pod):
    """Delegates to cluster_probe so the four scrapers cannot drift. Raises on a failed
    kubectl exec instead of reporting all-zero counters."""
    return cluster_probe.pod_metrics(pod, COUNTERS)


def snapshot(ps):
    return {p: pod_metrics(p) for p in ps}


def send(gw, arm, prompt, max_tokens):
    body = json.dumps({"model": MODEL, "messages": [{"role": "user", "content": prompt}],
                       "max_tokens": max_tokens, "temperature": 0.0}).encode()
    req = urllib.request.Request(gw + "/v1/chat/completions", data=body,
        headers={"Content-Type": "application/json", "x-gateway-inference-objective": "batch",
                 **({"x-pub-arm": arm} if arm == "rr" else {})})
    t = time.time()
    try:
        with _urlopen_http(req, timeout=120) as _r:
            _r.read()
        return time.time() - t, True
    except Exception:
        return time.time() - t, False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cell", required=True)
    ap.add_argument("--arm", required=True, choices=["epp", "rr"])
    ap.add_argument("--mode", required=True, choices=["grouped", "interleaved"])
    ap.add_argument("--selector", required=True)
    ap.add_argument("--gateway", required=True)
    ap.add_argument("--groups", type=int, required=True, help="P distinct prefixes")
    ap.add_argument("--reqs", type=int, required=True, help="R requests per prefix")
    ap.add_argument("--prefix-reps", type=int, default=214)
    ap.add_argument("--salt", required=True, help="unique per cell => cold cache without restart")
    ap.add_argument("--max-tokens", type=int, default=64)
    ap.add_argument("--conc", type=int, default=16)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    base = ("The following is a long standing instruction set that all requests in this group share "
            "verbatim so that the prefix cache is populated once and reused. ")
    prefixes = [f"SharedContext{args.salt}Group{g}: " + base * args.prefix_reps for g in range(args.groups)]

    ps = pods(args.selector)
    assert len(ps) >= 2, f"need >=2 replicas, found {len(ps)}"
    n_start = len(ps)
    start = snapshot(ps)
    lat = []
    ok = 0

    def one(g, i):
        return send(args.gateway, args.arm, prefixes[g] + f" Distinct question {i}. Answer:", args.max_tokens)

    if args.mode == "interleaved":
        tasks = [(g, i) for i in range(args.reqs) for g in range(args.groups)]
        with cf.ThreadPoolExecutor(max_workers=args.conc) as ex:
            for dt, good in ex.map(lambda t: one(*t), tasks):
                lat.append(dt); ok += good
    else:  # grouped: one prefix's R requests at a time, drained before the next
        for g in range(args.groups):
            with cf.ThreadPoolExecutor(max_workers=args.conc) as ex:
                for dt, good in ex.map(lambda i: one(g, i), range(args.reqs)):
                    lat.append(dt); ok += good

    # Deliberate fixed wait in a measurement or test script: let the Prometheus counters settle before the final read.
    time.sleep(8)  # nosemgrep
    end = snapshot(ps)
    ps_end = pods(args.selector)
    n_end = len(ps_end)

    hits = sum(end[p]["vllm:prefix_cache_hits_total"] - start[p]["vllm:prefix_cache_hits_total"] for p in ps)
    queries = sum(end[p]["vllm:prefix_cache_queries_total"] - start[p]["vllm:prefix_cache_queries_total"] for p in ps)
    share = {p: round(end[p]["vllm:request_success_total"] - start[p]["vllm:request_success_total"], 1) for p in ps}
    tot = sum(share.values()) or 1
    requests_counted = sum(share.values())

    def pct(xs, q):
        s = sorted(xs); return round(s[min(len(s) - 1, int(round(q * (len(s) - 1))))], 3) if s else None

    result = {
        "cell": args.cell, "arm": args.arm, "mode": args.mode, "salt": args.salt,
        "P": args.groups, "R_requested": args.reqs, "prefix_reps": args.prefix_reps,
        "replicas": n_start, "n_start": n_start, "n_end": n_end,
        "requests_sent": len(lat), "requests_ok": ok,
        "prefix_cache_hits": hits, "prefix_cache_queries": queries,
        "prefix_cache_hit_rate": round(hits / queries, 4) if queries else None,
        "requests_counted": requests_counted,
        "effective_R": round(requests_counted / args.groups, 2) if args.groups else None,
        "per_replica_share": share,
        "per_replica_share_fraction": {p: round(share[p] / tot, 3) for p in ps},
        "e2e_latency_p50_s": pct(lat, 0.50), "e2e_latency_p90_s": pct(lat, 0.90),
        "max_concentration_fraction": round(max(share.values()) / tot, 3) if share else None,
    }

    # Integrity gate. These fields were recorded before but never checked, so a cell could
    # publish a hit rate after a mid-run pod restart (vLLM counters reset, giving a negative
    # delta) or with most requests failing. The run scripts then averaged such cells into the
    # reported (A-B) vs (C-D) interaction. The result file is still written, so the evidence
    # survives, but the exit code is non-zero so a caller cannot fold a bad cell into an average.
    ok_rate = (ok / len(lat)) if lat else 0.0
    problems = []
    if n_end != n_start:
        problems.append(
            f"replica count changed mid-run ({n_start} -> {n_end}); vLLM counters reset on "
            "restart, so the deltas below may be meaningless")
    if hits < 0 or queries < 0:
        problems.append(
            f"negative counter delta (hits={hits}, queries={queries}); a pod restarted and "
            "reset its counters")
    if ok_rate < MIN_OK_RATE:
        problems.append(
            f"only {ok}/{len(lat)} requests succeeded ({ok_rate:.1%} < {MIN_OK_RATE:.0%}); "
            "a hit rate over mostly-failed requests is not a measurement")
    if queries == 0:
        problems.append("zero prefix-cache queries recorded; nothing was measured")
    result["integrity"] = "ok" if not problems else "FAILED"
    result["integrity_problems"] = problems

    with open(args.out, "w") as fh:
        json.dump(result, fh, indent=2)
    print("LT8 " + json.dumps({k: result[k] for k in
        ("cell", "arm", "mode", "prefix_cache_hit_rate", "effective_R", "requests_ok",
         "max_concentration_fraction", "e2e_latency_p50_s", "e2e_latency_p90_s", "integrity")}))
    if problems:
        for p in problems:
            print(f"INTEGRITY FAIL {args.cell}: {p}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())   # non-zero when the integrity gate fails
