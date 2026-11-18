#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Generate the Spike B Neuron dashboard: the KV-cache metric and routing signals per Neuron
replica, so a future Neuron run renders live. This pass's measured data is committed as JSON/CSV
under loadtest/results/spike-b/ (the Neuron pods were not scraped during the runs; this dashboard
plus manifests/05-observability/neuron-vllm-podmonitor.yaml makes the next run observable). The KV
panel is labelled with the Phase 2 finding: the metric means occupied slots with prefix caching
OFF and KV memory with prefix caching ON. Output: spike-b-neuron.json + its ConfigMap.

Run: ./.venv/bin/python loadtest/dashboards/build_spike_b_dashboard.py
"""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
DS = {"type": "prometheus", "uid": "prometheus"}
_id = [0]
NEU = 'exported_pod=~"vllm-llama3-neuron.*"'


def ts(title, exprs, y, x=0, w=12, h=8, unit="short", ymax=None):
    _id[0] += 1
    return {"id": _id[0], "title": title, "type": "timeseries", "datasource": DS,
            "gridPos": {"h": h, "w": w, "x": x, "y": y},
            "fieldConfig": {"defaults": {"unit": unit, "max": ymax,
                                         "custom": {"drawStyle": "line", "fillOpacity": 12}}, "overrides": []},
            "targets": [{"refId": chr(65 + i), "datasource": DS, "expr": e, "legendFormat": lf}
                        for i, (e, lf) in enumerate(exprs)]}


def row(title, y):
    _id[0] += 1
    return {"id": _id[0], "title": title, "type": "row", "collapsed": False,
            "gridPos": {"h": 1, "w": 24, "x": 0, "y": y}}


panels = []
y = 0
panels.append(row("Neuron KV-cache metric (Phase 2: OFF=occupied slots, ON=KV memory)", y)); y += 1
panels.append(ts("vllm:kv_cache_usage_perc per replica", [
    ("vllm:kv_cache_usage_perc", "{{pod}}")], y, 0, 12, 8, "percentunit", 1))
panels.append(ts("Requests running per replica", [
    ("vllm:num_requests_running", "{{pod}}")], y, 12, 12, 8)); y += 8
panels.append(row("Routing signals (prefix-cache locality and preemptions)", y)); y += 1
panels.append(ts("Prefix-cache hit rate (rate ratio, prefix caching ON only)", [
    ("sum(rate(vllm:prefix_cache_hits_total[2m])) / clamp_min(sum(rate(vllm:prefix_cache_queries_total[2m])),1)", "hit rate")], y, 0, 12, 8, "percentunit", 1))
panels.append(ts("Preemptions/sec (dead on the contiguous OFF path)", [
    ("sum(rate(vllm:num_preemptions_total[2m]))", "preemptions/s")], y, 12, 12, 8, "short")); y += 8
panels.append(row("Latency", y)); y += 1
panels.append(ts("Neuron TTFT p95", [
    ("histogram_quantile(0.95, sum by (le)(rate(vllm:time_to_first_token_seconds_bucket[2m])))", "p95 TTFT")], y, 0, 12, 8, "s"))
panels.append(ts("Requests waiting per replica", [
    ("vllm:num_requests_waiting", "{{pod}}")], y, 12, 12, 8)); y += 8

dash = {"uid": "spike-b-neuron", "title": "Spike B: Neuron KV semantics and routing",
        "tags": ["spike-b", "neuron", "inference"], "timezone": "utc", "schemaVersion": 39,
        "time": {"from": "now-1h", "to": "now"}, "refresh": "15s",
        "templating": {"list": []}, "annotations": {"list": []}, "panels": panels}

with open(os.path.join(HERE, "spike-b-neuron.json"), "w") as fh:
    json.dump(dash, fh, indent=2)
cm = {"apiVersion": "v1", "kind": "ConfigMap",
      "metadata": {"name": "spike-b-neuron-dashboard", "namespace": "monitoring",
                   "labels": {"grafana_dashboard": "1"}},
      "data": {"spike-b-neuron.json": json.dumps(dash)}}
with open(os.path.join(HERE, "spike-b-neuron-configmap.json"), "w") as fh:
    json.dump(cm, fh, indent=2)
print("wrote spike-b-neuron.json and spike-b-neuron-configmap.json")
