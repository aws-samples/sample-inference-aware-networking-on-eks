#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# LT-10: confirm the SHIPPED queue path (submit API, Amazon SQS, worker, gateway, Endpoint Picker)
# reproduces the LT-8 effect. LT-8 drove the arrival pattern directly at the gateway to isolate the
# router; LT-10 drives it through the full async front door. The claim is the SIGN, not the magnitude:
# LT-10 passes if the grouped cell beats the interleaved cell on prefix-cache hit rate in the same
# direction LT-8 measured. Magnitude is EXPECTED to differ (queue pacing, worker poll timing,
# drain-detected grouping is stricter). If the sign does not match, that is thesis-changing: stop and
# report (escalation), do not tune.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1
# Respect caller credentials. Only fall back to the named profile when the caller
# supplied neither a profile nor explicit keys, so temporary session credentials work.
if [ -z "${AWS_PROFILE:-}" ] && [ -z "${AWS_ACCESS_KEY_ID:-}" ]; then
  export AWS_PROFILE=eksinference
fi
NS=inference
OUT=loadtest/results/lt10
CAP="$OUT/capture"
mkdir -p "$OUT" "$CAP"

# --- operating point: MUST equal LT-8 exactly (read from LT-8's files, not re-tuned) ---
P="${P:-52}"; PREFIX_REPS="${PREFIX_REPS:-214}"; JOBS="${JOBS:-624}"; MAXTOK="${MAXTOK:-64}"
N_REPLICAS="${N_REPLICAS:-4}"; WORKER_REPLICAS="${WORKER_REPLICAS:-8}"; REPS="${REPS:-2}"; DRAIN_TIMEOUT="${DRAIN_TIMEOUT:-1800}"
LT8=loadtest/results/lt8/predictions.json
echo "=== operating-point match assertion (LT-10 must equal LT-8) ==="
match() { # key mine
  local lt8v; lt8v=$(./.venv/bin/python -c "import json;print(json.load(open('$LT8'))['derived']['$1'])")
  [ "$lt8v" = "$2" ] || { echo "OPERATING-POINT MISMATCH: $1 LT-8=$lt8v LT-10=$2 (escalate: comparability is the whole point)" >&2; exit 1; }
  echo "  match $1 = $2"
}
match P_distinct_prefixes "$P"; match prefix_reps "$PREFIX_REPS"; match JOBS_total "$JOBS"; match N_replicas "$N_REPLICAS"
echo "  max_tokens=$MAXTOK (LT-8 run_lt8_direct MAXTOK=64), model Llama-3.1-8B (both drivers), temp 0, cold cache via unique salted prefixes (same as LT-8)"
echo "{\"P\":$P,\"prefix_reps\":$PREFIX_REPS,\"JOBS\":$JOBS,\"max_tokens\":$MAXTOK,\"N_replicas\":$N_REPLICAS,\"worker_replicas\":$WORKER_REPLICAS,\"reps\":$REPS,\"matches_lt8\":true}" > "$OUT/operating_point_match.json"

# --- preflight: deployed image must not be stale (async-worker and submit-api same image) ---
_wimg=$(kubectl -n "$NS" get deploy async-worker -o jsonpath='{.spec.template.spec.containers[0].image}' 2>/dev/null)
_simg=$(kubectl -n "$NS" get deploy submit-api  -o jsonpath='{.spec.template.spec.containers[0].image}' 2>/dev/null)
echo ">> preflight images: async-worker=$_wimg submit-api=$_simg"
[ -n "$_wimg" ] && [ "$_wimg" = "$_simg" ] || { echo "PREFLIGHT FAIL: images differ/unset; the submit API must be the version that stamps submitted_at. Redeploy." >&2; exit 1; }

DLQ_URL="$(aws sqs get-queue-url --queue-name inference-dlq --query QueueUrl --output text 2>/dev/null || echo '')"

# --- controls: suspend KEDA on BOTH batch and async-worker; pin replicas; SQS_BATCH_SIZE=1 ---
suspend() {
  kubectl -n $NS annotate scaledobject vllm-llama3-batch autoscaling.keda.sh/paused-replicas="$N_REPLICAS" --overwrite
  kubectl -n $NS annotate scaledobject async-worker      autoscaling.keda.sh/paused-replicas="$WORKER_REPLICAS" --overwrite
  kubectl -n $NS scale deploy/vllm-llama3-batch --replicas="$N_REPLICAS"
  kubectl -n $NS rollout status deploy/vllm-llama3-batch --timeout=600s
  kubectl -n $NS set env deploy/async-worker SQS_BATCH_SIZE=1 PUB_ARM- HTTP_TIMEOUT_SECONDS- CLAIM_TTL_SECONDS-  # confirm shipped defaults
  kubectl -n $NS scale deploy/async-worker --replicas="$WORKER_REPLICAS"
  kubectl -n $NS rollout status deploy/async-worker --timeout=300s
  echo ">> SQS_BATCH_SIZE (deployed): $(kubectl -n $NS get deploy async-worker -o jsonpath='{range .spec.template.spec.containers[0].env[*]}{.name}={.value} {end}' | tr ' ' '\n' | grep SQS_BATCH_SIZE || echo 'default(1)')"
}
restore() {
  kubectl -n $NS annotate scaledobject vllm-llama3-batch autoscaling.keda.sh/paused-replicas- 2>/dev/null || true
  kubectl -n $NS annotate scaledobject async-worker      autoscaling.keda.sh/paused-replicas- 2>/dev/null || true
  kill $ACCESS_PID $EPP_PID $EPPPF 2>/dev/null || true
}
ACCESS_PID=""; EPP_PID=""; EPPPF=""
trap restore EXIT

suspend

# --- H4 captures for the whole window (whether or not a stall occurs; absence is evidence) ---
EPOD=$(kubectl -n envoy-gateway-system get pod -l gateway.envoyproxy.io/owning-gateway-name=inference-gateway -o jsonpath='{.items[0].metadata.name}')
kubectl -n envoy-gateway-system logs -f "$EPOD" -c envoy --since=1s > "$CAP/envoy-access.log" 2>/dev/null &
ACCESS_PID=$!
# Endpoint Picker metrics via port-forward (the EPP container is distroless, no in-pod shell). Port
# 9090 is the EPP metrics port. Snapshot inference/latency/error/in-flight series every 20s.
kubectl -n $NS port-forward svc/vllm-llama3-batch-epp 9090:9090 >/dev/null 2>&1 & EPPPF=$!; sleep 3
( while true; do
    ts=$(date +%s)
    curl -s http://localhost:9090/metrics 2>/dev/null | grep -iE "inference_|latency|error|in_flight|running|waiting" | sed "s/^/$ts /" >> "$CAP/epp-metrics.txt"
    sleep 20
  done ) &
EPP_PID=$!

SUBMIT_URL_LOCAL=""
kubectl -n $NS port-forward svc/submit-api 8080:8080 >/dev/null 2>&1 & PF=$!; sleep 4
SUBMIT_URL_LOCAL="http://localhost:8080/submit"

reset_cache() { echo ">> cold cache via unique per-cell salt (lt8_cell.py sets SALT=tag); replicas stay loaded"; }
assert_n() {
  local n; n=$(kubectl -n $NS get pod -l app=vllm-llama3,tier=batch --field-selector=status.phase=Running -o name | wc -l | tr -d ' ')
  [ "$n" -ge "$N_REPLICAS" ] || { echo "GATE FAIL: batch replicas $n < $N_REPLICAS" >&2; }
  echo "  batch replicas visible: $n"
}

run_cell() { # cell mode
  local cell="$1" mode="$2" rep out
  for rep in $(seq 1 "$REPS"); do
    reset_cache; assert_n
    out="$OUT/cell_${cell}_r${rep}.json"
    echo ">> LT-10 cell $cell ($mode) rep $rep -> $out"
    ./.venv/bin/python loadtest/lt8_cell.py --cell "$cell" --arm epp --mode "$mode" \
      --selector app=vllm-llama3,tier=batch --tag "lt10-${cell}-r${rep}" \
      --groups "$P" --n "$JOBS" --prefix-reps "$PREFIX_REPS" --max-tokens "$MAXTOK" \
      --submit-url "$SUBMIT_URL_LOCAL" --dlq-url "$DLQ_URL" --drain-timeout "$DRAIN_TIMEOUT" \
      --out "$out" || echo "  cell $cell rep $rep FAILED (recorded, not retried)"
    assert_n
  done
}

run_cell Aprime grouped
run_cell Bprime interleaved

kill $PF 2>/dev/null

echo "=== LT-10 summary: sign vs LT-8 ==="
./.venv/bin/python - "$OUT" <<'PY'
import glob, json, sys, statistics
out=sys.argv[1]
def hit(c):
    xs=[json.load(open(f)).get("prefix_cache_hit_rate") for f in sorted(glob.glob(f"{out}/cell_{c}_r*.json"))]
    return [x for x in xs if x is not None]
A=hit("Aprime"); B=hit("Bprime")
mA=round(statistics.mean(A),4) if A else None; mB=round(statistics.mean(B),4) if B else None
print("LT-10 grouped(A') mean hit:", mA, "| interleaved(B') mean hit:", mB)
print("LT-8 reference: grouped 0.9135 > interleaved 0.000 (COMPLEMENTS)")
if mA is not None and mB is not None:
    print("LT-10 sign:", "grouped > interleaved (MATCHES LT-8)" if mA>mB else "grouped <= interleaved (DOES NOT MATCH: escalate, thesis-changing)")
PY
echo ">> per-cell JSON in $OUT/ ; captures in $CAP/ ; fill LT-10-REPORT.md"
