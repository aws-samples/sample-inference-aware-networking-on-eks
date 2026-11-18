# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Phase D cross-tier routing verdict from the Envoy access log. For every request whose
correlation id starts with the run tag (both the synchronous interactive requests and the
asynchronous batch jobs), this reads the REQTRACE line, maps the selected upstream pod to
its tier, and asserts that the tier matches the request's objective:

  objective=batch  -> served by a batch-tier pod
  objective=-      -> served by an interactive-tier pod   (no objective header)

A single cross-tier placement (a batch request on an interactive pod, or the reverse) is a
failure. Decides pass or fail from the trace, not from an assessment.

Usage: ./.venv/bin/python loadtest/phased_verdict.py --tag phased-123
"""
import argparse
import json
import re
import subprocess

ENVOY_NS = "envoy-gateway-system"
# ONE DEFINITION, imported. This file carried its own copy of the pattern and it went stale with the
# other two on 2026-08-04; cluster_probe.py exists precisely so the parsers cannot drift apart.
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from cluster_probe import REQTRACE  # noqa: E402


def sh(*a: str) -> str:
    # Every caller passes literal kubectl arguments: list form, no shell, no external input.
    return subprocess.run(a, capture_output=True, text=True).stdout  # nosec B603  # nosemgrep


def ip_to_tier() -> dict:
    out = sh("kubectl", "-n", "inference", "get", "pods", "-l", "app=vllm-llama3", "-o", "json")
    m = {}
    for it in json.loads(out or '{"items":[]}')["items"]:
        ip = it.get("status", {}).get("podIP")
        if ip:
            m[ip] = it["metadata"].get("labels", {}).get("tier", "?")
    return m


def envoy_dep() -> str:
    for line in sh("kubectl", "-n", ENVOY_NS, "get", "deploy", "-o", "name").splitlines():
        if "envoy-inference" in line:
            return line.strip()
    return ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True)
    args = ap.parse_args()

    tiers = ip_to_tier()
    logs = sh("kubectl", "-n", ENVOY_NS, "logs", envoy_dep(), "--tail=-1")
    batch_ok = int_ok = cross = other = 0
    examples = []
    for line in logs.splitlines():
        mm = REQTRACE.search(line)
        if not mm or args.tag not in mm.group("cid"):
            continue
        obj = mm.group("obj")
        ip = mm.group("upstream").split(":")[0]
        tier = tiers.get(ip, "unknown")
        expect = "batch" if obj == "batch" else "interactive"
        if tier == "unknown":
            other += 1
        elif tier == expect:
            if expect == "batch":
                batch_ok += 1
            else:
                int_ok += 1
        else:
            cross += 1
            if len(examples) < 5:
                examples.append(f"cid={mm.group('cid')} obj={obj} -> tier={tier}")

    print(f"Phase D cross-tier verdict (tag={args.tag})")
    print(f"  batch requests on batch pods       : {batch_ok}")
    print(f"  interactive requests on interactive: {int_ok}")
    print(f"  cross-tier placements              : {cross}   (must be 0)")
    print(f"  unresolved upstream (pod IP not current): {other}")
    for e in examples:
        print(f"    CROSS: {e}")
    verdict = "PASS" if (cross == 0 and (batch_ok + int_ok) > 0) else "FAIL"
    print(f"  RESULT: {verdict}")
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
