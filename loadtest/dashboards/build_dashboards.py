#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Generate and provision the Grafana dashboards for the inference stack, as ConfigMaps the
kube-prometheus-stack Grafana sidecar imports (label grafana_dashboard=1, namespace monitoring).

Produces three dashboards plus their ConfigMaps:
  - Request Journey (wraps the committed request-journey.json)
  - vLLM serving (per-tier requests, TTFT, tokens/s, prefix cache, e2e latency)
  - DCGM GPU (utilization, framebuffer, temperature, power)

Every panel uses the Prometheus datasource by uid "prometheus", which is this cluster's default
Grafana datasource uid (verified against the running Grafana API), so the dashboards render on a
fresh import with no datasource remap. Run:
  ./.venv/bin/python loadtest/dashboards/build_dashboards.py
Then apply the ConfigMaps: kubectl apply -f loadtest/dashboards/<name>-configmap.json
"""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
DS = {"type": "prometheus", "uid": "prometheus"}
_id = [0]


def panel(title, exprs, y, x=0, w=12, h=8, unit="short", ymax=None):
    _id[0] += 1
    return {"id": _id[0], "title": title, "type": "timeseries", "datasource": DS,
            "gridPos": {"h": h, "w": w, "x": x, "y": y},
            "fieldConfig": {"defaults": {"unit": unit, "max": ymax, "custom": {"drawStyle": "line", "fillOpacity": 12}}, "overrides": []},
            "targets": [{"refId": chr(65 + i), "datasource": DS, "expr": e, "legendFormat": lf}
                        for i, (e, lf) in enumerate(exprs)]}


def dashboard(uid, title, tags, panels):
    return {"uid": uid, "title": title, "tags": tags, "timezone": "utc", "schemaVersion": 39,
            "time": {"from": "now-1h", "to": "now"}, "refresh": "10s",
            "templating": {"list": []}, "annotations": {"list": []}, "panels": panels}


def configmap(name, filename, dash):
    return {"apiVersion": "v1", "kind": "ConfigMap",
            "metadata": {"name": name, "namespace": "monitoring", "labels": {"grafana_dashboard": "1"}},
            "data": {filename: json.dumps(dash)}}


def write(dash, base, cm_name):
    with open(os.path.join(HERE, base + ".json"), "w") as fh:
        json.dump(dash, fh, indent=2)
    with open(os.path.join(HERE, base + "-configmap.json"), "w") as fh:
        json.dump(configmap(cm_name, base + ".json", dash), fh, indent=2)
    print(f"wrote {base}.json and {base}-configmap.json")


# ---- vLLM serving dashboard -------------------------------------------------
_id[0] = 0
vllm_panels = [
    panel("Requests running by tier", [('sum by (tier) (vllm:num_requests_running)', '{{tier}}')], 0, 0, 12),
    panel("Requests waiting (queue) by tier", [('sum by (tier) (vllm:num_requests_waiting)', '{{tier}}')], 0, 12, 12),
    panel("TTFT p95 by tier (s)", [
        ('histogram_quantile(0.95, sum by (le,tier) (rate(vllm:time_to_first_token_seconds_bucket[2m])))', '{{tier}}')], 8, 0, 12, unit="s"),
    panel("Generation tokens/sec by tier", [('sum by (tier) (rate(vllm:generation_tokens_total[1m]))', '{{tier}}')], 8, 12, 12),
    panel("Prefix-cache hit rate by tier", [
        ('clamp_max(sum by (tier) (rate(vllm:prefix_cache_hits_total[2m])) / clamp_min(sum by (tier) (rate(vllm:prefix_cache_queries_total[2m])), 1), 1)', '{{tier}}')], 16, 0, 12, unit="percentunit", ymax=1),
    panel("Completed requests/sec by tier", [('sum by (tier) (rate(vllm:e2e_request_latency_seconds_count[1m]))', '{{tier}}')], 16, 12, 12, unit="reqps"),
]
write(dashboard("inference-vllm", "Inference-Aware EKS: vLLM serving", ["inference", "vllm"], vllm_panels),
      "vllm", "vllm-dashboard")

# ---- DCGM GPU dashboard -----------------------------------------------------
_id[0] = 0
dcgm_panels = [
    panel("Per-GPU utilization (%)", [('DCGM_FI_DEV_GPU_UTIL', '{{hostname}} gpu{{gpu}}')], 0, 0, 12, unit="percent"),
    panel("Aggregate fleet GPU utilization (%)", [('avg(DCGM_FI_DEV_GPU_UTIL)', 'avg')], 0, 12, 12, unit="percent"),
    panel("GPU framebuffer used (MiB)", [('sum by (hostname, gpu) (DCGM_FI_DEV_FB_USED)', '{{hostname}} gpu{{gpu}}')], 8, 0, 12, unit="decmbytes"),
    panel("GPU temperature (C)", [('DCGM_FI_DEV_GPU_TEMP', '{{hostname}} gpu{{gpu}}')], 8, 12, 12, unit="celsius"),
    panel("GPU power draw (W)", [('DCGM_FI_DEV_POWER_USAGE', '{{hostname}} gpu{{gpu}}')], 16, 0, 12, unit="watt"),
    panel("GPU node count (NVIDIA)", [('count(count by (hostname) (DCGM_FI_DEV_GPU_UTIL))', 'gpu hosts')], 16, 12, 12),
]
write(dashboard("inference-dcgm-gpu", "Inference-Aware EKS: DCGM GPU", ["inference", "gpu", "dcgm"], dcgm_panels),
      "dcgm-gpu", "dcgm-gpu-dashboard")

# ---- Request Journey ConfigMap (wraps the committed request-journey.json) ----
with open(os.path.join(HERE, "request-journey.json")) as fh:
    rj = json.load(fh)
with open(os.path.join(HERE, "request-journey-configmap.json"), "w") as fh:
    json.dump(configmap("request-journey-dashboard", "request-journey.json", rj), fh, indent=2)
print("wrote request-journey-configmap.json")
