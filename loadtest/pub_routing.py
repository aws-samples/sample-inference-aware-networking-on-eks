# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Publication measurement driver: one arm (routing on or off) through the gateway, matched conditions.

Both arms enter through the gateway and differ only in the x-pub-arm header (epp routes to the
InferencePool and its Endpoint Picker; rr routes to the plain Service, cache-blind Envoy load
balancing), so the network path is equalized and the only variable is the scoring (Phase 2.5).

Two measurement stages per run, both prefix-heavy:
  1. Co-location probe (primary evidence, Phase 3.2): G groups, a warmup then a burst per group,
     per-replica request-count deltas give the concentration (1.0 = the whole group on one replica).
  2. Sustained steady-state load for --steady seconds at fixed concurrency: client TTFT p50/p90,
     inter-token latency, and throughput; plus start/end per-replica metric deltas for the aggregate
     prefix-cache hit rate and the per-replica request distribution.

Per-replica metrics are read by exec-ing each replica's /metrics (pod identity for co-location comes
from those deltas; streaming through the gateway does not expose the backend pod per request, a
stated limitation). Utilization is read from Prometheus separately, not here.

Usage:
  ./.venv/bin/python loadtest/pub_routing.py --label pc_on_epp --arm epp \
     --selector app=vllm-llama3,tier=interactive --gateway http://<gw> \
     --steady 360 --conc 8 --groups 6 --burst 12 --out loadtest/results/publication/<file>.json
"""
import argparse
import concurrent.futures as cf
import json
import subprocess
import threading
import time
import urllib.request

import cluster_probe   # single source for pod scraping / attribution (same directory)


def _urlopen_http(target, *args, **kwargs):
    """urllib.request.urlopen restricted to http and https URLs (bandit B310)."""
    import urllib.request
    url = getattr(target, "full_url", target)
    if not str(url).lower().startswith(("http://", "https://")):
        raise ValueError("refusing a non-HTTP URL: %r" % (url,))
    return urllib.request.urlopen(target, *args, **kwargs)  # nosec B310 - scheme checked above


# NO HARDCODED MODEL ID, 2026-09-24. This read "NousResearch/Meta-Llama-3.1-8B-Instruct" as a
# module constant, which was correct for the Llama publication arms and silently wrong for every
# Qwen3 run: vLLM answers an unknown model id with a 404, and a 404 on every request reads as a
# broken route rather than a stale test. The model is now either passed explicitly (--model) or
# read LIVE from the Deployment's own --model arg via cluster_probe.served_model, which is the same
# cure already applied to the diagnosis script. It is recorded in the result JSON either way.
NS = "inference"
COUNTERS = ("vllm:prefix_cache_hits_total", "vllm:prefix_cache_queries_total",
            "vllm:request_success_total", "vllm:generation_tokens_total",
            "vllm:num_preemptions_total")


def pods(selector):
    """Delegates to cluster_probe so the copies in lt8_direct.py and lt8_cell.py stay
    identical to this one."""
    return cluster_probe.running_pods(selector)


def pod_metrics(pod):
    """Delegates to cluster_probe so the four scrapers cannot drift. Raises on a failed
    kubectl exec instead of reporting all-zero counters."""
    return cluster_probe.pod_metrics(pod, COUNTERS)


def snapshot(ps):
    return {p: pod_metrics(p) for p in ps}


def success_counts(snap):
    return {p: m["vllm:request_success_total"] for p, m in snap.items()}


def stream(url, headers, prompt, max_tokens, model, seed):
    """Streaming request. Returns (ttft_s, total_s, n_chunks, ok).

    SEED, added 2026-09-24 on the maintainer's ruling: the Llama publication arms recorded none, so an
    arm could not be reproduced exactly. Decoding here is already greedy (temperature 0.0), so the
    seed changes no output today; it is carried so that every arm states its sampling state in the
    request and in the result rather than leaving it to a default.
    """
    body = json.dumps({"model": model, "prompt": prompt, "max_tokens": max_tokens,
                       "temperature": 0.0, "seed": seed, "ignore_eos": True, "stream": True}).encode()
    req = urllib.request.Request(url + "/v1/completions", data=body,
                                 headers={"Content-Type": "application/json", **headers})
    t0 = time.time()
    ttft = None
    n = 0
    try:
        with _urlopen_http(req, timeout=180) as r:
            for line in r:
                if line.startswith(b"data:") and b"[DONE]" not in line:
                    if ttft is None:
                        ttft = time.time() - t0
                    n += 1
            return ttft, time.time() - t0, n, True
    except Exception:
        return None, None, 0, False


def pct(xs, q):
    if not xs:
        return None
    s = sorted(xs)
    return round(s[min(len(s) - 1, int(round(q * (len(s) - 1))))], 4)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", required=True)
    ap.add_argument("--arm", required=True, help="x-pub-arm header value (epp|rr|neuron-epp|neuron-rr)")
    ap.add_argument("--selector", required=True)
    ap.add_argument("--gateway", required=True)
    ap.add_argument("--steady", type=int, default=360)
    ap.add_argument("--conc", type=int, default=8)
    ap.add_argument("--groups", type=int, default=6, help="distinct prefixes in the sustained working set")
    ap.add_argument("--coloc-groups", type=int, default=6, help="groups used for the co-location probe (subset, kept small so the probe is fast at large working sets)")
    ap.add_argument("--burst", type=int, default=12)
    ap.add_argument("--prefix-reps", type=int, default=60)
    ap.add_argument("--salt", default="p")
    ap.add_argument("--model", default=None,
                    help="served model id. Omit to read it live from --model-deployment.")
    ap.add_argument("--model-deployment", default=None,
                    help="Deployment whose --model arg is read when --model is omitted. Needs "
                         "kubectl, so the in-cluster driver passes --model explicitly instead.")
    ap.add_argument("--seed", type=int, required=True,
                    help="sampling seed, recorded per arm. Required: an unrecorded seed is what "
                         "made the Llama arms unreproducible.")
    ap.add_argument("--max-tokens", type=int, default=64)
    ap.add_argument("--churn-reuse", type=int, default=0, help="if >0, prefix churn mode: each distinct prefix is used exactly this many times consecutively then never again (new prefixes arrive continuously). R equals this value by construction, the cold-start gap is permanent, and the active set stays small so cache never overflows.")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    headers = {"x-pub-arm": args.arm}
    if args.model:
        model = args.model
    elif args.model_deployment:
        model = cluster_probe.served_model(args.model_deployment)
    else:
        ap.error("pass --model, or --model-deployment to read it live. There is no default: a "
                 "default model id is the defect this argument replaced.")
    assert model, "resolved an empty model id"
    ps = pods(args.selector)
    assert len(ps) >= 4, f"need >=4 replicas, found {len(ps)}: {ps}"
    base = "In the field of distributed systems on AWS, consider the following scenario in depth. "
    groups = [f"[[{args.salt}-g{g}]] " + (base * args.prefix_reps) for g in range(args.groups)]

    def send(prompt):
        return stream(args.gateway, headers, prompt, args.max_tokens, model, args.seed)

    # Stage 1: co-location probe over a small subset of groups (fast even when the sustained working
    # set is large). Sustained load (stage 2) still cycles all groups.
    per_group = []
    for g, prefix in enumerate(groups[:args.coloc_groups]):
        pre = success_counts(snapshot(ps))
        send(prefix + "Q0. Answer:")
        # Deliberate fixed wait in a measurement or test script: let the warm-up request populate the prefix cache.
        time.sleep(1.2)  # nosemgrep
        with cf.ThreadPoolExecutor(max_workers=4) as ex:
            list(ex.map(lambda i: send(prefix + f"Q{i}. Answer:"), range(1, args.burst + 1)))
        post = success_counts(snapshot(ps))
        d = {p: post[p] - pre[p] for p in ps}
        tot = sum(d.values()) or 1
        per_group.append({"group": g, "per_pod": {p: round(d[p], 1) for p in ps},
                          "concentration": round(max(d.values()) / tot, 3)})
        # Deliberate fixed wait in a measurement or test script: space the groups so one cannot affect the next.
        time.sleep(0.5)  # nosemgrep

    # Stage 2: sustained steady-state load.
    start = snapshot(ps)
    t_start = time.time()
    ttfts, itls, ok_n, fail_n, gen_chunks = [], [], 0, 0, 0
    stop = threading.Event()
    lock = threading.Lock()
    gi = {"i": 0}

    churn_cache = {}

    def churn_prefix(g):
        if g not in churn_cache:
            churn_cache[g] = f"[[{args.salt}-churn-{g}]] " + (base * args.prefix_reps)
        return churn_cache[g]

    def worker():
        nonlocal ok_n, fail_n, gen_chunks
        while not stop.is_set():
            with lock:
                if args.churn_reuse > 0:
                    # each distinct prefix used churn_reuse times consecutively, then a NEW prefix
                    g = gi["i"] // args.churn_reuse
                    prefix = churn_prefix(g)
                else:
                    g = gi["i"] % len(groups)
                    prefix = groups[g]
                gi["i"] += 1
            ttft, total, n, ok = send(prefix + f"Sustained {g}. Answer:")
            with lock:
                if ok and ttft is not None:
                    ttfts.append(ttft)
                    if n > 1 and total is not None:
                        itls.append((total - ttft) / (n - 1))
                    gen_chunks += n
                    ok_n += 1
                else:
                    fail_n += 1

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(args.conc)]
    for t in threads:
        t.start()
    time.sleep(args.steady)
    stop.set()
    for t in threads:
        t.join(timeout=5)
    dur = time.time() - t_start
    end = snapshot(ps)

    hits = sum(end[p]["vllm:prefix_cache_hits_total"] - start[p]["vllm:prefix_cache_hits_total"] for p in ps)
    queries = sum(end[p]["vllm:prefix_cache_queries_total"] - start[p]["vllm:prefix_cache_queries_total"] for p in ps)
    gen_tokens = sum(end[p]["vllm:generation_tokens_total"] - start[p]["vllm:generation_tokens_total"] for p in ps)
    preempt = sum(end[p]["vllm:num_preemptions_total"] - start[p]["vllm:num_preemptions_total"] for p in ps)
    share = {p: round(end[p]["vllm:request_success_total"] - start[p]["vllm:request_success_total"], 1) for p in ps}
    tot_share = sum(share.values()) or 1

    result = {
        "label": args.label, "arm": args.arm, "model": model, "seed": args.seed,
        "replicas": len(ps), "replica_pods": ps,
        "steady_seconds": round(dur, 1), "concurrency": args.conc,
        "requests_ok": ok_n, "requests_failed": fail_n,
        "prefix_cache_hits": hits, "prefix_cache_queries": queries,
        "prefix_cache_hit_rate": round(hits / queries, 4) if queries else None,
        "ttft_p50_s": pct(ttfts, 0.50), "ttft_p90_s": pct(ttfts, 0.90), "ttft_n": len(ttfts),
        "inter_token_latency_p50_s": pct(itls, 0.50), "inter_token_latency_p90_s": pct(itls, 0.90),
        "throughput_gen_tokens_per_s": round(gen_tokens / dur, 1) if dur else None,
        "mean_colocation_concentration": round(sum(x["concentration"] for x in per_group) / len(per_group), 3),
        "per_group_colocation": per_group,
        "per_replica_request_share": share,
        "per_replica_share_fraction": {p: round(share[p] / tot_share, 3) for p in ps},
        "preemptions_during_run": preempt,
    }
    with open(args.out, "w") as fh:
        json.dump(result, fh, indent=2)
    print("PUB_ROUTE " + json.dumps({k: result[k] for k in
          ("label", "arm", "model", "seed", "replicas", "steady_seconds", "requests_ok", "prefix_cache_hit_rate",
           "ttft_p50_s", "ttft_p90_s", "inter_token_latency_p50_s", "throughput_gen_tokens_per_s",
           "mean_colocation_concentration", "per_replica_share_fraction", "preemptions_during_run")}))


if __name__ == "__main__":
    main()
