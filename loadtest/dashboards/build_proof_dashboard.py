#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Generate the 'Inference-Aware EKS: Proof' Grafana dashboard (Phase 2.3).
One row per correctness claim, every panel reading real metrics. Output:
loadtest/dashboards/proof.json (checked in) and a ConfigMap the kube-prometheus-stack
Grafana sidecar imports (label grafana_dashboard=1).
Run: ./.venv/bin/python loadtest/dashboards/build_proof_dashboard.py
"""
import json
import os

DS = {"type": "prometheus", "uid": "prometheus"}
HERE = os.path.dirname(os.path.abspath(__file__))
_id = [0]


def panel(title, exprs, y, x=0, w=12, h=8, unit="short"):
    _id[0] += 1
    return {
        "id": _id[0], "title": title, "type": "timeseries", "datasource": DS,
        "gridPos": {"h": h, "w": w, "x": x, "y": y},
        "fieldConfig": {"defaults": {"unit": unit}, "overrides": []},
        "targets": [{"refId": chr(65 + i), "datasource": DS, "expr": e, "legendFormat": lf}
                    for i, (e, lf) in enumerate(exprs)],
    }


def row(title, y):
    _id[0] += 1
    return {"id": _id[0], "title": title, "type": "row", "collapsed": False,
            "gridPos": {"h": 1, "w": 24, "x": 0, "y": y}}


panels = []
y = 0
# Row 1: Routing value (LT-2)
panels.append(row("LT-2 Routing value: EPP vs round-robin", y)); y += 1
panels.append(panel("Per-GPU utilization (%)", [("DCGM_FI_DEV_GPU_UTIL", "{{hostname}} gpu{{gpu}}")], y, 0, 12, 8, "percent"))
panels.append(panel("Prefix-cache hit rate (interactive)",
                    [("clamp_max(sum(rate(vllm:prefix_cache_hits_total{tier=\"interactive\"}[1m]))/"
                      "clamp_min(sum(rate(vllm:prefix_cache_queries_total{tier=\"interactive\"}[1m])),1), 1)",
                      "hit rate")], y, 12, 12, 8, "percentunit")); y += 8
# Row 2: Idempotency (LT-3)
panels.append(row("LT-3 Idempotency: actual GPU runs vs jobs", y)); y += 1
panels.append(panel("worker_claims_total (actual GPU runs)", [("sum(worker_claims_total)", "GPU runs")], y, 0, 12, 8))
panels.append(panel("Async submit rate", [("sum(rate(vllm:num_requests_running{tier=\"batch\"}[1m]))", "batch running")], y, 12, 12, 8)); y += 8
# Row 3: Isolation (LT-4)
panels.append(row("LT-4 Isolation: interactive holds under batch flood", y)); y += 1
panels.append(panel("Interactive TTFT p95 (s)",
                    [("histogram_quantile(0.95, sum by (le) (rate(vllm:time_to_first_token_seconds_bucket{tier=\"interactive\"}[1m])))", "p95 TTFT")], y, 0, 12, 8, "s"))
panels.append(panel("Batch tokens/sec",
                    [("sum(rate(vllm:generation_tokens_total{tier=\"batch\"}[1m]))", "batch tok/s")], y, 12, 12, 8)); y += 8
# Row 4: Autoscaling (LT-5)
panels.append(row("LT-5 Autoscaling: queue, replicas, nodes", y)); y += 1
panels.append(panel("Queue depth (waiting) by tier",
                    [("sum by (tier) (vllm:num_requests_waiting)", "{{tier}}")], y, 0, 8, 8))
panels.append(panel("Ready replicas by tier",
                    [("sum by (deployment) (kube_deployment_status_replicas_ready{deployment=~\"vllm-llama3-.*\"})", "{{deployment}}")], y, 8, 8, 8))
panels.append(panel("GPU node count",
                    [("count(count by (hostname) (DCGM_FI_DEV_GPU_UTIL))", "gpu nodes")], y, 16, 8, 8)); y += 8
# Row 5: Utilization and cost
panels.append(row("Fleet utilization and GPU-hours", y)); y += 1
panels.append(panel("Aggregate fleet GPU utilization (%)", [("avg(DCGM_FI_DEV_GPU_UTIL)", "avg gpu util")], y, 0, 12, 8, "percent"))
panels.append(panel("GPU framebuffer used (MiB)", [("sum(DCGM_FI_DEV_FB_USED)", "fb used")], y, 12, 12, 8, "decmbytes")); y += 8

dash = {
    "uid": "inference-aware-proof", "title": "Inference-Aware EKS: Proof",
    "tags": ["loadtest", "proof"], "timezone": "utc", "schemaVersion": 39,
    "time": {"from": "now-1h", "to": "now"}, "refresh": "10s", "panels": panels,
}

os.makedirs(HERE, exist_ok=True)
with open(os.path.join(HERE, "proof.json"), "w") as f:
    json.dump(dash, f, indent=2)

# ConfigMap for the Grafana sidecar (namespace monitoring, label grafana_dashboard=1).
cm = {
    "apiVersion": "v1", "kind": "ConfigMap",
    "metadata": {"name": "proof-dashboard", "namespace": "monitoring",
                 "labels": {"grafana_dashboard": "1"}},
    "data": {"proof.json": json.dumps(dash)},
}
with open(os.path.join(HERE, "proof-dashboard-configmap.json"), "w") as f:
    json.dump(cm, f, indent=2)
print("wrote proof.json and proof-dashboard-configmap.json")
