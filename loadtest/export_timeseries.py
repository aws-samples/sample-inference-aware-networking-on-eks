# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Export the raw time series behind the Request Journey dashboard for the run window, so the
proof exists as committed data and not only as a panel definition (Phase E.2). Reads range
queries from Prometheus (port-forward it to localhost:9090 first) and writes both JSON and a
flat CSV under loadtest/results/.

Series: queue depth (batch serving queue), per-tier backend request rate (batch and
interactive), and interactive time-to-first-token p95. The authoritative SQS queue-depth
series is loadtest/results/phasec_queue_depth.csv (sampled directly from SQS during Phase C).

Usage: ./.venv/bin/python loadtest/export_timeseries.py --minutes 45 --now <epoch>
"""
import argparse
import json
import urllib.parse
import urllib.request


def _urlopen_http(target, *args, **kwargs):
    """urllib.request.urlopen restricted to http and https URLs (bandit B310)."""
    import urllib.request
    url = getattr(target, "full_url", target)
    if not str(url).lower().startswith(("http://", "https://")):
        raise ValueError("refusing a non-HTTP URL: %r" % (url,))
    return urllib.request.urlopen(target, *args, **kwargs)  # nosec B310 - scheme checked above


PROM = "http://localhost:9090"
SERIES = {
    "batch_queue_waiting": 'sum(vllm:num_requests_waiting{tier="batch"})',
    "batch_req_per_s": 'sum(rate(vllm:e2e_request_latency_seconds_count{tier="batch"}[1m]))',
    "interactive_req_per_s": 'sum(rate(vllm:e2e_request_latency_seconds_count{tier="interactive"}[1m]))',
    "interactive_ttft_p95": 'histogram_quantile(0.95, sum(rate(vllm:time_to_first_token_seconds_bucket{tier="interactive"}[2m])) by (le))',
}


def range_query(expr: str, start: float, end: float, step: int):
    qs = urllib.parse.urlencode({"query": expr, "start": start, "end": end, "step": step})
    with _urlopen_http(f"{PROM}/api/v1/query_range?{qs}", timeout=30) as r:
        data = json.load(r)
    res = data.get("data", {}).get("result", [])
    return res[0]["values"] if res else []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--now", type=float, required=True, help="window end epoch (passed in; the runtime clock is not used here)")
    ap.add_argument("--minutes", type=int, default=45)
    ap.add_argument("--step", type=int, default=30)
    args = ap.parse_args()
    end = args.now
    start = end - args.minutes * 60

    out = {"window": {"start": start, "end": end, "step": args.step}, "series": {}}
    for name, expr in SERIES.items():
        out["series"][name] = range_query(expr, start, end, args.step)

    json_path = "loadtest/results/request_journey_timeseries.json"
    with open(json_path, "w") as fh:
        json.dump(out, fh, indent=2)

    # Flat CSV: timestamp then one column per series (aligned on the shared step grid).
    grid = sorted({int(float(ts)) for s in out["series"].values() for ts, _ in s})
    idx = {name: {int(float(ts)): v for ts, v in s} for name, s in out["series"].items()}
    csv_path = "loadtest/results/request_journey_timeseries.csv"
    cols = list(SERIES.keys())
    with open(csv_path, "w") as f:
        f.write("epoch," + ",".join(cols) + "\n")
        for ts in grid:
            f.write(str(ts) + "," + ",".join(idx[c].get(ts, "") for c in cols) + "\n")
    print(f"wrote {json_path} ({sum(len(s) for s in out['series'].values())} points) and {csv_path} ({len(grid)} rows)")


if __name__ == "__main__":
    main()
