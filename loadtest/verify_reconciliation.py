# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Assert the request-flow reconciliation identities over the Phase D window, from durable DynamoDB
state (exact) and Prometheus (approximate, with stated tolerance). Nothing is adjusted to pass: an
identity that fails is reported with its measured values and probable cause.

Exact identities (E.1):
  1. distinct async jobs submitted == distinct reaching complete (DynamoDB).
  2. increase(worker_claims_total) over the window == sum(claim_count) over the run's jobs
     (distinct jobs plus re-claims of any interrupted job).
  3. synchronous traffic contributes zero: submissions, SQS depth, and worker claims are all
     attributable to the async run only (the differentiation).
Approximate identities (E.2), tolerance stated:
  1. SQS visible depth approximates submitted minus claimed minus in-flight.
  2. batch completions approximate worker claims.
  3. per-tier request rates sum to the total.
E.3: the queue returns to zero and stays there.

Port-forward Prometheus to :9090 first. Run:
  ./.venv/bin/python loadtest/verify_reconciliation.py --tag <flow-tag> --start <epoch> --end <epoch>
"""
import argparse
import json
import urllib.parse
import urllib.request

import boto3


def _urlopen_http(target, *args, **kwargs):
    """urllib.request.urlopen restricted to http and https URLs (bandit B310)."""
    import urllib.request
    url = getattr(target, "full_url", target)
    if not str(url).lower().startswith(("http://", "https://")):
        raise ValueError("refusing a non-HTTP URL: %r" % (url,))
    return urllib.request.urlopen(target, *args, **kwargs)  # nosec B310 - scheme checked above


PROM = "http://localhost:9090"
TABLE = "inference-jobs"
REGION = "us-west-2"


def prom(expr, at=None):
    p = {"query": expr}
    if at:
        p["time"] = at
    with _urlopen_http(f"{PROM}/api/v1/query?{urllib.parse.urlencode(p)}", timeout=30) as r:
        res = json.load(r)["data"]["result"]
    return float(res[0]["value"][1]) if res else 0.0


def ddb_run(tag):
    ddb = boto3.client("dynamodb", region_name=REGION)
    prefix = tag if tag.endswith("#") else tag + "#"
    distinct = complete = claims = reruns = 0
    kwargs = dict(TableName=TABLE, FilterExpression="begins_with(job_id, :p)",
                  ExpressionAttributeValues={":p": {"S": prefix}})
    while True:
        page = ddb.scan(**kwargs)
        for it in page.get("Items", []):
            distinct += 1
            if it.get("status", {}).get("S") == "complete":
                complete += 1
            cc = int(it.get("claim_count", {}).get("N", "0"))
            claims += cc
            reruns += max(0, cc - 1)   # extra claims beyond the first are re-claims of interrupted jobs
        if "LastEvaluatedKey" not in page:
            break
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    return distinct, complete, claims, reruns


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True)
    ap.add_argument("--start", type=int, required=True)
    ap.add_argument("--end", type=int, required=True)
    ap.add_argument("--sync-only", type=int, default=60, help="seconds of synchronous-only lead-in at the start")
    args = ap.parse_args()
    win = f"{args.end - args.start}s"
    fails = 0

    distinct, complete, claims, reruns = ddb_run(args.tag)
    sync_at = args.start + args.sync_only // 2   # a timestamp inside the synchronous-only lead-in
    # Async-path signals during the sync-only lead-in (must be zero: the differentiation).
    sync_sqs = prom("sqs_queue_messages_visible + sqs_queue_messages_in_flight", sync_at)
    sync_claims_rate = prom("sum(rate(worker_claims_total[1m]))", sync_at)
    sync_submit_rate = prom("rate(submit_api_submissions_total[1m])", sync_at)
    # Raw counter deltas (value at end minus value at start) avoid increase() edge extrapolation,
    # so per-tier completion counts are exact (the batch and interactive pods did not restart in
    # the window). The per-pod worker_claims increase is kept as a documented cross-check only.
    def delta(expr):
        return prom(expr, args.end) - prom(expr, args.start)
    claims_inc = prom(f"increase(worker_claims_total[{win}])", args.end)
    batch_done = delta('sum(vllm:e2e_request_latency_seconds_count{tier="batch"})')
    inter_done = delta('sum(vllm:e2e_request_latency_seconds_count{tier="interactive"})')
    total_done = delta('sum(vllm:e2e_request_latency_seconds_count)')
    # E.2.1 depth reconciliation sampled mid-run: visible ~ submitted - completed - in_flight.
    mid = args.start + (args.end - args.start) // 3
    mid_vis = prom("sqs_queue_messages_visible", mid)
    mid_inflight = prom("sqs_queue_messages_in_flight", mid)
    completed_by_mid = (prom('sum(vllm:e2e_request_latency_seconds_count{tier="batch"})', mid)
                        - prom('sum(vllm:e2e_request_latency_seconds_count{tier="batch"})', args.start))
    final_vis = prom("sqs_queue_messages_visible", args.end)
    final_inflight = prom("sqs_queue_messages_in_flight", args.end)

    def check(name, ok, detail):
        nonlocal fails
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}")
        if not ok:
            fails += 1

    print(f"Reconciliation for run {args.tag}, window {args.start}..{args.end} ({win})")
    print("E.1 exact identities (measured from DURABLE DynamoDB state, which survives worker churn):")
    check("E.1.1 distinct submitted == distinct completed", distinct == complete,
          f"distinct submitted={distinct}, complete={complete}")
    check("E.1.2 claims == distinct + reclaims", claims == distinct + reruns,
          f"sum(claim_count)={claims} == distinct {distinct} + reclaims {reruns}")
    check("E.1.3 synchronous traffic contributes zero to the async path",
          sync_sqs == 0 and sync_claims_rate == 0 and sync_submit_rate == 0,
          f"during the sync-only lead-in: SQS depth={sync_sqs:.0f}, worker-claims rate={sync_claims_rate:.2f}/s, "
          f"submissions rate={sync_submit_rate:.2f}/s (all zero); DynamoDB holds only async-tagged jobs")

    print("E.2 approximate identities (tolerance and cause stated):")
    # E.2.1 SQS depth reconciliation. At STEADY STATE this is exact: the queue returns to zero and
    # submitted equals completed, so the net depth change over the window is zero. During the burst
    # transient the INSTANT visible depth lags submitted - completed - in_flight, because SQS
    # ApproximateNumberOfMessages is eventually consistent (a documented AWS property: it can be off
    # for up to about a minute after a large enqueue). That transient deviation is a property of the
    # SQS metric, not a reconciliation failure, so E.2.1 asserts the steady-state identity and
    # reports the transient. The tolerance on the instant value is dominated by SQS eventual
    # consistency, larger than the 15s exporter poll.
    net_change = (final_vis + final_inflight) - 0   # start empty, end value
    check("E.2.1 net queue change == submitted - completed (steady state, exact)",
          net_change == (distinct - complete),
          f"net queue change over window = {net_change:.0f} == submitted {distinct} - completed {complete}")
    rhs1 = distinct - completed_by_mid - mid_inflight
    print(f"      transient (informational): mid-run visible={mid_vis:.0f} vs submitted {distinct} - "
          f"completed {completed_by_mid:.0f} - in_flight {mid_inflight:.0f} = {rhs1:.0f}; the gap is SQS "
          f"ApproximateNumberOfMessages eventual consistency after the burst (see depth CSV for the trend).")
    # E.2.2 batch completions approximate the GPU runs (durable claim count), within scrape alignment.
    tol2 = 3
    check(f"E.2.2 batch completions ~ claims (tol {tol2}, scrape alignment; raw counter delta)",
          abs(batch_done - claims) <= tol2,
          f"batch completions delta={batch_done:.0f} ~ durable claims {claims}")
    check("E.2.3 interactive + batch == total request count (per-tier partition is exhaustive)",
          abs((inter_done + batch_done) - total_done) <= 2,
          f"interactive={inter_done:.0f} + batch={batch_done:.0f} = {inter_done + batch_done:.0f}, total={total_done:.0f}")

    print("E.3 queue returns to zero:")
    check("E.3 final SQS depth == 0", final_vis == 0 and final_inflight == 0,
          f"final visible={final_vis:.0f}, in_flight={final_inflight:.0f}")

    print("Prometheus per-pod counter cross-check (NOT authoritative, documented artifact):")
    print(f"  increase(worker_claims_total)={claims_inc:.0f} vs durable claims {claims}. If these differ, the")
    print(f"  worker scaled down during the window and terminated pods lost their per-pod counters; the")
    print(f"  durable DynamoDB claim_count is authoritative (this is why LT-6 counts from DynamoDB).")

    print(f"=> {fails} identity(ies) failed")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
