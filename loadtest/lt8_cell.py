# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
LT-8: run and measure ONE cell of the 2x2 (arm x admission), end to end, with validity gates.

A cell is one combination of router (Endpoint Picker or round-robin, selected by the worker's
PUB_ARM env before this runs) and admission (GROUPED or INTERLEAVED, selected by --mode). This
script does NOT decide the arm; run_lt8.sh sets PUB_ARM on the worker and resets the cache before
calling this. Here we: assert the replica count, snapshot per-replica cache counters, drive the
submission (loadtest/phased_batch.py), wait for drain, snapshot again, and compute the measures and
gates the report needs.

IMPORTANT (standard 4): this script pre-commits to NO interaction direction. It reports the sign it
measures. Cell labels are neutral (A/B/C/D), never "lever" or "amplifies".

Measures (per A.1.8):
  1. prefix-cache hit rate = sum(hits delta) / sum(queries delta), counters are token-level.
  2. end-to-end latency p50/p90 from durable DynamoDB records (completed_at - submitted_at). TTFT is
     NOT measurable on the async path (the worker issues a non-streaming request); reported as null.
  3. per-replica request share (co-location signal); per-group co-location for INTERLEAVED comes from
     the correlation-id trace (loadtest/build_trace.py) post-hoc and is out of scope here.
  4. effective R = requests / P, derived from run counters (NEVER back-solved from the hit rate).

Validity gates (per A.1.8), recorded pass/fail:
  5. sum(claim_count) over the cell's jobs, AND infra_rejections_total == 0 (else claim_count is an
     upper bound on GPU runs, per B.2). real_gpu_runs = sum(claim_count) - sum(infra_rejections).
  6. dead-letter queue empty; reconciliation clean (all cell jobs terminal, all complete).
  7. replica count N >= 2, asserted at START and END (a mid-cell scale event fails the gate).

Usage:
  ./.venv/bin/python loadtest/lt8_cell.py --cell A --arm epp --mode grouped \
     --selector app=vllm-llama3,tier=batch --tag lt8-A-r1 --groups 12 --n 240 \
     --submit-url http://localhost:8080/submit --dlq-url <dlq> --out loadtest/results/lt8/cell_A_r1.json
"""
import argparse
import json
import os
import subprocess
import sys
import time

import boto3

import cluster_probe   # single source for pod scraping / attribution (same directory)

NS = "inference"
DDB_TABLE = os.environ.get("DDB_TABLE", "inference-jobs")
CACHE = ("vllm:prefix_cache_hits_total", "vllm:prefix_cache_queries_total", "vllm:request_success_total")


def running_pods(selector):
    # Fixed kubectl argv from module constants: list form, no shell.
    out = subprocess.run(["kubectl", "-n", NS, "get", "pod", "-l", selector,  # nosec B603
                          "--field-selector=status.phase=Running",
                          "-o", "jsonpath={range .items[*]}{.metadata.name}{\"\\n\"}{end}"],
                         capture_output=True, text=True, check=True).stdout
    return [p for p in out.split("\n") if p.strip()]


def pod_metrics(pod):
    """Delegates to cluster_probe so the four scrapers cannot drift. Raises on a failed
    kubectl exec instead of reporting all-zero counters."""
    return cluster_probe.pod_metrics(pod, CACHE)


def snapshot(pods):
    return {p: pod_metrics(p) for p in pods}


def pct(xs, q):
    if not xs:
        return None
    s = sorted(xs)
    return round(s[min(len(s) - 1, int(round(q * (len(s) - 1))))], 3)


def scan_cell_items(ddb, tag):
    """All DynamoDB items whose job_id begins with this cell's TAG (job_id = <tag>-g<g>#<hash>)."""
    items, kwargs = [], {}
    while True:
        resp = ddb.scan(TableName=DDB_TABLE,
                        FilterExpression="begins_with(job_id, :t)",
                        ExpressionAttributeValues={":t": {"S": tag + "-g"}}, **kwargs)
        items.extend(resp.get("Items", []))
        if "LastEvaluatedKey" not in resp:
            return items
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cell", required=True, help="A|B|C|D (neutral label, no direction implied)")
    ap.add_argument("--arm", required=True, help="epp|rr (informational; the worker PUB_ARM selects the route)")
    ap.add_argument("--mode", required=True, choices=["grouped", "interleaved"])
    ap.add_argument("--selector", required=True)
    ap.add_argument("--tag", required=True)
    ap.add_argument("--groups", type=int, required=True, help="P, distinct prefixes")
    ap.add_argument("--n", type=int, required=True, help="total jobs")
    ap.add_argument("--submit-url", required=True)
    ap.add_argument("--dlq-url", default="", help="SQS DLQ url; if set, asserts depth 0")
    ap.add_argument("--prefix-reps", type=int, default=8, help="L control: shared-prefix repetitions")
    ap.add_argument("--max-tokens", type=int, default=64)
    ap.add_argument("--drain-timeout", type=int, default=1800)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    ddb = boto3.client("dynamodb")
    sqs = boto3.client("sqs")

    pods_start = running_pods(args.selector)
    n_start = len(pods_start)
    assert n_start >= 2, f"GATE FAIL N>=2 at start: found {n_start}"

    start = snapshot(pods_start)

    # Drive the submission. The driver blocks in GROUPED mode (drains each group); in INTERLEAVED it
    # returns after enqueue, so we then wait for all cell jobs to reach a terminal state.
    env = dict(os.environ)
    env.update({"SUBMIT_URL": args.submit_url, "TAG": args.tag, "N": str(args.n),
                "GROUPS": str(args.groups), "MODE": args.mode, "MAX_TOKENS": str(args.max_tokens),
                "PREFIX_REPS": str(args.prefix_reps), "SALT": args.tag,  # unique prefixes per cell = cold cache
                "DDB_TABLE": DDB_TABLE, "DRAIN_TIMEOUT": str(args.drain_timeout)})
    here = os.path.dirname(__file__)
    # Runs this interpreter on a fixed sibling script: list form, no shell, no external input.
    subprocess.run([sys.executable, os.path.join(here, "phased_batch.py")], env=env, check=True)  # nosec B603  # nosemgrep

    if args.mode == "interleaved":
        deadline = time.time() + args.drain_timeout
        while time.time() < deadline:
            items = scan_cell_items(ddb, args.tag)
            if len(items) >= args.n and all(i.get("status", {}).get("S") in ("complete", "failed") for i in items):
                break
            # Deliberate fixed wait in a measurement or test script: poll interval until every job record is complete or failed.
            time.sleep(5)  # nosemgrep

    pods_end = running_pods(args.selector)
    n_end = len(pods_end)
    end = snapshot(pods_end)

    # Metric deltas over the common pods (a pod that appeared or vanished mid-cell trips the N gate).
    common = [p for p in pods_start if p in end]
    hits = sum(end[p]["vllm:prefix_cache_hits_total"] - start[p]["vllm:prefix_cache_hits_total"] for p in common)
    queries = sum(end[p]["vllm:prefix_cache_queries_total"] - start[p]["vllm:prefix_cache_queries_total"] for p in common)
    share = {p: round(end[p]["vllm:request_success_total"] - start[p]["vllm:request_success_total"], 1) for p in common}
    tot_share = sum(share.values()) or 1
    requests = sum(share.values())

    items = scan_cell_items(ddb, args.tag)
    claim_sum = sum(int(i.get("claim_count", {}).get("N", "0")) for i in items)
    infra_sum = sum(int(i.get("infra_rejections", {}).get("N", "0")) for i in items)
    statuses = {}
    for i in items:
        s = i.get("status", {}).get("S", "absent")
        statuses[s] = statuses.get(s, 0) + 1
    # Latency split (LT-10): the records store submitted_at and completed_at as strings (S) and
    # claimed_at as a number (N). Decompose end-to-end into queue wait (submitted -> claimed) and
    # inference interval (claimed -> completed). submitted_at requires a submit-api image that stamps
    # it; if empty (a stale deploy), queue_wait and e2e are simply empty and only the inference
    # interval is reported.
    def _epoch(v):
        if not v:
            return None
        x = v.get("N") or v.get("S")
        try:
            return int(x)
        except (TypeError, ValueError):
            return None
    qwait, infer, lat = [], [], []
    for i in items:
        s = _epoch(i.get("submitted_at")); c = _epoch(i.get("claimed_at")); done = _epoch(i.get("completed_at"))
        if s and c and c >= s:
            qwait.append(c - s)
        if c and done and done >= c:
            infer.append(done - c)
        if s and done and done >= s:
            lat.append(done - s)

    dlq_depth = None
    if args.dlq_url:
        a = sqs.get_queue_attributes(QueueUrl=args.dlq_url, AttributeNames=["ApproximateNumberOfMessages"])
        dlq_depth = int(a["Attributes"]["ApproximateNumberOfMessages"])

    # Effective R from run counters (NON-circular: requests / P, never from the hit rate).
    eff_R = round(requests / args.groups, 2) if args.groups else None

    gates = {
        "n_start": n_start, "n_end": n_end,
        "gate_replicas_ge2_stable": bool(n_start >= 2 and n_end >= 2 and n_start == n_end),
        "claim_count_sum": claim_sum, "infra_rejections_sum": infra_sum,
        "real_gpu_runs": claim_sum - infra_sum,
        "gate_infra_rejections_zero": bool(infra_sum == 0),
        "dlq_depth": dlq_depth, "gate_dlq_empty": (dlq_depth == 0) if dlq_depth is not None else None,
        "statuses": statuses,
        "gate_reconciliation_clean": bool(statuses.get("complete", 0) == args.n and statuses.get("failed", 0) == 0),
    }

    result = {
        "cell": args.cell, "arm": args.arm, "mode": args.mode, "tag": args.tag,
        "P_distinct_prefixes": args.groups, "N_total_jobs": args.n, "prefix_reps_L": args.prefix_reps,
        "replicas": n_start, "replica_pods": common,
        "prefix_cache_hits": hits, "prefix_cache_queries": queries,
        "prefix_cache_hit_rate": round(hits / queries, 4) if queries else None,
        "requests_counted": requests, "effective_R": eff_R,
        "per_replica_request_share": share,
        "per_replica_share_fraction": {p: round(share[p] / tot_share, 3) for p in common},
        "e2e_latency_p50_s": pct(lat, 0.50), "e2e_latency_p90_s": pct(lat, 0.90), "latency_n": len(lat),
        "queue_wait_p50_s": pct(qwait, 0.50), "queue_wait_p90_s": pct(qwait, 0.90), "queue_wait_n": len(qwait),
        "inference_p50_s": pct(infer, 0.50), "inference_p90_s": pct(infer, 0.90), "inference_n": len(infer),
        "ttft_note": "not measurable on the async path (worker issues a non-streaming request)",
        "gates": gates,
    }
    with open(args.out, "w") as fh:
        json.dump(result, fh, indent=2)
    print("LT8_CELL " + json.dumps({k: result[k] for k in
          ("cell", "arm", "mode", "prefix_cache_hit_rate", "effective_R", "e2e_latency_p50_s",
           "e2e_latency_p90_s", "per_replica_share_fraction")}))
    print("LT8_GATES " + json.dumps(gates))


if __name__ == "__main__":
    main()
