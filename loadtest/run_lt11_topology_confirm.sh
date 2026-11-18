#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# LT-11 topology confirmation at rho 0.80 (4 cells).
#
# WHY. In LT-11 as reported, cells A and B (Endpoint Picker) come from the 2026-07-26 pass on one
# g6.12xlarge (4x L4 on one node) while cells C and D (round-robin) come from the 2026-07-27 re-run on
# 4x g6.xlarge (one L4 per node). Each difference is internally single-topology ((A-B) all old,
# (C-D) all new), so the interaction test compares one number from each topology. That is robust where
# the margin is large (rho 1.0, 1.2, 1.6: margins 0.21 to 0.24) but FRAGILE at rho 0.80, where the
# margin is only 0.0519 and the arm that could bias it (D, interleaved) is the timing-sensitive one.
#
# WHAT THIS RUN DOES. Re-runs A and B at rho 0.80 ONLY, on the CURRENT topology, two reps each.
# Primary purpose: measure the topology effect on the interleaved arm, which is the one quantity
# currently unbounded (new B versus old B). Secondary: settle the rho 0.80 interaction sign on a
# single topology, since C and D at rho 0.80 already exist on this topology.
#
# Outputs are written as cell_{A,B}_rho0.8_t2_r{1,2}.json (t2 = second topology) so the original
# 2026-07-26 A and B cells are NOT overwritten. Both sets are kept and compared.
#
# Controls identical to LT-11: P=52, R=12, total 624, N=4, C=30736, prefix_reps 67, max_tokens 64,
# conc 16, same driver (lt8_direct.py), same salt scheme with a t2 marker.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1
[ -f .lt11-creds.env ] && source .lt11-creds.env
NS=inference
OUT=loadtest/results/lt11
REPS="${REPS:-2}"
P=52; R=12; PREFIX_REPS=67; MAXTOK=64; CONC=16; RHO=0.8

GW="${GW:-http://$(kubectl -n $NS get gateway inference-gateway -o jsonpath='{.status.addresses[0].value}')}"
[ "$GW" = "http://" ] && { echo "STOP: gateway has no external address" >&2; exit 1; }
echo "== gateway $GW =="

echo "== the rr route and Service must exist for the rr arm (idempotent apply) =="
kubectl apply -f loadtest/lt8-batch-rr-service.yaml >/dev/null
kubectl apply -f loadtest/lt8-batch-route.yaml >/dev/null

echo "== escalation gate 1: C must equal 30736 on this topology =="
POD=$(kubectl -n $NS get pod -l app=vllm-llama3,tier=batch --field-selector=status.phase=Running -o jsonpath='{.items[0].metadata.name}')
NGB=$(kubectl -n $NS exec "$POD" -- python3 -c "import urllib.request;print([l for l in urllib.request.urlopen('http://localhost:8000/metrics',timeout=10).read().decode().splitlines() if 'cache_config_info' in l and not l.startswith('#')][0])" 2>/dev/null | grep -oE 'num_gpu_blocks="[0-9]+"' | grep -oE '[0-9]+')
C=$((NGB*16)); echo "  num_gpu_blocks=$NGB C=$C"
[ "$C" = "30736" ] || { echo "STOP (escalation 1): C=$C != 30736; rho would be undefined." >&2; exit 1; }

echo "== record the instance topology actually used (this is the variable under test) =="
kubectl -n $NS get pods -l app=vllm-llama3,tier=batch -o json > "$OUT/topology_t2_pods.json" 2>/dev/null
kubectl get nodes -L node.kubernetes.io/instance-type --no-headers 2>/dev/null | awk '{print $1, $NF}' > "$OUT/topology_t2_nodes.txt"
echo "  nodes:"; sed 's/^/    /' "$OUT/topology_t2_nodes.txt"

echo "== preflight: the EPP arm must CO-LOCATE (this run is the EPP arm, so gate on it) =="
./.venv/bin/python loadtest/lt8_direct.py --cell PF --arm epp --mode grouped \
  --selector app=vllm-llama3,tier=batch --gateway "$GW" --groups 4 --reqs 12 \
  --prefix-reps "$PREFIX_REPS" --salt "pf-epp-t2-$(date +%s)" --max-tokens 32 --conc 16 \
  --out "$OUT/preflight_epp_t2.json"
PFHIT=$(./.venv/bin/python -c "import json;print(json.load(open('$OUT/preflight_epp_t2.json'))['prefix_cache_hit_rate'])")
echo "  preflight epp: hit=$PFHIT (want well above 0.8; ~0.667 would mean the EPP arm is NOT co-locating)"
./.venv/bin/python -c "import sys;h=$PFHIT;sys.exit(0 if (h is not None and h>0.8) else 1)" \
  || { echo "STOP: the epp arm is not co-locating; check the InferencePool and the EPP deployment." >&2; exit 1; }
echo "  PREFLIGHT PASS"

cell() { # arm mode label
  local arm="$1" mode="$2" lbl="$3" rep out
  for rep in $(seq 1 "$REPS"); do
    out="$OUT/cell_${lbl}_rho${RHO}_t2_r${rep}.json"
    echo ">> LT-11-T2 ${lbl} ($arm/$mode) rho=$RHO rep=$rep -> $out"
    ./.venv/bin/python loadtest/lt8_direct.py --cell "${lbl}" --arm "$arm" --mode "$mode" \
      --selector app=vllm-llama3,tier=batch --gateway "$GW" --groups "$P" --reqs "$R" \
      --prefix-reps "$PREFIX_REPS" --salt "lt11-${lbl}-rho${RHO}-t2-r${rep}" \
      --max-tokens "$MAXTOK" --conc "$CONC" --out "$out" \
      || echo "  ${lbl} rep=$rep FAILED (recorded, not retried)"
  done
}

echo "== A (epp grouped) and B (epp interleaved) at rho 0.80 on the current topology =="
cell epp grouped A
cell epp interleaved B
echo "== DONE =="
