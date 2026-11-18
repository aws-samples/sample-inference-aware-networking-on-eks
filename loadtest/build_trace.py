# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Build the end-to-end per-request trace for a run, from durable state, by joining three
sources on the correlation id (which IS the deterministic job_id):

  1. DynamoDB (inference-jobs): submitted_at, claimed_at, backend_selected_at, completed_at,
     status, claim_count, per job_id.
  2. Envoy access log (the inference gateway): the REQTRACE line for each correlation id
     gives the selected upstream vLLM pod (IP:port) that served the request. This is the
     authoritative record of which backend served which request.
  3. The live cluster: pod IP -> pod name, tier, node; node -> instance type (accelerator).

Output: a markdown per-request trace table and a JSON sidecar under loadtest/results/, plus a
routing verdict (every request served by a pod of the tier its objective selected, zero
cross-tier). Decides pass or fail from the joined durable trace, not from an assessment.

Usage:
  ./.venv/bin/python loadtest/build_trace.py --run <job_id-prefix> [--objective batch] \
      [--out loadtest/results/trace.md]
"""
import argparse
import json
import re
import subprocess
import sys

import boto3

TABLE = "inference-jobs"
REGION = "us-west-2"
ENVOY_NS = "envoy-gateway-system"

# ONE DEFINITION, imported. This file carried its own copy of the pattern and it went stale with the
# other two on 2026-08-04; cluster_probe.py exists precisely so the parsers cannot drift apart.
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from cluster_probe import REQTRACE  # noqa: E402


def sh(*args: str) -> str:
    # Every caller passes literal kubectl arguments: list form, no shell, no external input.
    return subprocess.run(args, capture_output=True, text=True).stdout  # nosec B603  # nosemgrep


def pod_map() -> dict:
    """podIP -> {pod, tier, node} for every vLLM serving pod."""
    out = sh("kubectl", "-n", "inference", "get", "pods", "-l", "app=vllm-llama3",
             "-o", "json")
    m = {}
    for it in json.loads(out or '{"items":[]}')["items"]:
        ip = it.get("status", {}).get("podIP")
        if not ip:
            continue
        m[ip] = {
            "pod": it["metadata"]["name"],
            "tier": it["metadata"].get("labels", {}).get("tier", "?"),
            "node": it["spec"].get("nodeName", "?"),
        }
    return m


def node_accel() -> dict:
    """nodeName -> accelerator string (from the instance type + nodepool labels)."""
    out = sh("kubectl", "get", "nodes", "-o", "json")
    m = {}
    for n in json.loads(out or '{"items":[]}')["items"]:
        labels = n["metadata"].get("labels", {})
        itype = labels.get("node.kubernetes.io/instance-type", "?")
        pool = labels.get("karpenter.sh/nodepool", "?")
        # g6e/g6/g5 are NVIDIA GPU; inf2/trn are AWS Neuron. Report the instance type so the
        # accelerator is exact and verifiable, with the family as a readable label.
        fam = "NVIDIA-GPU" if itype.startswith(("g6", "g5", "p5", "p4")) else (
            "AWS-Neuron" if itype.startswith(("inf", "trn")) else "?")
        m[n["metadata"]["name"]] = f"{fam} ({itype}, pool={pool})"
    return m


def envoy_traces() -> dict:
    """correlation id -> upstream host (IP:port), from the gateway access log."""
    dep = ""
    for line in sh("kubectl", "-n", ENVOY_NS, "get", "deploy", "-o", "name").splitlines():
        if "envoy-inference" in line:
            dep = line.strip()
            break
    if not dep:
        return {}
    logs = sh("kubectl", "-n", ENVOY_NS, "logs", dep, "--tail=-1")
    traces = {}
    for line in logs.splitlines():
        mm = REQTRACE.search(line)
        if mm:
            traces[mm.group("cid")] = {"upstream": mm.group("upstream"), "code": mm.group("code")}
    return traces


def ddb_items(prefix: str) -> dict:
    ddb = boto3.client("dynamodb", region_name=REGION)
    items = {}
    kwargs = dict(TableName=TABLE, FilterExpression="begins_with(job_id, :p)",
                  ExpressionAttributeValues={":p": {"S": prefix}})
    while True:
        page = ddb.scan(**kwargs)
        for it in page.get("Items", []):
            jid = it["job_id"]["S"]
            items[jid] = {k: list(v.values())[0] for k, v in it.items()}
        if "LastEvaluatedKey" not in page:
            break
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    return items


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True, help="job_id prefix, e.g. phased-123#")
    ap.add_argument("--objective", default="batch", help="expected tier for this run's jobs")
    ap.add_argument("--out", default="loadtest/results/trace.md")
    ap.add_argument("--limit", type=int, default=25, help="rows in the markdown sample")
    args = ap.parse_args()
    # Use the prefix as given (the run may span several client_ids, e.g. per prefix-group
    # phased-<ts>-g0, -g1, so a trailing "-" matches them all).
    prefix = args.run

    pods = pod_map()
    accel = node_accel()
    traces = envoy_traces()
    items = ddb_items(prefix)

    rows = []
    for jid, it in sorted(items.items()):
        cid = jid
        up = traces.get(cid, {}).get("upstream", "")
        ip = up.split(":")[0] if up else ""
        p = pods.get(ip, {})
        tier = p.get("tier", "unknown")
        node = p.get("node", "?")
        rows.append({
            "correlation_id": cid,
            "submitted_at": it.get("submitted_at", ""),
            "claimed_at": it.get("claimed_at", ""),
            "backend_selected_at": it.get("backend_selected_at", ""),
            "completed_at": it.get("completed_at", ""),
            "status": it.get("status", ""),
            "claim_count": it.get("claim_count", ""),
            "upstream": up,
            "backend_pod": p.get("pod", "(no gateway trace)"),
            "tier": tier,
            "node": node,
            "accelerator": accel.get(node, "?"),
        })

    # Verdict: every traced request served by a pod of the expected tier, zero cross-tier.
    traced = [r for r in rows if r["upstream"]]
    right = [r for r in traced if r["tier"] == args.objective]
    cross = [r for r in traced if r["tier"] not in (args.objective, "unknown")]
    summary = {
        "run": prefix, "expected_tier": args.objective,
        "jobs": len(rows), "traced_through_gateway": len(traced),
        "on_expected_tier": len(right), "cross_tier": len(cross),
    }

    # Markdown table (a representative sample) + full JSON sidecar.
    md = [f"# Per-request trace: {prefix}", "",
          f"Expected tier (objective): `{args.objective}`. "
          f"Jobs: {summary['jobs']}. Traced through the gateway: {summary['traced_through_gateway']}. "
          f"On the expected tier: {summary['on_expected_tier']}. Cross-tier: {summary['cross_tier']}.", "",
          "| correlation_id | submitted | claimed | backend_selected | completed | status | runs | backend_pod | tier | node | accelerator |",
          "|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows[:args.limit]:
        md.append("| {correlation_id} | {submitted_at} | {claimed_at} | {backend_selected_at} | "
                  "{completed_at} | {status} | {claim_count} | {backend_pod} | {tier} | {node} | "
                  "{accelerator} |".format(**r))
    open(args.out, "w").write("\n".join(md) + "\n")
    open(args.out.replace(".md", ".json"), "w").write(json.dumps({"summary": summary, "rows": rows}, indent=2))

    print(json.dumps(summary, indent=2))
    verdict = "PASS" if (traced and summary["cross_tier"] == 0 and summary["on_expected_tier"] == len(traced)) else "FAIL"
    print(f"routing verdict (zero cross-tier, all traced on expected tier): {verdict}")
    print(f"wrote {args.out} and {args.out.replace('.md', '.json')}")
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
