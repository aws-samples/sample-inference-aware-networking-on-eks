#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# LT-11 t3: the whole interaction curve in ONE session on ONE pinned topology.
#
# WHY. After the rho 0.80 confirmation, exactly one point of the sign flip was measured on a single
# topology. The rho 1.00, 1.20 and 1.60 points still compared (A-B) from 2026-07-26 (one g6.12xlarge)
# against (C-D) from 2026-07-27 (four g6.xlarge). That cannot be patched by reusing cells, because the
# confirmation measured that the inference-aware arms do NOT reproduce across sessions: the Endpoint
# Picker's group-to-replica assignment moved (max concentration 0.298 to 0.689) and the interleaved
# timing moved (B +0.1454), while the cache-blind arms reproduced to within 0.003. So every cell in the
# curve has to come from one session on one topology. That is what this run does.
#
# SCOPE. All four arms at the four non-degenerate rho points, two reps: 32 cells.
#   A = epp grouped, B = epp interleaved, C = rr grouped, D = rr interleaved
#   rho 0.80, 1.00, 1.20, 1.60 via PREFIX_REPS 67, 84, 101, 135 (verified against rho_map.txt: those
#   give rho 0.7998, 1.0011, 1.2025, 1.6051 at C = 30736).
# rho 2.54 stays the LT-8 anchor: both interleaved arms are floored at 0 there, the interaction test is
# degenerate and topology-independent, so it is NOT re-run.
# Optional extra points via EXTRA_RHOS (for example 0.9) to locate the crossover more tightly.
#
# TOPOLOGY PIN (new, mandatory). The batch Deployment is family-pinned to g6 but NOT size-pinned, which
# is why Karpenter picked g6.xlarge. This run pins node.kubernetes.io/instance-type=g6.xlarge so the
# whole curve is one topology and Karpenter cannot substitute g6.12xlarge. Test-only, reverted at
# teardown by removing that one nodeSelector key.
#
# THREE GATES, all must pass before any cell runs:
#   1/ C re-derived on a live pod equals 30736 (every L depends on it).
#   2/ rr-grouped probe SPREADS (max concentration below 0.5) and reads near 1 - N/R = 0.667.
#   3/ epp-grouped probe reaches the cache-locality regime (hit above 0.8, near 1 - 1/R = 0.917), and
#      epp hit minus rr hit exceeds 0.15, which is what proves the two arms reach DIFFERENT backends.
#      Gate 3 is new: gate 2 alone cannot catch the case where both arms read the same backend.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1
[ -f .lt11-creds.env ] && source .lt11-creds.env
NS=inference
OUT=loadtest/results/lt11
LOG="$OUT/run_log_t3.txt"
mkdir -p "$OUT"
exec > >(tee -a "$LOG") 2>&1

P=52; R=12; MAXTOK=64; CONC=16; REPS="${REPS:-2}"; N_REPLICAS=4
RHOS="${RHOS:-0.8 1.0 1.2 1.6} ${EXTRA_RHOS:-}"
PIN_TYPE=g6.xlarge

reps_for() { case "$1" in
  0.8) echo 67;; 0.9) echo 76;; 1.0) echo 84;; 1.2) echo 101;; 1.6) echo 135;; *) echo "";; esac; }

echo "===================== LT-11 t3 single-session run $(date -u '+%Y-%m-%dT%H:%M:%SZ') ====================="

# Teardown of the test-only resources this run applies (the rr route and Service, and the
# instance-type topology pin). Registered BEFORE the first mutation and fired on ANY exit,
# including a mid-run gate failure, so nothing is left behind to perturb a later run. A
# leftover lt8-batch-rr-route from an earlier run is exactly the fallthrough this guards.
cleanup() {
  echo "== teardown: remove the test-only rr route/Service and revert the topology pin =="
  kubectl delete -f loadtest/lt8-batch-route.yaml --ignore-not-found >/dev/null 2>&1 || true
  kubectl delete -f loadtest/lt8-batch-rr-service.yaml --ignore-not-found >/dev/null 2>&1 || true
  kubectl -n "$NS" patch deploy vllm-llama3-batch --type merge \
    -p '{"spec":{"template":{"spec":{"nodeSelector":{"node.kubernetes.io/instance-type":null}}}}}' >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "== pin the topology to $PIN_TYPE (test-only; revert at teardown) =="
kubectl -n $NS patch deploy vllm-llama3-batch --type merge \
  -p "{\"spec\":{\"template\":{\"spec\":{\"nodeSelector\":{\"node.kubernetes.io/instance-type\":\"$PIN_TYPE\"}}}}}" >/dev/null
kubectl -n $NS get deploy vllm-llama3-batch -o jsonpath='{.spec.template.spec.nodeSelector}{"\n"}'

echo "== the rr route and Service must exist (idempotent; their absence caused the original fallthrough) =="
kubectl apply -f loadtest/lt8-batch-rr-service.yaml >/dev/null
kubectl apply -f loadtest/lt8-batch-route.yaml >/dev/null

echo "== provision: KEDA pinned at $N_REPLICAS, batch serving and its Endpoint Picker up =="
kubectl -n $NS annotate scaledobject vllm-llama3-batch autoscaling.keda.sh/paused-replicas="$N_REPLICAS" --overwrite >/dev/null
kubectl -n $NS scale deploy/vllm-llama3-batch --replicas=$N_REPLICAS >/dev/null
kubectl -n $NS scale deploy/vllm-llama3-batch-epp --replicas=2 >/dev/null
kubectl -n $NS rollout status deploy/vllm-llama3-batch-epp --timeout=300s | tail -1
echo "  waiting for $N_REPLICAS batch pods Ready (model load takes minutes)"
kubectl -n $NS wait --for=condition=Ready pod -l app=vllm-llama3,tier=batch --timeout=1200s | tail -1

echo "== GATE 1: C must equal 30736 on this topology =="
POD=$(kubectl -n $NS get pod -l app=vllm-llama3,tier=batch --field-selector=status.phase=Running -o jsonpath='{.items[0].metadata.name}')
NGB=$(kubectl -n $NS exec "$POD" -- python3 -c "import urllib.request;print([l for l in urllib.request.urlopen('http://localhost:8000/metrics',timeout=10).read().decode().splitlines() if 'cache_config_info' in l and not l.startswith('#')][0])" 2>/dev/null | grep -oE 'num_gpu_blocks="[0-9]+"' | grep -oE '[0-9]+')
C=$((NGB*16)); echo "  num_gpu_blocks=$NGB C=$C"
[ "$C" = "30736" ] || { echo "STOP (escalation 1): C=$C != 30736; the L table is invalid." >&2; exit 1; }

echo "== topology evidence (this is the variable being controlled) =="
kubectl get nodes -L node.kubernetes.io/instance-type --no-headers | awk '{print $1, $NF}' > "$OUT/topology_t3_nodes.txt"
kubectl -n $NS get pods -l app=vllm-llama3,tier=batch -o wide --no-headers | awk '{print $1, $7}' > "$OUT/topology_t3_pods.txt"
sed 's/^/    /' "$OUT/topology_t3_nodes.txt"
sed 's/^/    /' "$OUT/topology_t3_pods.txt"
NTYPES=$(awk '{print $2}' "$OUT/topology_t3_nodes.txt" | grep -c "^${PIN_TYPE}$")
echo "  $PIN_TYPE nodes: $NTYPES"
[ "$NTYPES" = "4" ] || { echo "STOP (escalation 2): expected 4 $PIN_TYPE nodes, found $NTYPES. A heterogeneous topology reintroduces the confound this run removes." >&2; exit 1; }

GW="${GW:-http://$(kubectl -n $NS get gateway inference-gateway -o jsonpath='{.status.addresses[0].value}')}"
[ "$GW" = "http://" ] && { echo "STOP: gateway has no external address" >&2; exit 1; }
echo "== gateway $GW =="

probe() { # arm outfile
  ./.venv/bin/python loadtest/lt8_direct.py --cell PF --arm "$1" --mode grouped \
    --selector app=vllm-llama3,tier=batch --gateway "$GW" --groups 4 --reqs 12 \
    --prefix-reps 67 --salt "pf-$1-t3-$(date +%s)" --max-tokens 32 --conc 16 --out "$2" >/dev/null
  ./.venv/bin/python -c "import json;d=json.load(open('$2'));print(d['prefix_cache_hit_rate'],d['max_concentration_fraction'])"
}

echo "== GATE 2: rr-grouped must be cache-blind (spread, hit near 0.667) =="
read RRHIT RRCONC <<<"$(probe rr "$OUT/preflight_rr_t3.json")"
echo "  rr: hit=$RRHIT conc=$RRCONC"
./.venv/bin/python -c "import sys;sys.exit(0 if ($RRHIT is not None and $RRHIT<0.8 and $RRCONC<0.5) else 1)" \
  || { echo "STOP (escalation 3): rr arm is not cache-blind; the rr route is not routing." >&2; exit 1; }

echo "== GATE 3: epp-grouped must reach the cache-locality regime, and differ from rr =="
read EPHIT EPCONC <<<"$(probe epp "$OUT/preflight_epp_t3.json")"
echo "  epp: hit=$EPHIT conc=$EPCONC"
./.venv/bin/python -c "import sys;sys.exit(0 if ($EPHIT is not None and $EPHIT>0.8 and ($EPHIT-$RRHIT)>0.15) else 1)" \
  || { echo "STOP (escalation 3): epp arm did not separate from rr (epp $EPHIT vs rr $RRHIT); the two arms may be reaching the same backend." >&2; exit 1; }
echo "  GATES PASS: rr $RRHIT versus epp $EPHIT, separation $(./.venv/bin/python -c "print(round($EPHIT-$RRHIT,4))")"

cell() { # arm mode label rho
  local arm="$1" mode="$2" lbl="$3" rho="$4" reps rep out
  reps=$(reps_for "$rho")
  [ -z "$reps" ] && { echo "  skip rho=$rho (no PREFIX_REPS mapping)"; return; }
  for rep in $(seq 1 "$REPS"); do
    out="$OUT/cell_${lbl}_rho${rho}_t3_r${rep}.json"
    echo ">> t3 ${lbl} ($arm/$mode) rho=$rho reps=$reps rep=$rep -> $out"
    ./.venv/bin/python loadtest/lt8_direct.py --cell "${lbl}" --arm "$arm" --mode "$mode" \
      --selector app=vllm-llama3,tier=batch --gateway "$GW" --groups "$P" --reqs "$R" \
      --prefix-reps "$reps" --salt "lt11-${lbl}-rho${rho}-t3-r${rep}" \
      --max-tokens "$MAXTOK" --conc "$CONC" --out "$out" \
      || echo "  ${lbl} rho=$rho rep=$rep FAILED (recorded, not retried)"
  done
}

for rho in $RHOS; do
  [ -z "$rho" ] && continue
  echo "===== rho $rho ====="
  cell epp grouped     A "$rho"
  cell epp interleaved B "$rho"
  cell rr  grouped     C "$rho"
  cell rr  interleaved D "$rho"
done

echo "== DONE $(date -u '+%Y-%m-%dT%H:%M:%SZ') =="
echo "== summary =="
SUFFIX=_t3 ./.venv/bin/python loadtest/lt11_summarize.py || true
