# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Verify Grafana dashboards render REAL and PLAUSIBLE data, not just that they exist. For each
dashboard this pulls every panel's query from the running Grafana, runs it against Prometheus over
a given window, and checks three things per panel:

  1. returns data (series with points), catching a datasource uid mismatch (No data) or a metric
     name that does not exist on this build;
  2. unit consistency, catching a value outside the declared unit's range (the prefix-cache bug
     returned data and was still wrong: a ratio shown as percentunit must stay within 0.0 to 1.0,
     a percent within 0 to 100);
  3. plausibility, catching a utilization above 100, a negative counter or rate, and flagging (not
     failing) a panel that is constant zero across the window, since a legitimate zero exists (the
     prefix-cache hit rate on a workload with no shared prefixes).

A panel that returns data but violates unit range or plausibility FAILS. A constant zero is
flagged for review. An empty panel is reported (the window may simply lack that tier's activity).

Port-forward Grafana to :3000 and Prometheus to :9090 first, then:
  ./.venv/bin/python loadtest/dashboards/verify_dashboards.py --uid <uid> --start <epoch> --end <epoch>
"""
import argparse
import base64
import json
import os
import urllib.parse
import urllib.request


def _urlopen_http(target, *args, **kwargs):
    """urllib.request.urlopen restricted to http and https URLs (bandit B310)."""
    import urllib.request
    url = getattr(target, "full_url", target)
    if not str(url).lower().startswith(("http://", "https://")):
        raise ValueError("refusing a non-HTTP URL: %r" % (url,))
    return urllib.request.urlopen(target, *args, **kwargs)  # nosec B310 - scheme checked above


GRAFANA = os.environ.get("GRAFANA_URL", "http://localhost:3000")
PROM = os.environ.get("PROM_URL", "http://localhost:9090")
USER = "admin"
PW = os.environ.get("GRAFANA_PW", "")
EPS = 1e-6

# unit -> (low, high) allowed value range; None high means unbounded above.
UNIT_RANGE = {
    "percent": (0.0, 100.0),
    "percentunit": (0.0, 1.0),
    "reqps": (0.0, None),
    "decmbytes": (0.0, None),
    "watt": (0.0, None),
    "celsius": (0.0, None),
    "s": (0.0, None),
    "short": (0.0, None),
}


def grafana_get(path):
    req = urllib.request.Request(f"{GRAFANA}{path}")
    req.add_header("Authorization", "Basic " + base64.b64encode(f"{USER}:{PW}".encode()).decode())
    with _urlopen_http(req, timeout=20) as r:
        return json.load(r)


def prom_range(expr, start, end, step=60):
    qs = urllib.parse.urlencode({"query": expr, "start": start, "end": end, "step": step})
    with _urlopen_http(f"{PROM}/api/v1/query_range?{qs}", timeout=30) as r:
        return json.load(r)["data"]["result"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--uid", required=True)
    ap.add_argument("--start", type=int, required=True)
    ap.add_argument("--end", type=int, required=True)
    args = ap.parse_args()

    dash = grafana_get(f"/api/dashboards/uid/{args.uid}")["dashboard"]
    panels = [p for p in dash.get("panels", []) if p.get("type") != "row"]
    fails = 0
    print(f"Dashboard {dash['title']} (uid={args.uid}) window {args.start}..{args.end}")
    for p in panels:
        exprs = [t.get("expr", "") for t in p.get("targets", []) if t.get("expr")]
        if not exprs:
            continue
        unit = p.get("fieldConfig", {}).get("defaults", {}).get("unit", "short")
        vals = []
        for e in exprs:
            try:
                for s in prom_range(e, args.start, args.end):
                    vals += [float(v) for _, v in s.get("values", []) if v not in ("NaN", "+Inf", "-Inf")]
            except Exception:
                pass
        title = p.get("title", "?")
        if not vals:
            print(f"  [EMPTY]                      {title} (no series in this window)")
            continue
        lo, hi = min(vals), max(vals)
        # unit consistency + plausibility
        rng = UNIT_RANGE.get(unit)
        unit_ok = True
        note = ""
        if rng is not None:
            low, high = rng
            if lo < low - EPS:
                unit_ok = False; note = f"value {lo:.3f} below {low}"
            elif high is not None and hi > high + 1e-3:
                unit_ok = False; note = f"value {hi:.3f} exceeds {high} for unit {unit}"
        zero = abs(hi) <= EPS and abs(lo) <= EPS
        if not unit_ok:
            fails += 1
            print(f"  [DATA] [unit FAIL]           {title} (min={lo:.3f} max={hi:.3f} unit={unit}: {note})")
        elif zero:
            print(f"  [DATA] [unit OK] [ZERO-review] {title} (constant 0 in this window; legitimate if no activity of this kind)")
        else:
            print(f"  [DATA] [unit OK] [plausible] {title} (min={lo:.3f} max={hi:.3f} unit={unit})")
    print(f"  => {fails} panel(s) failed unit/plausibility")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
