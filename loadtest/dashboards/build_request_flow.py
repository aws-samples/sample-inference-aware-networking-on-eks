#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Generate the "Request Flow" Grafana dashboard: the two request paths as two lanes on one
shared timeline. Row 1 is the asynchronous path (long requests through the durable SQS queue to
the batch tier), row 2 is the synchronous path (short requests straight through the gateway to the
interactive tier) with an explicit panel showing the async-path metrics are zero for synchronous
traffic, row 3 is reconciliation stats for the selected window, row 4 is GPU utilization split by
tier. Every panel uses the Prometheus datasource by uid "prometheus" and the DCGM node label is the
lowercase hostname. Output: request-flow.json + request-flow-configmap.json.

Run: ./.venv/bin/python loadtest/dashboards/build_request_flow.py
"""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
DS = {"type": "prometheus", "uid": "prometheus"}
_id = [0]


def ts(title, exprs, y, x=0, w=12, h=8, unit="short", ymax=None):
    _id[0] += 1
    return {"id": _id[0], "title": title, "type": "timeseries", "datasource": DS,
            "gridPos": {"h": h, "w": w, "x": x, "y": y},
            "fieldConfig": {"defaults": {"unit": unit, "max": ymax,
                                         "custom": {"drawStyle": "line", "fillOpacity": 12}}, "overrides": []},
            "targets": [{"refId": chr(65 + i), "datasource": DS, "expr": e, "legendFormat": lf}
                        for i, (e, lf) in enumerate(exprs)]}


def stat(title, expr, y, x, w=5, h=6, unit="short", calc="lastNotNull"):
    _id[0] += 1
    return {"id": _id[0], "title": title, "type": "stat", "datasource": DS,
            "gridPos": {"h": h, "w": w, "x": x, "y": y},
            "fieldConfig": {"defaults": {"unit": unit}, "overrides": []},
            "options": {"reduceOptions": {"calcs": [calc], "fields": "", "values": False},
                        "textMode": "value_and_name", "colorMode": "value"},
            "targets": [{"refId": "A", "datasource": DS, "expr": expr, "instant": False}]}


def row(title, y):
    _id[0] += 1
    return {"id": _id[0], "title": title, "type": "row", "collapsed": False,
            "gridPos": {"h": 1, "w": 24, "x": 0, "y": y}}


panels = []
y = 0

# Row 1: asynchronous path (long requests, durable queue, batch tier)
panels.append(row("Asynchronous path: long requests through the SQS queue to the batch tier", y)); y += 1
panels.append(ts("Submissions/sec (async entry point)", [("rate(submit_api_submissions_total[1m])", "submissions/s")], y, 0, 8, 8, "reqps"))
panels.append(ts("SQS queue depth (real, not a proxy)", [
    ("sqs_queue_messages_visible", "visible (waiting)"),
    ("sqs_queue_messages_in_flight", "in flight (processing)")], y, 8, 8, 8))
panels.append(ts("Worker claims/sec (GPU runs)", [("sum(rate(worker_claims_total[1m]))", "claims/s")], y, 16, 8, 8, "reqps")); y += 8
panels.append(ts("Batch tier requests running", [('sum(vllm:num_requests_running{tier="batch"})', "batch running")], y, 0, 12, 8))
panels.append(ts("Batch tier completions/sec", [('sum(rate(vllm:e2e_request_latency_seconds_count{tier="batch"}[1m]))', "batch completed/s")], y, 12, 12, 8, "reqps")); y += 8

# Row 2: synchronous path (short requests, no queue, interactive tier)
panels.append(row("Synchronous path: short requests straight through the gateway to the interactive tier", y)); y += 1
panels.append(ts("Interactive tier request rate", [('sum(rate(vllm:e2e_request_latency_seconds_count{tier="interactive"}[1m]))', "interactive req/s")], y, 0, 8, 8, "reqps"))
panels.append(ts("Interactive tier requests running", [('sum(vllm:num_requests_running{tier="interactive"})', "interactive running")], y, 8, 8, 8))
panels.append(ts("Interactive TTFT p95", [('histogram_quantile(0.95, sum by (le)(rate(vllm:time_to_first_token_seconds_bucket{tier="interactive"}[2m])))', "p95 TTFT")], y, 16, 8, 8, "s")); y += 8
# The differentiation: async-path metrics are ZERO for synchronous traffic.
panels.append(ts("Async-path metrics stay zero for synchronous traffic (the differentiation)", [
    ("rate(submit_api_submissions_total[1m])", "submissions/s"),
    ("sqs_queue_messages_visible + sqs_queue_messages_in_flight", "SQS total depth"),
    ("sum(rate(worker_claims_total[1m]))", "worker claims/s")], y, 0, 24, 8)); y += 8

# Row 3: reconciliation stats over the selected window
panels.append(row("Reconciliation over the selected window (check the arithmetic)", y)); y += 1
panels.append(stat("submitted", "increase(submit_api_submissions_total[$__range])", y, 0))
panels.append(stat("claimed (GPU runs)", "increase(worker_claims_total[$__range])", y, 5))
panels.append(stat("batch completions", 'increase(sum(vllm:e2e_request_latency_seconds_count{tier="batch"})[$__range:])', y, 10))
panels.append(stat("queue visible now", "sqs_queue_messages_visible", y, 15, 4))
panels.append(stat("queue in flight now", "sqs_queue_messages_in_flight", y, 19, 5)); y += 6

# Row 4: GPU utilization split by tier
panels.append(row("GPU utilization by tier (which fleet does the work in each lane)", y)); y += 1
panels.append(ts("Batch tier GPU utilization (%)", [('avg(DCGM_FI_DEV_GPU_UTIL{exported_pod=~"vllm-llama3-batch.*"})', "batch avg")], y, 0, 12, 8, "percent"))
panels.append(ts("Interactive tier GPU utilization (%)", [('avg(DCGM_FI_DEV_GPU_UTIL{exported_pod=~"vllm-llama3-interactive.*"})', "interactive avg")], y, 12, 12, 8, "percent")); y += 8
panels.append(ts("Per-GPU utilization (%)", [("DCGM_FI_DEV_GPU_UTIL", "{{hostname}} gpu{{gpu}} {{exported_pod}}")], y, 0, 24, 8, "percent")); y += 8

dash = {"uid": "inference-request-flow", "title": "Inference-Aware EKS: Request Flow",
        "tags": ["inference", "flow", "reconciliation"], "timezone": "utc", "schemaVersion": 39,
        "time": {"from": "now-30m", "to": "now"}, "refresh": "10s",
        "templating": {"list": []}, "annotations": {"list": []}, "panels": panels}

with open(os.path.join(HERE, "request-flow.json"), "w") as fh:
    json.dump(dash, fh, indent=2)
cm = {"apiVersion": "v1", "kind": "ConfigMap",
      "metadata": {"name": "request-flow-dashboard", "namespace": "monitoring", "labels": {"grafana_dashboard": "1"}},
      "data": {"request-flow.json": json.dumps(dash)}}
with open(os.path.join(HERE, "request-flow-configmap.json"), "w") as fh:
    json.dump(cm, fh, indent=2)
print("wrote request-flow.json and request-flow-configmap.json")
