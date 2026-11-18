#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# LT-11 RR re-run (repair). The original LT-11 ran with the round-robin route/Service ABSENT, so the
# x-pub-arm=rr header fell through to the canonical batch rule (InferencePool + Endpoint Picker) and
# cells C and D were measured as EPP, not round-robin (see LT-11-REPORT.md, root-cause note). This
# script re-runs ONLY the round-robin cells (C grouped, D interleaved) after the route/Service were
# restored (loadtest/lt8-batch-rr-service.yaml, loadtest/lt8-batch-route.yaml). A and B (EPP) from the
# original run are valid and reused. Controls asserted identical to LT-8/LT-11: P=52, R=12, total=624,
# N=4, C=30736, max-tokens 64, conc 16, same driver (lt8_direct.py). Instance note: this re-run landed
# on 4x g6.xlarge (1 L4 each); the original was 1x g6.12xlarge (4 L4). Per-replica GPU is identical
# (1x L4) and C=30736 was re-verified, so the per-replica measurement is comparable.
#
# Preflight gate (the check that was missing): a small rr-grouped probe must SPREAD (max concentration
# < 0.5, hit ~= 1 - N/R = 0.667), proving the rr arm reaches the plain Service and not the EPP.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1
# Self-source credentials for unattended/background execution (repair run only; file is gitignored).
[ -f .lt11-creds.env ] && source .lt11-creds.env
NS=inference
OUT=loadtest/results/lt11
# Reach the gateway via its external ELB (a kubectl port-forward dies under a heavy interleaved cell).
GW="${GW:-http://$(kubectl -n $NS get gateway inference-gateway -o jsonpath='{.status.addresses[0].value}')}"
[ "$GW" = "http://" ] && { echo "STOP: gateway has no external address" >&2; exit 1; }

echo "== apply the round-robin route and Service (idempotent; the rr arm needs them) =="
kubectl apply -f loadtest/lt8-batch-rr-service.yaml
kubectl apply -f loadtest/lt8-batch-route.yaml
P=52; R=12; MAXTOK=64; CONC=16; REPS=2
reps_for() { case "$1" in 0.8) echo 67;; 1.0) echo 84;; 1.2) echo 101;; 1.6) echo 135;; *) echo "";; esac; }

echo "== gateway $GW =="
echo "== escalation gate: C must be 30736 =="
POD=$(kubectl -n $NS get pod -l app=vllm-llama3,tier=batch --field-selector=status.phase=Running -o jsonpath='{.items[0].metadata.name}')
NGB=$(kubectl -n $NS exec "$POD" -- python3 -c "import urllib.request;print([l for l in urllib.request.urlopen('http://localhost:8000/metrics',timeout=10).read().decode().splitlines() if 'cache_config_info' in l and not l.startswith('#')][0])" 2>/dev/null | grep -oE 'num_gpu_blocks="[0-9]+"' | grep -oE '[0-9]+')
C=$((NGB*16)); echo "  C=$C"; [ "$C" = "30736" ] || { echo "STOP: C=$C != 30736" >&2; exit 1; }

echo "== PREFLIGHT rr-grouped must SPREAD (maxconc<0.5, hit~0.667) =="
./.venv/bin/python loadtest/lt8_direct.py --cell PF --arm rr --mode grouped \
  --selector app=vllm-llama3,tier=batch --gateway "$GW" --groups 4 --reqs 12 \
  --prefix-reps 67 --salt "pf-rr-$(date +%s)" --max-tokens 32 --conc 16 \
  --out "$OUT/preflight_rr.json"
PFHIT=$(./.venv/bin/python -c "import json;print(json.load(open('$OUT/preflight_rr.json'))['prefix_cache_hit_rate'])")
PFCONC=$(./.venv/bin/python -c "import json;print(json.load(open('$OUT/preflight_rr.json'))['max_concentration_fraction'])")
echo "  preflight rr: hit=$PFHIT maxconc=$PFCONC"
./.venv/bin/python -c "import sys;h=$PFHIT;c=$PFCONC;sys.exit(0 if (h is not None and h<0.8 and c<0.5) else 1)" \
  || { echo "STOP: rr preflight looks like EPP (route not routing to Service). Do NOT run cells." >&2; exit 1; }
echo "  PREFLIGHT PASS: rr arm reaches the cache-blind Service"

# Preserve the contaminated originals before overwriting.
mkdir -p "$OUT/superseded_rr_fallthrough"
for f in "$OUT"/cell_C_rho*_r*.json "$OUT"/cell_D_rho*_r*.json; do
  [ -e "$f" ] && cp -n "$f" "$OUT/superseded_rr_fallthrough/$(basename "$f")"
done
echo "== backed up original C/D cells to $OUT/superseded_rr_fallthrough/ =="

cell() { # arm mode rho label
  local arm="$1" mode="$2" rho="$3" lbl="$4" reps rep out
  reps=$(reps_for "$rho")
  for rep in $(seq 1 "$REPS"); do
    out="$OUT/cell_${lbl}_rho${rho}_r${rep}.json"
    echo ">> LT-11-RR ${lbl} ($arm/$mode) rho=$rho reps=$reps rep=$rep -> $out"
    ./.venv/bin/python loadtest/lt8_direct.py --cell "${lbl}" --arm "$arm" --mode "$mode" \
      --selector app=vllm-llama3,tier=batch --gateway "$GW" --groups "$P" --reqs "$R" \
      --prefix-reps "$reps" --salt "lt11-${lbl}-rho${rho}-r${rep}" --max-tokens "$MAXTOK" --conc "$CONC" \
      --out "$out" || echo "  ${lbl} rho=$rho rep=$rep FAILED (recorded, not retried)"
  done
}

echo "== RR cells: C (grouped), D (interleaved) across rho 0.8 1.0 1.2 1.6 =="
for rho in 0.8 1.0 1.2 1.6; do cell rr grouped "$rho" C; cell rr interleaved "$rho" D; done
echo "== DONE =="
