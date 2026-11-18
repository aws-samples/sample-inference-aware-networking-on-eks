#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# Reproducible wrapper: port-forward Prometheus, then assert every KEDA Prometheus
# query returns a non-empty vector (the empty-vector trap). Run with both tiers warm
# (before the batch scale-to-zero scaler idles the batch pod, which would legitimately
# empty its query).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${ROOT}"
kubectl -n monitoring port-forward svc/kps-kube-prometheus-stack-prometheus 9090:9090 >/tmp/prom-pf.log 2>&1 &
PF=$!
trap 'kill ${PF} 2>/dev/null || true' EXIT
sleep 8
PROM=http://localhost:9090 bash tests/smoke/test_keda_queries.sh
