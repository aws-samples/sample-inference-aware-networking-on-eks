#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# LT-4 isolation: interactive at a fixed reference rate throughout, batch flooded for
# a sustained window. Measures interactive server-side TTFT p95 (Prometheus) in the
# quiet baseline window vs the flood window. Windows bounded (flood 4 min, not 15) for
# the 3h budget; stated. analyze.py lt4 is the arbiter.
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
PF=$!; IREF=""; FLOOD=""; trap 'kill $PF ${IREF:-} ${FLOOD:-} 2>/dev/null' EXIT; sleep 6

# interactive reference driver for the whole run (baseline + flood)
GATEWAY="$GW" WORKLOAD=interactive-short TIER=interactive CONCURRENCY=10 DURATION=430 \
  ./.venv/bin/python loadtest/drive_sync.py >/tmp/lt4-iref.out 2>&1 &
IREF=$!
echo ">> warm-up 45s"; sleep 45
BASE_START=$(date +%s); echo ">> baseline window 90s"; sleep 90; BASE_END=$(date +%s)

# batch flood
GATEWAY="$GW" WORKLOAD=batch-long TIER=batch CONCURRENCY=40 DURATION=300 \
  ./.venv/bin/python loadtest/drive_sync.py >/tmp/lt4-flood.out 2>&1 &
FLOOD=$!
echo ">> flood warm-up 30s"; sleep 30
FLOOD_START=$(date +%s); echo ">> flood window 240s"; sleep 240; FLOOD_END=$(date +%s)

kill $IREF $FLOOD 2>/dev/null; sleep 3
echo "=== tiers on separate nodes? ==="
kubectl -n inference get pods -l app=vllm-llama3 -o jsonpath='{range .items[*]}{.metadata.labels.tier}{" "}{.spec.nodeName}{"\n"}{end}' | sort | uniq -c
echo "=== interactive reference driver (client view) ===" && cat /tmp/lt4-iref.out
echo "=== LT-4 verdict (analyze.py) ==="
PROM_URL=http://localhost:9090 ./.venv/bin/python loadtest/analyze.py lt4 \
  --start "$BASE_START" --end "$BASE_END" --flood-start "$FLOOD_START" --flood-end "$FLOOD_END" \
  --tol 0.15 --target-ms 2000
echo "WINDOWS base=$BASE_START..$BASE_END flood=$FLOOD_START..$FLOOD_END"
