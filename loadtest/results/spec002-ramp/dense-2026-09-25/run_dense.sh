#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# Dense rho sweep, EPP interleaved arm only. Same driver, same operating point, same formula as
# run_lt11.sh; the round-robin arm is dropped because the curve-versus-step question is about the
# arm that collapses, and the wall-clock release at 12:20 does not fit 28 cells.
set -uo pipefail
cd /path/to/inference-aware-eks || exit 1
NS=inference; OUT="loadtest/results/spec002-ramp/dense-2026-09-25"
# shellcheck disable=SC2034  # JOBS = P x R, recorded beside the inputs it derives from
JOBS=624
P=52; C=24064; TOK=28; MAXTOK=64; CONC=16
GW="http://$(kubectl -n envoy-gateway-system get svc -o name | grep envoy-inference | head -1 | sed 's|service/||').envoy-gateway-system.svc.cluster.local"
reps_for() { ./.venv/bin/python -c "print(round(($1*$C*4/52-7)/$TOK))"; }
for rho in 0.9 0.95 1.0 1.05 1.1 1.2 1.4; do
  reps=$(reps_for "$rho")
  for rep in 1 2; do
    out="$OUT/cell_B_rho${rho}_r${rep}.json"
    [ -f "$out" ] && { echo ">> skip $out (already recorded)"; continue; }
    echo ">> DENSE B epp/interleaved rho=$rho reps=$reps rep=$rep -> $out"
    kubectl -n $NS exec loadtest-driver -- env POD_ENDPOINTS="$POD_ENDPOINTS" LOADTEST_MODEL="Qwen/Qwen3-8B" \
      python3 /scripts/lt8_direct.py --cell B --arm epp --mode interleaved \
      --selector app=vllm-llama3,tier=batch --gateway "$GW" --groups $P --reqs 12 \
      --prefix-reps "$reps" --salt "dense-B-rho${rho}-r${rep}" --max-tokens $MAXTOK --conc $CONC \
      --out /tmp/c.json && kubectl -n $NS exec loadtest-driver -- cat /tmp/c.json > "$out" \
      || echo "  FAILED rho=$rho rep=$rep (recorded, not retried)"
  done
done
echo ">> DENSE SWEEP COMPLETE"
