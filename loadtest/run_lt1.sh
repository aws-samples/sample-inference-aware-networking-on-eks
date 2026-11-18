#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# LT-1 baseline: interactive-short at a sustainable steady rate. Server-side TTFT
# percentiles (Prometheus), tokens/sec, GPU utilization (DCGM), client error rate.
# Steady window bounded to 180s for the budget; warm-up discarded. Pass: error rate
# < 0.5% and no unbounded queue growth (waiting stays bounded).
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1
# Respect caller credentials. Only fall back to the named profile when the caller
# supplied neither a profile nor explicit keys, so temporary session credentials work.
if [ -z "${AWS_PROFILE:-}" ] && [ -z "${AWS_ACCESS_KEY_ID:-}" ]; then
  export AWS_PROFILE=eksinference
fi
GW="http://$(kubectl -n inference get gateway inference-gateway -o jsonpath='{.status.addresses[0].value}')"
[ "$GW" = "http://" ] && { echo "STOP: gateway has no external address" >&2; exit 1; }
kubectl -n monitoring port-forward svc/kps-kube-prometheus-stack-prometheus 9090:9090 >/tmp/prom-pf.log 2>&1 &
PF=$!; D=""; trap 'kill $PF ${D:-} 2>/dev/null' EXIT; sleep 6
CONC="${CONC:-20}"
GATEWAY="$GW" WORKLOAD=interactive-short TIER=interactive CONCURRENCY=$CONC DURATION=240 \
  ./.venv/bin/python loadtest/drive_sync.py >/tmp/lt1.out 2>&1 &
D=$!
echo ">> warm-up 45s"; sleep 45
S=$(date +%s); echo ">> steady window 180s (conc=$CONC)"; sleep 180; E=$(date +%s)
kill $D 2>/dev/null; sleep 3
q(){ curl -sG http://localhost:9090/api/v1/query --data-urlencode "query=$1" --data-urlencode "time=$E" | jq -r '.data.result[0].value[1] // "n/a"'; }
D2=$((E-S))
echo "=== LT-1 baseline (server-side over ${D2}s steady window) ==="
for Q in 0.50 0.90 0.99; do
  v=$(q "histogram_quantile($Q, sum by (le) (rate(vllm:time_to_first_token_seconds_bucket{tier=\"interactive\"}[${D2}s])))")
  printf "  TTFT p%s: %s s\n" "${Q#0.}" "$v"
done
echo "  tokens/sec: $(q "sum(rate(vllm:generation_tokens_total{tier=\"interactive\"}[${D2}s]))")"
echo "  GPU util avg %: $(q "avg(DCGM_FI_DEV_GPU_UTIL)")"
echo "  queue waiting (end): $(q "sum(vllm:num_requests_waiting{tier=\"interactive\"})")"
echo "=== client view ===" && cat /tmp/lt1.out
echo "WINDOW $S..$E"
