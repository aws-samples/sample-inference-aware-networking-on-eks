#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# Experiment 3, routing four regimes on Qwen3-8B / L40S.
# Runs EXACTLY the operating point fixed in loadtest/results/spec002-regimes/PRE-REGISTRATION.md
# (amendment 1): C measured 170,320, L measured 2,087 at --prefix-reps 130, P = 42 / 5 / 158.
# Nothing here is tuned toward a result; the predictions were committed before the fleet existed.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1
NS=inference
OUT="${OUT:-loadtest/results/spec002-regimes/run-2026-09-24}"; mkdir -p "$OUT"
DRIVER=loadtest-driver-r3
MODEL=Qwen/Qwen3-8B
REPS=2; CONC=8; PREFIX_REPS=130; SEL="app=vllm-llama3,tier=interactive"

POD_ENDPOINTS=$(kubectl -n $NS get pods -l $SEL -o jsonpath='{range .items[*]}{.status.podIP}:8000,{end}' | sed 's/,$//')
echo ">> replicas: $POD_ENDPOINTS"
[ "$(echo $POD_ENDPOINTS | tr ',' '\n' | wc -l | tr -d ' ')" = "4" ] || { echo "STOP (gate 2): need exactly 4 replicas" >&2; exit 1; }
GW="http://$(kubectl -n envoy-gateway-system get svc -o name | grep envoy-inference | head -1 | sed 's|service/||').envoy-gateway-system.svc.cluster.local"
echo ">> gateway: $GW"

cell() { # regime arm groups steady extra...
  local reg="$1" arm="$2" groups="$3" steady="$4"; shift 4
  for rep in $(seq 1 $REPS); do
    local out="$OUT/${reg}_${arm}_rep${rep}.json"
    local seed=$((20260924 + rep))
    echo ">> EXP3 ${reg} ${arm} rep${rep} groups=${groups} steady=${steady}s seed=${seed} -> $out"
    kubectl -n $NS exec "$DRIVER" -- env POD_ENDPOINTS="$POD_ENDPOINTS" \
      python3 /scripts/pub_routing.py --label "${reg}_${arm}_rep${rep}" --arm "$arm" \
        --selector "$SEL" --gateway "$GW" --model "$MODEL" --seed "$seed" \
        --groups "$groups" --prefix-reps "$PREFIX_REPS" --conc "$CONC" --steady "$steady" \
        --salt "exp3-${reg}-${arm}-r${rep}" "$@" --out /tmp/c.json \
      && kubectl -n $NS exec "$DRIVER" -- cat /tmp/c.json > "$out" \
      || echo "  ${reg} ${arm} rep${rep} FAILED (recorded, not retried)"
  done
}

echo "=== PREFLIGHT GATE: the rr arm must be cache-blind (spread), not the Endpoint Picker ==="
kubectl -n $NS exec "$DRIVER" -- env POD_ENDPOINTS="$POD_ENDPOINTS" \
  python3 /scripts/pub_routing.py --label preflight_rr --arm rr --selector "$SEL" --gateway "$GW" \
    --model "$MODEL" --seed 20260924 --groups 4 --prefix-reps 130 --conc 8 --steady 60 \
    --coloc-groups 4 --burst 12 --salt "exp3-pf-$(date +%s)" --out /tmp/pf.json \
  && kubectl -n $NS exec "$DRIVER" -- cat /tmp/pf.json > "$OUT/preflight_rr.json"
PFC=$(./.venv/bin/python -c "import json;print(json.load(open('$OUT/preflight_rr.json'))['mean_colocation_concentration'])")
echo "  preflight rr mean co-location concentration = $PFC (want < 0.5; the Endpoint Picker reads ~1.0)"
./.venv/bin/python -c "import sys;sys.exit(0 if $PFC < 0.5 else 1)" \
  || { echo "STOP (gate 3): the rr arm is not cache-blind; it is being routed. Do NOT run cells." >&2; exit 1; }
echo "  PREFLIGHT PASS"

echo "=== REGIME 1: cold-start replication (P=42, ratio 0.52) ==="
for arm in epp rr; do cell r1_coldstart $arm 42 300; done
echo "=== REGIME 2: control, small working set (P=5, ratio 0.065) ==="
for arm in epp rr; do cell r2_control $arm 5 300; done
echo "=== REGIME 4: prefix churn (reuse=8, R fixed by construction) ==="
for arm in epp rr; do cell r4_churn $arm 42 300 --churn-reuse 8; done
echo "=== REGIME 3: eviction (P=158, ratio 1.93) -- longest, run last ==="
for arm in epp rr; do cell r3_eviction $arm 158 1000; done
echo ">> EXP3 COMPLETE"
