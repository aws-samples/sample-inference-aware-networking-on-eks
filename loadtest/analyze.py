# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Analysis and pass/fail assertions for the load-test suite (Phase 4).

Reads REAL measurements from Prometheus and DynamoDB and prints a verdict per
scenario. It never invents a number: if a query returns nothing it says so and the
scenario is inconclusive, not a pass.

Prometheus is reached at PROM_URL (default http://localhost:9090; port-forward
kps-kube-prometheus-stack-prometheus). AWS creds via the environment.

CLI:
  analyze.py lt3 --run <tag> --n <N> --start <epoch> --end <epoch>
  analyze.py ttft --tier interactive --start <epoch> --end <epoch> [--quantile 0.95]
  analyze.py prefix-hit --tier interactive --start <epoch> --end <epoch>
  analyze.py claims-delta --start <epoch> --end <epoch>
"""
import argparse
import os
import sys

import boto3
import requests

PROM = os.environ.get("PROM_URL", "http://localhost:9090")
TABLE = os.environ.get("DDB_TABLE", "inference-jobs")


def q_instant(expr: str, at: float | None = None):
    params = {"query": expr}
    if at:
        params["time"] = at
    r = requests.get(f"{PROM}/api/v1/query", params=params, timeout=30)
    r.raise_for_status()
    res = r.json()["data"]["result"]
    return res


def scalar(expr: str, at: float | None = None):
    res = q_instant(expr, at)
    if not res:
        return None
    return float(res[0]["value"][1])


def claims_delta(start: float, end: float):
    """Actual GPU runs during the window = sum(worker_claims_total) at end minus start."""
    a = scalar("sum(worker_claims_total)", start)
    b = scalar("sum(worker_claims_total)", end)
    if a is None or b is None:
        return None
    return b - a


def ddb_complete_count(run_tag: str):
    """Distinct complete jobs for this run. Requires dynamodb:Scan on the analysis role
    (the worker's own role is intentionally Scan-less; use a separate read role)."""
    ddb = boto3.client("dynamodb")
    n = 0
    kwargs = dict(TableName=TABLE,
                  FilterExpression="begins_with(job_id, :p) AND #s = :c",
                  ExpressionAttributeNames={"#s": "status"},
                  ExpressionAttributeValues={":p": {"S": f"{run_tag}#"}, ":c": {"S": "complete"}})
    while True:
        page = ddb.scan(**kwargs)
        n += page["Count"]
        if "LastEvaluatedKey" not in page:
            break
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    return n


def ddb_run_stats(run_tag: str):
    """Durable LT-6 counts from DynamoDB (survive worker-pod death, unlike the per-pod
    Prometheus counter). Returns (complete, total_claims, rerun_jobs, swept_jobs): distinct
    jobs at status complete, sum of claim_count over the run's jobs (= total real GPU runs),
    how many jobs were claimed more than once, and how many were recovered by the orphan
    sweeper (DESIGN.md 3.4). The sweeper is the ONLY component that writes requeued_at, so an
    item carrying that attribute was re-driven by a sweep (message-less orphan), as opposed
    to an interrupted job that SQS redelivery recovered on its own. Needs dynamodb:Scan."""
    ddb = boto3.client("dynamodb")
    complete = total_claims = rerun_jobs = swept_jobs = 0
    kwargs = dict(TableName=TABLE, FilterExpression="begins_with(job_id, :p)",
                  ExpressionAttributeValues={":p": {"S": f"{run_tag}#"}})
    while True:
        page = ddb.scan(**kwargs)
        for it in page.get("Items", []):
            if it.get("status", {}).get("S") == "complete":
                complete += 1
            cc = int(it.get("claim_count", {}).get("N", "0"))
            total_claims += cc
            if cc > 1:
                rerun_jobs += 1
            if "requeued_at" in it:
                swept_jobs += 1
        if "LastEvaluatedKey" not in page:
            break
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    return complete, total_claims, rerun_jobs, swept_jobs


def lt6d(args):
    """LT-6 durability verdict from DURABLE DynamoDB state. All N distinct jobs must
    reach complete, and GPU runs beyond N (extra claims) must be bounded by the number
    of interrupted jobs. Submissions are D=1 here, so any extra claim is necessarily a
    re-claim of an interrupted job, not a duplicate."""
    complete, claims, reruns, swept = ddb_run_stats(args.run)
    N = args.n
    extra = claims - N
    sqs_recovered = reruns - swept   # interrupted jobs SQS redelivery recovered on its own
    print(f"LT-6 durability (run={args.run}, N={N}, D=1)")
    print(f"  distinct complete            : {complete}   (expect exactly {N})")
    print(f"  total GPU runs (sum claim_count): {claims}")
    print(f"  extra runs beyond N          : {extra}   (re-claims of interrupted jobs)")
    print(f"  jobs run more than once      : {reruns}   (bound: <= {args.max_interrupted} interrupted)")
    print(f"  recovered by sweeper (3.4)   : {swept}   (message-less orphans, requeued_at set)")
    print(f"  recovered by SQS redelivery  : {sqs_recovered}   (interrupted, message still live)")
    a1 = complete == N
    a2 = extra >= 0 and extra <= args.max_interrupted
    a3 = reruns <= args.max_interrupted
    ok = a1 and a2 and a3
    print(f"  zero lost: {_verdict(a1)}   extra runs bounded: {_verdict(a2)}   reruns bounded: {_verdict(a3)}   => {_verdict(ok)}")
    return 0 if ok else 1


def ttft_quantile(tier: str, start: float, end: float, quantile: float):
    dur = int(max(1, end - start))
    expr = (f"histogram_quantile({quantile}, sum by (le) (rate("
            f"vllm:time_to_first_token_seconds_bucket{{tier=\"{tier}\"}}[{dur}s])))")
    return scalar(expr, end)


def prefix_hit_rate(tier: str, start: float, end: float):
    dur = int(max(1, end - start))
    hits = scalar(f"sum(increase(vllm:prefix_cache_hits_total{{tier=\"{tier}\"}}[{dur}s]))", end)
    qrs = scalar(f"sum(increase(vllm:prefix_cache_queries_total{{tier=\"{tier}\"}}[{dur}s]))", end)
    if not hits or not qrs:
        return None
    return hits / qrs


def _verdict(ok: bool):
    return "PASS" if ok else "FAIL"


def lt3(args):
    delta = claims_delta(args.start, args.end)
    complete = ddb_complete_count(args.run)
    N = args.n
    print(f"LT-3 idempotency (run={args.run}, N={N})")
    if delta is None:
        print("  INCONCLUSIVE: worker_claims_total not found in Prometheus (check the worker PodMonitor)")
        return 2
    print(f"  distinct jobs complete : {complete}   (expect exactly {N})")
    print(f"  worker_claims_total delta (actual GPU runs): {delta:.0f}   (expect exactly {N})")
    a1 = complete == N
    a2 = abs(delta - N) < 0.5
    if not a2 and delta > N:
        print(f"  NOTE: delta > N means dedup let {delta - N:.0f} duplicate GPU runs through (partial or full break)")
    ok = a1 and a2
    print(f"  3a distinct-complete==N: {_verdict(a1)}   3b claims-delta==N: {_verdict(a2)}   => {_verdict(ok)}")
    return 0 if ok else 1


def lt4(args):
    """LT-4 isolation verdict: interactive TTFT p95 during the batch flood must stay
    within TOL of the quiet baseline AND under the absolute target. Both read the
    server-side vllm histogram, so client RTT does not distort it."""
    base = ttft_quantile("interactive", args.start, args.end, 0.95)
    flood = ttft_quantile("interactive", args.flood_start, args.flood_end, 0.95)
    tol = args.tol
    target = args.target_ms / 1000.0
    print(f"LT-4 isolation (interactive p95 TTFT, tol {int(tol*100)}%, target {args.target_ms}ms)")
    if base is None or flood is None:
        print("  INCONCLUSIVE: TTFT histogram missing for a window"); return 2
    print(f"  baseline p95 : {base*1000:.0f} ms")
    print(f"  under flood  : {flood*1000:.0f} ms   (ratio {flood/base:.2f}x)")
    a1 = flood <= base * (1 + tol)
    a2 = flood <= target
    ok = a1 and a2
    print(f"  within {int(tol*100)}% of baseline: {_verdict(a1)}   under target: {_verdict(a2)}   => {_verdict(ok)}")
    return 0 if ok else 1


def lt2(args):
    """LT-2 routing verdict: on prefix-heavy, EPP TTFT p90 lower than round-robin AND
    EPP prefix-cache hit rate higher. Windows: EPP run then round-robin run."""
    epp_ttft = ttft_quantile("interactive", args.start, args.end, 0.90)
    rr_ttft = ttft_quantile("interactive", args.flood_start, args.flood_end, 0.90)
    epp_hit = prefix_hit_rate("interactive", args.start, args.end)
    rr_hit = prefix_hit_rate("interactive", args.flood_start, args.flood_end)
    print("LT-2 routing EPP vs round-robin (prefix-heavy)")
    if None in (epp_ttft, rr_ttft, epp_hit, rr_hit):
        print("  INCONCLUSIVE: missing TTFT or prefix-cache metric for a window"); return 2
    print(f"  TTFT p90       EPP={epp_ttft*1000:.0f} ms   RR={rr_ttft*1000:.0f} ms   (EPP {(1-epp_ttft/rr_ttft)*100:+.0f}%)")
    print(f"  prefix-hit     EPP={epp_hit*100:.1f}%   RR={rr_hit*100:.1f}%")
    a1 = epp_ttft < rr_ttft
    a2 = epp_hit > rr_hit
    ok = a1 and a2
    print(f"  EPP TTFT lower: {_verdict(a1)}   EPP hit higher: {_verdict(a2)}   => {_verdict(ok)}")
    print("  (a null or negative result is a finding, not a silent pass)")
    return 0 if ok else 1


def ttft(args):
    v = ttft_quantile(args.tier, args.start, args.end, args.quantile)
    if v is None:
        print(f"TTFT p{int(args.quantile*100)} {args.tier}: INCONCLUSIVE (no data)"); return 2
    print(f"TTFT p{int(args.quantile*100)} {args.tier}: {v*1000:.0f} ms")
    return 0


def prefix_hit(args):
    v = prefix_hit_rate(args.tier, args.start, args.end)
    if v is None:
        print(f"prefix-cache hit rate {args.tier}: INCONCLUSIVE (no data)"); return 2
    print(f"prefix-cache hit rate {args.tier}: {v*100:.1f}%")
    return 0


def claims_delta_cmd(args):
    d = claims_delta(args.start, args.end)
    print("claims delta:", "INCONCLUSIVE" if d is None else f"{d:.0f}")
    return 0 if d is not None else 2


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("lt3", "ttft", "prefix-hit", "claims-delta", "lt4", "lt2", "lt6d"):
        s = sub.add_parser(name)
        s.add_argument("--start", type=float, default=0)
        s.add_argument("--end", type=float, default=0)
        # --tier is interpolated into PromQL label matchers, so it is restricted to the tier labels
        # the serving Deployments carry (manifests/02-serving/) rather than accepted as free text.
        s.add_argument("--tier", default="interactive",
                       choices=("interactive", "batch", "premium", "economy", "neuron"))
        s.add_argument("--quantile", type=float, default=0.95)
        s.add_argument("--run", default="run")
        s.add_argument("--n", type=int, default=0)
        s.add_argument("--flood-start", dest="flood_start", type=float, default=0)
        s.add_argument("--flood-end", dest="flood_end", type=float, default=0)
        s.add_argument("--tol", type=float, default=0.15)
        s.add_argument("--target-ms", dest="target_ms", type=float, default=2000)
        s.add_argument("--max-interrupted", dest="max_interrupted", type=int, default=0)
    a = ap.parse_args()
    fn = {"lt3": lt3, "ttft": ttft, "prefix-hit": prefix_hit, "claims-delta": claims_delta_cmd,
          "lt4": lt4, "lt2": lt2, "lt6d": lt6d}[a.cmd]
    sys.exit(fn(a))


if __name__ == "__main__":
    main()
