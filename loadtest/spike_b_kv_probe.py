# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Spike B, Phase 2: empirical KV-cache-metric semantics probe on Neuron.

Runs INSIDE the Neuron pod (localhost:8000), so it needs no port-forward. It drives three
conditions and samples vllm:kv_cache_usage_perc (and num_requests_running, num_preemptions_total)
while the requests are in flight, capturing the peak reading per condition:

  A. one short sequence            (baseline: 1 slot, little context)
  B. one long sequence             (1 slot, near-max context)      -> LENGTH discriminator vs A
  C. many short sequences (=max_num_seqs)  (several slots, little context) -> COUNT discriminator vs A

Interpretation (the discriminating test):
  - If the metric barely moves A->B (length) but jumps A->C (count), it measures occupied
    SEQUENCE SLOTS, not memory pressure (the contiguous-path semantics).
  - If it moves A->B with length, it tracks memory as on NVIDIA.

Output: a JSON blob on stdout with the peak readings; the caller commits it.

Usage (inside the pod):
  python3 spike_b_kv_probe.py <label>
"""
import concurrent.futures as cf
import json
import sys
import threading
import time
import urllib.request


def _urlopen_http(target, *args, **kwargs):
    """urllib.request.urlopen restricted to http and https URLs (bandit B310)."""
    import urllib.request
    url = getattr(target, "full_url", target)
    if not str(url).lower().startswith(("http://", "https://")):
        raise ValueError("refusing a non-HTTP URL: %r" % (url,))
    return urllib.request.urlopen(target, *args, **kwargs)  # nosec B310 - scheme checked above


BASE = "http://localhost:8000"
LABEL = sys.argv[1] if len(sys.argv) > 1 else "run"
MODEL = sys.argv[2] if len(sys.argv) > 2 else "TinyLlama/TinyLlama-1.1B-Chat-v1.0"


def read_metrics():
    with _urlopen_http(f"{BASE}/metrics", timeout=10) as _r:
        t = _r.read().decode()
    out = {}
    for line in t.splitlines():
        if line.startswith("#") or not line.startswith("vllm:"):
            continue
        name = line.split("{")[0].split(" ")[0]
        try:
            val = float(line.rsplit(" ", 1)[1])
        except (ValueError, IndexError):
            continue
        if name in ("vllm:kv_cache_usage_perc", "vllm:num_requests_running",
                    "vllm:num_requests_waiting", "vllm:num_preemptions_total"):
            out[name] = val
    return out


def completion(prompt, max_tokens):
    # BASE is http://localhost through kubectl port-forward: the request never leaves this host.
    req = urllib.request.Request(  # nosemgrep
        f"{BASE}/v1/completions",
        data=json.dumps({"model": MODEL, "prompt": prompt, "max_tokens": max_tokens,
                         "temperature": 0.0, "ignore_eos": True}).encode(),
        headers={"Content-Type": "application/json"})
    try:
        with _urlopen_http(req, timeout=180) as _r:
            return json.load(_r)
    except Exception as e:  # a single failed request must not abort the whole probe
        return {"error": str(e)}


def run_condition(name, concurrency, prompt, max_tokens, settle=8):
    """Fire `concurrency` identical requests, poll /metrics during the run, return peak readings.
    ignore_eos keeps every sequence generating the full max_tokens so slots stay occupied long
    enough to sample."""
    # let any prior condition drain
    for _ in range(settle):
        m = read_metrics()
        if m.get("vllm:num_requests_running", 0) == 0:
            break
        # Deliberate fixed wait in a measurement or test script: poll interval until no request is running.
        time.sleep(1)  # nosemgrep
    peak = {"kv": 0.0, "running": 0.0}
    stop = threading.Event()

    def poller():
        while not stop.is_set():
            try:
                m = read_metrics()
                peak["kv"] = max(peak["kv"], m.get("vllm:kv_cache_usage_perc", 0.0))
                peak["running"] = max(peak["running"], m.get("vllm:num_requests_running", 0.0))
            except Exception:
                pass
            # Deliberate fixed wait in a measurement or test script: metrics sampling interval.
            time.sleep(0.3)  # nosemgrep

    pre = read_metrics()
    pt = threading.Thread(target=poller, daemon=True)
    pt.start()
    with cf.ThreadPoolExecutor(max_workers=concurrency) as ex:
        futs = [ex.submit(completion, prompt, max_tokens) for _ in range(concurrency)]
        _ = [f.result() for f in futs]
    # Deliberate fixed wait in a measurement or test script: let the last batch finish before the next read.
    time.sleep(0.5)  # nosemgrep
    stop.set()
    pt.join(timeout=2)
    post = read_metrics()
    return {"condition": name, "concurrency": concurrency,
            "prompt_tokens_approx": len(prompt.split()), "max_tokens": max_tokens,
            "peak_kv_cache_usage_perc": round(peak["kv"], 4),
            "peak_num_requests_running": peak["running"],
            "preemptions_before": pre.get("vllm:num_preemptions_total", 0.0),
            "preemptions_after": post.get("vllm:num_preemptions_total", 0.0)}


SHORT = "Hello, how are you today?"
# Long context measured at 1622 tokens via /tokenize (90 reps). With max_tokens 256 the total
# 1878 stays under max_model_len 2048. Strong length contrast vs SHORT (about 8 tokens).
LONG = ("The quick brown fox jumps over the lazy dog near the riverbank at dawn. " * 90).strip()

idle = read_metrics()
results = {
    "label": LABEL,
    "idle": {"kv_cache_usage_perc": idle.get("vllm:kv_cache_usage_perc", 0.0),
             "num_preemptions_total": idle.get("vllm:num_preemptions_total", 0.0)},
    "conditions": [
        run_condition("A_one_short", 1, SHORT, 256),
        run_condition("B_one_long", 1, LONG, 256),
        run_condition("C_many_short", 4, SHORT, 256),
    ],
}
print("SPIKE_B_KV_JSON " + json.dumps(results))
