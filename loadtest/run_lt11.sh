#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# LT-11: the cliff sweep. Map prefix-cache hit rate against the overflow ratio rho by sweeping the
# prefix length L (holding P=52, R=12, total=624, N=4, C fixed), so rho moves alone and every model
# prediction stays constant. Same direct driver as LT-8 (lt8_direct.py), NOT the queue path (LT-10).
# Pre-registration in loadtest/results/lt11/LT-11-REPORT.md; do not tune toward step or ramp.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1
# Respect caller credentials. Only fall back to the named profile when the caller
# supplied neither a profile nor explicit keys, so temporary session credentials work.
if [ -z "${AWS_PROFILE:-}" ] && [ -z "${AWS_ACCESS_KEY_ID:-}" ]; then
  export AWS_PROFILE=eksinference
fi
NS=inference
OUT="${OUT:-loadtest/results/lt11}"
mkdir -p "$OUT"

# shellcheck disable=SC2034  # JOBS = P x R, recorded beside the inputs it derives from
JOBS=624
P=52; R=12; MAXTOK="${MAXTOK:-64}"; CONC="${CONC:-16}"; REPS="${REPS:-2}"; N_REPLICAS=4

# HARDENING (added 2026-07-28 after the first pass measured the Endpoint Picker in all four cells).
# The round-robin route and Service are labelled MEASUREMENT ONLY / delete at teardown and are NOT part
# of the permanent manifests, so a teardown between runs removes them. With them missing, the
# x-pub-arm=rr header matches no route and falls through to the canonical batch rule (InferencePool plus
# Endpoint Picker): the rr arm then silently measures the Endpoint Picker. Apply them idempotently here
# and gate on observed round-robin behaviour before spending any cell time. See LT-11-REPORT.md.
echo "=== apply the round-robin route and Service (idempotent; required for the rr arm) ==="
kubectl apply -f loadtest/lt8-batch-rr-service.yaml
kubectl apply -f loadtest/lt8-batch-route.yaml
PRIMARY_RHOS="${PRIMARY_RHOS:-0.8 1.0 1.2 1.6}"       # interleaved cliff (the goal)
GROUPED_RHOS="${GROUPED_RHOS:-0.8 1.0 1.2 1.6}"       # grouped flatness (secondary; cut to '0.8 1.6' if budget)

# rho -> PREFIX_REPS, COMPUTED rather than tabulated: round((rho*C*N/P - 7)/TOK_PER_REP).
# The table this replaced (67 84 101 135 for C=30736) is reproduced EXACTLY by this formula, which
# is why the change is safe for the Llama series while deriving the Qwen3 series from the same line.
# Parameterised 2026-09-23 so ONE script serves both series instead of a fork that can drift:
#   EXPECTED_C   the C this run asserts (escalation gate 1). Llama on L4: 30736. Qwen3 on L4: 24064.
#   MODEL_ID     the model the /tokenize call names; a stale literal 404s and reads as a broken pod.
#   TOK_PER_REP  tokens per repetition of the base sentence under THIS model's tokenizer.
#                Measured 2026-09-23: Llama ~27.6, Qwen3 28.0, a 1.4 percent difference, so the
#                operating point moved because C fell, not because the tokenizer changed.
EXPECTED_C="${EXPECTED_C:-30736}"
MODEL_ID="${MODEL_ID:-NousResearch/Meta-Llama-3.1-8B-Instruct}"
TOK_PER_REP="${TOK_PER_REP:-28}"
reps_for() { ./.venv/bin/python -c "print(round(($1*$EXPECTED_C*4/52-7)/$TOK_PER_REP))"; }

echo "=== escalation gate 0: MODEL_ID must equal what the batch Deployment actually serves ==="
# ADDED 2026-09-24. MODEL_ID defaults to the Llama id because the committed Llama cells use this
# same script, so a Qwen3 run that forgets to set it would name a model the pod does not serve.
# vLLM answers that with a 404 per request, the tokenize step below is muted with 2>/dev/null and
# would yield an empty L, and the run would look like a broken pod rather than a stale literal.
# Read live from the Deployment's own --model arg, which is the authority, and STOP on a mismatch.
# Reuses cluster_probe.served_model rather than re-implementing the parse in shell: the first
# attempt at this line used a sed BRE that returned EMPTY on BSD sed, which would have failed the
# gate closed on every run. Four scrapers were single-sourced into cluster_probe for this reason.
SERVED=$(PYTHONPATH=loadtest ./.venv/bin/python -c "import cluster_probe; print(cluster_probe.served_model('vllm-llama3-batch'))" 2>/dev/null)
[ -n "$SERVED" ] || { echo "STOP (escalation 0): could not read the served model from deploy/vllm-llama3-batch." >&2; exit 1; }
[ "$SERVED" = "$MODEL_ID" ] || { echo "STOP (escalation 0): MODEL_ID=$MODEL_ID but the batch tier serves $SERVED. Set MODEL_ID deliberately; do not run a ramp against a model the pods do not serve." >&2; exit 1; }
echo "  MODEL_ID=$MODEL_ID agrees with the served model"

echo "=== escalation gate: re-derive C on the pinned instance, assert == $EXPECTED_C ==="
POD=$(kubectl -n $NS get pod -l app=vllm-llama3,tier=batch --field-selector=status.phase=Running -o jsonpath='{.items[0].metadata.name}')
NGB=$(kubectl -n $NS exec "$POD" -- python3 -c "import urllib.request;print([l for l in urllib.request.urlopen('http://localhost:8000/metrics',timeout=10).read().decode().splitlines() if 'cache_config_info' in l and not l.startswith('#')][0])" 2>/dev/null | grep -oE 'num_gpu_blocks="[0-9]+"' | grep -oE '[0-9]+')
C=$((NGB*16))
echo "  num_gpu_blocks=$NGB  C=$C tokens"
[ "$C" = "$EXPECTED_C" ] || { echo "STOP (escalation 1): C=$C != $EXPECTED_C. Every L depends on it; recompute PREFIX_REPS (or set EXPECTED_C deliberately) before running." >&2; exit 1; }

echo "=== verify L live per rho (tokenize the ACTUAL salted prefix), record measured rho ==="
: > "$OUT/rho_map.txt"; echo "rho_target prefix_reps L_actual rho_actual" >> "$OUT/rho_map.txt"
for rho in 0.8 1.0 1.2 1.6; do
  reps=$(reps_for "$rho")
  L=$(kubectl -n $NS exec -i "$POD" -- python3 - "$reps" "$MODEL_ID" <<'PY' 2>/dev/null
import json,sys,urllib.request
reps=int(sys.argv[1])
base=("The following is a long standing instruction set that all requests in this group share "
      "verbatim so that the prefix cache is populated once and reused. ")
# representative salt matching lt8_direct's format: SharedContext<salt>Group<g>:
s="SharedContext"+"lt11-B-rho"+"XX"+"-r1"+"Group0: "+base*reps
req=urllib.request.Request("http://localhost:8000/tokenize",data=json.dumps({"model":sys.argv[2],"prompt":s}).encode(),headers={"Content-Type":"application/json"})
print(json.load(urllib.request.urlopen(req,timeout=15))["count"])
PY
)
  ra=$(./.venv/bin/python -c "print(round((52/4)*$L/$C,4))" 2>/dev/null)
  echo "  rho_target=$rho reps=$reps L_actual=$L rho_actual=$ra"
  echo "$rho $reps $L $ra" >> "$OUT/rho_map.txt"
done

kubectl -n $NS annotate scaledobject vllm-llama3-batch autoscaling.keda.sh/paused-replicas="$N_REPLICAS" --overwrite >/dev/null 2>&1
kubectl -n $NS scale deploy/vllm-llama3-batch --replicas=$N_REPLICAS >/dev/null 2>&1
kubectl -n $NS rollout status deploy/vllm-llama3-batch --timeout=600s 2>&1 | tail -1
# Reach the gateway via its external ELB directly, NOT a kubectl port-forward: the port-forward tunnel
# dies under the sustained load of a heavy interleaved cell, which corrupted a first attempt (first
# cell partial, every later cell ok=0/queries=0). The ELB is robust (verified: batch route returns 200).
if [ -n "${DRIVER_POD:-}" ]; then
  # In-cluster: address the Envoy Service by DNS. The Gateway's status address is a ClusterIP,
  # which is correct for an in-cluster driver and unreachable from anywhere else.
  GW="http://$(kubectl -n envoy-gateway-system get svc -o name | grep envoy-inference | head -1 | sed 's|service/||').envoy-gateway-system.svc.cluster.local"
else
  GW="http://$(kubectl -n $NS get gateway inference-gateway -o jsonpath='{.status.addresses[0].value}')"
fi
[ "$GW" = "http://" ] && { echo "STOP: gateway has no external address" >&2; exit 1; }
echo ">> gateway: $GW"
# THE PIN IS NOT RELEASED ON EXIT, 2026-09-24. This was a trap that removed paused-replicas on any
# exit, including an abort: on 2026-09-23 that let KEDA scale batch 4 -> 1 and three g6 nodes were
# reclaimed while the campaign still needed them. the maintainer's standing order is that no tier scales to
# zero and no node is released until he declares the campaign complete, so the pin now OUTLIVES the
# script and is removed deliberately at teardown instead.
trap 'echo ">> run ended; KEDA pin paused-replicas=4 DELIBERATELY LEFT IN PLACE (no-release order)"' EXIT

# THE DRIVER RUNS WHERE THE ARRIVALS MUST ORIGINATE (2026-09-23, the maintainer's ruling). With
# DRIVER_POD set, every cell executes INSIDE the cluster: this experiment's independent variable is
# arrival order, and a laptop driver behind a port-forward serialises arrivals through one tunnel
# and measures the tunnel. Today's rebuilt cluster also exposes the gateway as a ClusterIP only, so
# a laptop driver cannot reach it at all. Unset, the behaviour is exactly as before.
# The in-cluster driver holds NO RBAC: POD_ENDPOINTS carries the replica addresses it scrapes, and
# the orchestrator (which already has kubectl) resolves them.
run_driver() { # --out <local path> then lt8_direct args
  local outfile="$2"; shift 2
  if [ -n "${DRIVER_POD:-}" ]; then
    kubectl -n $NS exec "$DRIVER_POD" -- env POD_ENDPOINTS="${POD_ENDPOINTS:-}" \
      LOADTEST_MODEL="${MODEL_ID}" python3 /scripts/lt8_direct.py "$@" --out /tmp/cell.json \
      && kubectl -n $NS exec "$DRIVER_POD" -- cat /tmp/cell.json > "$outfile"
  else
    ./.venv/bin/python loadtest/lt8_direct.py "$@" --out "$outfile"
  fi
}

cell() { # arm mode rho label
  local arm="$1" mode="$2" rho="$3" lbl="$4" reps rep out
  reps=$(reps_for "$rho")
  for rep in $(seq 1 "$REPS"); do
    out="$OUT/cell_${lbl}_rho${rho}_r${rep}.json"
    echo ">> LT-11 ${lbl} ($arm/$mode) rho=$rho reps=$reps rep=$rep -> $out"
    run_driver --out "$out" --cell "${lbl}" --arm "$arm" --mode "$mode" \
      --selector app=vllm-llama3,tier=batch --gateway "$GW" --groups "$P" --reqs "$R" \
      --prefix-reps "$reps" --salt "lt11-${lbl}-rho${rho}-r${rep}" --max-tokens "$MAXTOK" --conc "$CONC" \
      || echo "  ${lbl} rho=$rho rep=$rep FAILED (recorded, not retried)"
  done
}

echo "=== PREFLIGHT GATE: the rr arm must be cache-blind (spread), not the Endpoint Picker ==="
# A round-robin grouped probe must SPREAD across replicas (max concentration < 0.5) and land near
# 1 - N/R = 0.667. An Endpoint Picker arm reads ~0.90 with higher concentration. If this fails the route
# is not routing and NO cell should run (that is the exact defect the first pass shipped).
run_driver --out "$OUT/preflight_rr.json" --cell PF --arm rr --mode grouped \
  --selector app=vllm-llama3,tier=batch --gateway "$GW" --groups 4 --reqs 12 \
  --prefix-reps 67 --salt "pf-rr-$(date +%s)" --max-tokens 32 --conc 16
PFHIT=$(./.venv/bin/python -c "import json;print(json.load(open('$OUT/preflight_rr.json'))['prefix_cache_hit_rate'])")
PFCONC=$(./.venv/bin/python -c "import json;print(json.load(open('$OUT/preflight_rr.json'))['max_concentration_fraction'])")
echo "  preflight rr: hit=$PFHIT maxconc=$PFCONC (want hit<0.8 and maxconc<0.5)"
./.venv/bin/python -c "import sys;h=$PFHIT;c=$PFCONC;sys.exit(0 if (h is not None and h<0.8 and c<0.5) else 1)" \
  || { echo "STOP (escalation): rr preflight looks like the Endpoint Picker; the rr route is not routing. Do NOT run cells." >&2; exit 1; }
echo "  PREFLIGHT PASS: the rr arm reaches the cache-blind Service"

echo "=== PRIMARY: interleaved cliff (B EPP, D RR) ==="
for rho in $PRIMARY_RHOS; do cell epp interleaved "$rho" B; cell rr interleaved "$rho" D; done
echo "=== SECONDARY: grouped flatness (A EPP, C RR) ==="
for rho in $GROUPED_RHOS; do cell epp grouped "$rho" A; cell rr grouped "$rho" C; done

echo "=== LT-11 curve (hit rate vs rho, mean of reps) ==="
./.venv/bin/python - "$OUT" <<'PY'
import glob, json, sys, statistics
out=sys.argv[1]
def m(lbl,rho):
    xs=[json.load(open(f)).get("prefix_cache_hit_rate") for f in sorted(glob.glob(f"{out}/cell_{lbl}_rho{rho}_r*.json"))]
    xs=[x for x in xs if x is not None]
    return round(statistics.mean(xs),4) if xs else None
def Rm(lbl,rho):
    xs=[json.load(open(f)).get("effective_R") for f in sorted(glob.glob(f"{out}/cell_{lbl}_rho{rho}_r*.json"))]
    xs=[x for x in xs if x is not None]
    return round(statistics.mean(xs),1) if xs else None
print(f"{'rho':>5} | {'B epp intl':>10} | {'D rr intl':>10} | {'A epp grp':>10} | {'C rr grp':>9} | R(B)")
for rho in ("0.8","1.0","1.2","1.6"):
    print(f"{rho:>5} | {str(m('B',rho)):>10} | {str(m('D',rho)):>10} | {str(m('A',rho)):>10} | {str(m('C',rho)):>9} | {Rm('B',rho)}")
print(f"{'2.54':>5} | {'0.000':>10} | {'0.000':>10} | {'0.9135':>10} | {'0.6643':>9} | 12  (LT-8, reused)")
print("\nStep vs ramp is a judgment on the B/D columns above (fill the report). Interaction answerable only where B and D are both non-zero.")
PY
echo ">> per-cell JSON + rho_map.txt in $OUT/ ; fill LT-11-REPORT.md"
