#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# LT-8 driven directly through the gateway (see loadtest/lt8_direct.py header for why). Runs the full
# 2x2 x REPS, reports the interaction sign AS MEASURED (no pre-committed direction). KEDA is already
# suspended and N pinned outside this script; each cell uses unique salted prefixes (cold cache).
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1
OUT=loadtest/results/lt8
P="${P:-52}"; R="${R:-12}"; PREFIX_REPS="${PREFIX_REPS:-214}"; CONC="${CONC:-16}"; MAXTOK="${MAXTOK:-64}"; REPS="${REPS:-2}"

kubectl -n envoy-gateway-system port-forward svc/envoy-inference-inference-gateway-2e466d4a 8888:80 >/dev/null 2>&1 &
PF=$!; trap 'kill $PF 2>/dev/null' EXIT; sleep 4
GW="http://localhost:8888"

cell() { # $1 cell $2 arm $3 mode
  local c="$1" arm="$2" mode="$3" rep
  for rep in $(seq 1 "$REPS"); do
    echo ">> cell $c ($arm/$mode) rep $rep"
    ./.venv/bin/python loadtest/lt8_direct.py --cell "$c" --arm "$arm" --mode "$mode" \
      --selector app=vllm-llama3,tier=batch --gateway "$GW" --groups "$P" --reqs "$R" \
      --prefix-reps "$PREFIX_REPS" --salt "lt8-${c}-r${rep}" --max-tokens "$MAXTOK" --conc "$CONC" \
      --out "$OUT/cell_${c}_r${rep}.json"
  done
}

cell A epp grouped
cell B epp interleaved
cell C rr  grouped
cell D rr  interleaved

echo "=== LT-8 2x2 summary (sign reported as measured) ==="
./.venv/bin/python - "$OUT" <<'PY'
import glob, json, sys, statistics
out = sys.argv[1]
def hr(c):
    xs=[json.load(open(f))["prefix_cache_hit_rate"] for f in sorted(glob.glob(f"{out}/cell_{c}_r*.json"))
        if json.load(open(f)).get("prefix_cache_hit_rate") is not None]
    return xs
H={c:hr(c) for c in "ABCD"}
m={c:(round(statistics.mean(v),4) if v else None) for c,v in H.items()}
print("mean hit rate:", m, " reps:", {c:len(v) for c,v in H.items()})
if all(m[c] is not None for c in "ABCD"):
    ab,cd=m["A"]-m["B"], m["C"]-m["D"]
    print(f"(A-B) grouping benefit under EPP = {ab:+.4f}")
    print(f"(C-D) grouping benefit under RR  = {cd:+.4f}")
    print("SIGN:", "COMPLEMENTS (A-B)>(C-D)" if ab>cd else "SUBSTITUTES (C-D)>(A-B)" if cd>ab else "EQUAL")
PY
