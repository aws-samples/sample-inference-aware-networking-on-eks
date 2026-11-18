# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Spike B, Phase 3: does Endpoint Picker routing beat cache-blind round-robin on Neuron.

Drives a prefix-heavy workload (G groups, each sharing a long common prefix) at a Neuron backend
through one of two routing modes, and measures the three things that decide the question:

  - aggregate prefix-cache hit rate (prefix_cache_hits_total / prefix_cache_queries_total, summed
    across the replicas, delta over the run): the direct consequence of cache-locality routing.
  - client TTFT p50/p90 (streaming, time to first token): the user-visible consequence.
  - per-group co-location: after a group's warmup request, where do the rest of that group's
    requests land. Read from per-pod request-count deltas. High concentration on one pod = the
    router placed same-prefix requests together (backend-aware placement). ~50/50 = scattered.

Modes:
  --mode epp : POST to the gateway with the neuron objective header. The Neuron Endpoint Picker
               scores backends and picks the endpoint.
  --mode rr  : POST straight to the Neuron Service (kube-proxy L4, cache-blind). The round-robin
               equivalent baseline: uniform across pods, no cache awareness (it is L4, it cannot
               see the cache). Confirmed cache-blind by construction.

Per-pod metrics are read by exec-ing each Neuron pod's /metrics. Pod identity for co-location comes
from those per-pod deltas, so no per-request pod label is needed.

Usage:
  ./.venv/bin/python loadtest/spike_b_routing.py --mode epp --url http://<gw> --label pc_on_epp \
      --groups 4 --reqs 10 --out loadtest/results/spike-b/route_pc_on_epp.json
"""
import argparse
import concurrent.futures as cf
import json
import subprocess
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


MODEL = "TinyLlama/TinyLlama-1.1B-Chat-v1.0"
NS = "inference"


def neuron_pods():
    # Fixed kubectl argv: list form, no shell.
    out = subprocess.run(["kubectl", "-n", NS, "get", "pod", "-l", "app=vllm-neuron",  # nosec B603
                          "-o", "jsonpath={range .items[*]}{.metadata.name}{\"\\n\"}{end}"],
                         capture_output=True, text=True, check=True).stdout
    return [p for p in out.split("\n") if p.strip()]


# request_success_total increments once per completed request (streaming included); the e2e latency
# count is not observed for streaming on this fork, so success_total is the per-pod request counter.
COUNTERS = ("vllm:prefix_cache_hits_total", "vllm:prefix_cache_queries_total",
            "vllm:request_success_total", "vllm:time_to_first_token_seconds_sum",
            "vllm:time_to_first_token_seconds_count", "vllm:num_preemptions_total")


def pod_metrics(pod):
    """Delegates to cluster_probe so the four scrapers cannot drift. Raises on a failed
    kubectl exec instead of reporting all-zero counters."""
    return cluster_probe.pod_metrics(pod, COUNTERS)


def snapshot(pods):
    return {p: pod_metrics(p) for p in pods}


def counts(snap):
    return {p: m["vllm:request_success_total"] for p, m in snap.items()}


def stream_ttft(url, headers, prompt, max_tokens=48):
    """Send a streaming completion, return (ttft_seconds, ok)."""
    body = json.dumps({"model": MODEL, "prompt": prompt, "max_tokens": max_tokens,
                       "temperature": 0.0, "stream": True}).encode()
    req = urllib.request.Request(url + "/v1/completions", data=body,
                                 headers={"Content-Type": "application/json", **headers})
    t0 = time.time()
    ttft = None
    try:
        with _urlopen_http(req, timeout=180) as r:
            for line in r:
                if ttft is None and line.startswith(b"data:") and b"[DONE]" not in line:
                    ttft = time.time() - t0
                # keep draining to completion so the request FINISHES: request_success_total only
                # increments on a completed request, and it is the per-pod co-location counter.
            return ttft, True
    except Exception:
        return None, False


def percentile(xs, q):
    if not xs:
        return None
    s = sorted(xs)
    i = min(len(s) - 1, int(round(q * (len(s) - 1))))
    return round(s[i], 4)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["epp", "rr"], required=True)
    ap.add_argument("--url", help="gateway base URL (epp mode)")
    ap.add_argument("--pod-urls", help="rr mode: comma-separated per-pod base URLs, alternated strictly per request (genuine round-robin, cache-blind: the client picks by counter, not by cache state)")
    ap.add_argument("--label", required=True)
    ap.add_argument("--groups", type=int, default=4)
    ap.add_argument("--reqs", type=int, default=10, help="requests per group after the warmup")
    ap.add_argument("--prefix-reps", type=int, default=40, help="shared-prefix length control")
    ap.add_argument("--salt", default="s", help="per-arm token so each arm uses FRESH prefixes (no cache carryover between arms)")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    pods = neuron_pods()
    assert len(pods) >= 2, f"need >=2 neuron replicas, found {pods}"

    # Request dispatch. epp: one URL (the gateway) with the neuron header, the EPP picks the pod.
    # rr: strict client-side round-robin across the per-pod URLs, cache-blind by construction.
    if args.mode == "epp":
        assert args.url, "--url (gateway) required for epp mode"
        rr_urls = None
    else:
        assert args.pod_urls, "--pod-urls required for rr mode"
        rr_urls = [u.strip() for u in args.pod_urls.split(",") if u.strip()]
        assert len(rr_urls) >= 2, "rr needs >=2 per-pod URLs"
    _rr = {"i": 0}
    _lock = __import__("threading").Lock()

    def send(prompt):
        if args.mode == "epp":
            return stream_ttft(args.url, {"x-gateway-inference-objective": "neuron"}, prompt)
        with _lock:
            u = rr_urls[_rr["i"] % len(rr_urls)]
            _rr["i"] += 1
        return stream_ttft(u, {}, prompt)

    # Distinct long shared prefix per group; short unique suffix per request. The salt makes every
    # arm's prefixes unique so one arm's warmed cache cannot inflate the next arm's hit rate.
    base = "In the field of distributed systems on AWS, consider the following scenario carefully. "
    groups = [f"[[{args.salt}-group-{g}]] " + (base * args.prefix_reps) for g in range(args.groups)]

    start = snapshot(pods)
    ttfts = []
    per_group = []
    for g, prefix in enumerate(groups):
        # warmup: one request to populate the prefix cache on whichever pod handles it
        pre = counts(snapshot(pods))
        send(prefix + "Question 0. Answer:")
        # Deliberate fixed wait in a measurement or test script: let the warm-up request populate the prefix cache.
        time.sleep(1.5)  # nosemgrep
        # the rest of the group, at low concurrency, should co-locate if routing is cache-aware
        def one(i):
            return send(prefix + f"Question {i}. Answer:")
        with cf.ThreadPoolExecutor(max_workers=2) as ex:
            res = list(ex.map(one, range(1, args.reqs + 1)))
        ttfts += [t for t, ok in res if ok and t is not None]
        post = counts(snapshot(pods))
        deltas = {p: post[p] - pre[p] for p in pods}
        total = sum(deltas.values()) or 1
        top = max(deltas.values())
        per_group.append({"group": g, "per_pod_request_delta": {p: round(deltas[p], 1) for p in pods},
                          "concentration": round(top / total, 3)})
        # Deliberate fixed wait in a measurement or test script: space the groups so one cannot affect the next.
        time.sleep(1)  # nosemgrep

    end = snapshot(pods)
    hits = sum(end[p]["vllm:prefix_cache_hits_total"] - start[p]["vllm:prefix_cache_hits_total"] for p in pods)
    queries = sum(end[p]["vllm:prefix_cache_queries_total"] - start[p]["vllm:prefix_cache_queries_total"] for p in pods)
    reqs = sum(end[p]["vllm:request_success_total"] - start[p]["vllm:request_success_total"] for p in pods)
    preempt = sum(end[p]["vllm:num_preemptions_total"] - start[p]["vllm:num_preemptions_total"] for p in pods)
    per_pod_share = {p: round(end[p]["vllm:request_success_total"] - start[p]["vllm:request_success_total"], 1) for p in pods}

    result = {
        "label": args.label, "mode": args.mode, "groups": args.groups, "reqs_per_group": args.reqs,
        "replicas": pods,
        "requests_served": reqs,
        "prefix_cache_hits": hits, "prefix_cache_queries": queries,
        "prefix_cache_hit_rate": round(hits / queries, 4) if queries else None,
        "ttft_p50_s": percentile(ttfts, 0.50), "ttft_p90_s": percentile(ttfts, 0.90),
        "ttft_n": len(ttfts),
        "mean_colocation_concentration": round(sum(x["concentration"] for x in per_group) / len(per_group), 3),
        "per_group": per_group,
        "per_pod_request_share": per_pod_share,
        "preemptions_during_run": preempt,
    }
    with open(args.out, "w") as fh:
        json.dump(result, fh, indent=2)
    print("SPIKE_B_ROUTE " + json.dumps({k: result[k] for k in
          ("label", "mode", "requests_served", "prefix_cache_hit_rate", "ttft_p50_s", "ttft_p90_s",
           "mean_colocation_concentration", "per_pod_request_share", "preemptions_during_run")}))


if __name__ == "__main__":
    main()
