#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# LT-2 routing: prefix-heavy (many distinct prefix groups) run twice at the same rate
# and topology: once through the EPP (gateway) and once through a plain round-robin
# Service over the same 4 interactive pods. Server-side TTFT p90 and prefix-cache
# hit rate are compared over the two time windows. analyze.py lt2 is the arbiter; a null
# or negative result is reported as a finding, not a silent pass.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1
# HISTORICAL, NOT RUNNABLE ON THE CURRENT TOPOLOGY (2026-11-02). The recorded run (2026-07-21)
# drove both arms from outside the cluster through internet-facing load balancers: the gateway's
# own Service and the round-robin Service. The relay cutover (2026-08-04) made the gateway
# ClusterIP, and loadtest/lt2-rr-service.yaml is ClusterIP now, so neither address exists.
# kubectl port-forward is no substitute: it pins every connection to ONE pod, which would turn
# the round-robin arm into a single-replica arm and measure nothing. The supported successor is
# loadtest/run_exp3.sh: both arms enter through the same gateway from the in-cluster driver.
# The body below is kept unchanged as the record of how LT-2 ran.
echo "STOP: LT-2 ran on the pre-relay topology and cannot run on this one. Use loadtest/run_exp3.sh (see this script's header)." >&2
exit 2
# Respect caller credentials. Only fall back to the named profile when the caller
# supplied neither a profile nor explicit keys, so temporary session credentials work.
if [ -z "${AWS_PROFILE:-}" ] && [ -z "${AWS_ACCESS_KEY_ID:-}" ]; then
  export AWS_PROFILE=eksinference
fi
GW="http://$(kubectl -n inference get gateway inference-gateway -o jsonpath='{.status.addresses[0].value}')"
[ "$GW" = "http://" ] && { echo "STOP: gateway has no external address" >&2; exit 1; }
RR="http://$(kubectl -n inference get svc vllm-interactive-rr -o jsonpath='{.status.loadBalancer.ingress[0].hostname}')"
# Unguarded this is worse than a failed run: the EPP arm would succeed, the round-robin
# arm would measure nothing, and analyze.py would compare a real window against an empty one.
[ "$RR" = "http://" ] && { echo "STOP: svc vllm-interactive-rr has no LoadBalancer hostname" >&2; exit 1; }
kubectl -n monitoring port-forward svc/kps-kube-prometheus-stack-prometheus 9090:9090 >/tmp/prom-pf.log 2>&1 &
PF=$!; D=""; trap 'kill $PF ${D:-} 2>/dev/null' EXIT; sleep 6
CONC="${CONC:-24}"; WIN="${WIN:-150}"

echo ">> EPP run (through gateway)"
GATEWAY="$GW" WORKLOAD=prefix-heavy TIER=interactive ROUTER=epp CONCURRENCY=$CONC DURATION=$((WIN+40)) \
  ./.venv/bin/python loadtest/drive_sync.py >/tmp/lt2-epp.out 2>&1 &
D=$!; sleep 40; EPP_S=$(date +%s); sleep $WIN; EPP_E=$(date +%s); kill $D 2>/dev/null; sleep 8

echo ">> round-robin run (through LoadBalancer, no EPP)"
GATEWAY="$RR" WORKLOAD=prefix-heavy TIER=interactive ROUTER=round-robin CONCURRENCY=$CONC DURATION=$((WIN+40)) \
  ./.venv/bin/python loadtest/drive_sync.py >/tmp/lt2-rr.out 2>&1 &
D=$!; sleep 40; RR_S=$(date +%s); sleep $WIN; RR_E=$(date +%s); kill $D 2>/dev/null; sleep 3

echo "=== client views ==="; echo "[EPP]"; cat /tmp/lt2-epp.out; echo "[RR]"; cat /tmp/lt2-rr.out
echo "=== LT-2 verdict (analyze.py) ==="
PROM_URL=http://localhost:9090 ./.venv/bin/python loadtest/analyze.py lt2 \
  --start "$EPP_S" --end "$EPP_E" --flood-start "$RR_S" --flood-end "$RR_E"
echo "WINDOWS epp=$EPP_S..$EPP_E rr=$RR_S..$RR_E"
