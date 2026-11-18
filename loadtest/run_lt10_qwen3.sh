#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# LT-10 equivalent for the Qwen3 series: does the SHIPPED QUEUE PATH reproduce the direct-path
# effect experiment 4 measured? The Qwen3 series had no queue-path measurement of any kind until
# this run, so every queue claim in the writeup rested on the Llama series alone.
#
# THE CLAIM IS THE SIGN, NOT THE MAGNITUDE, exactly as LT-10 framed it. Magnitude is expected to
# differ (queue pacing, worker poll timing, drain-detected grouping is stricter). If the sign does
# not match, that is thesis-changing: stop and report, do not tune.
#
# ANCHOR: experiment 4's committed direct-path cells at rho = 1.0, the middle of the ramp.
#   direct grouped (A, epp)      0.8938
#   direct interleaved (B, epp)  0.1116
# so the direct sign is grouped >> interleaved, and this run passes if the queue path agrees.
#
# THE KEDA PINS ARE NOT RELEASED ON EXIT. run_lt10.sh's restore() removed them, which under the
# standing no-release order would let KEDA scale the batch tier down and reclaim held g6 nodes.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1
NS=inference
OUT="${OUT:-loadtest/results/spec002-queue/run-2026-09-24}"; mkdir -p "$OUT"

# Operating point: P, JOBS and N are IDENTICAL to both the Llama LT-8/LT-10 pair and the Qwen3
# ramp. Only PREFIX_REPS moves, because it is the one term that depends on C (24,064 here).
P=52; JOBS=624; N_REPLICAS=4; MAXTOK=64; PREFIX_REPS="${PREFIX_REPS:-66}"   # rho 1.0 for C=24,064
WORKER_REPLICAS="${WORKER_REPLICAS:-8}"; REPS="${REPS:-2}"; DRAIN_TIMEOUT="${DRAIN_TIMEOUT:-1800}"
MODEL_ID="Qwen/Qwen3-8B"
export LOADTEST_MODEL="$MODEL_ID"

echo "=== gate 0: MODEL_ID must equal what the batch tier actually serves ==="
SERVED=$(PYTHONPATH=loadtest ./.venv/bin/python -c "import cluster_probe; print(cluster_probe.served_model('vllm-llama3-batch'))" 2>/dev/null)
[ "$SERVED" = "$MODEL_ID" ] || { echo "STOP (gate 0): serves $SERVED, not $MODEL_ID" >&2; exit 1; }
echo "  served model agrees: $SERVED"

echo "=== gate 1: C on the batch tier must still be 24,064 (the ramp's constant) ==="
POD=$(kubectl -n $NS get pod -l app=vllm-llama3,tier=batch --field-selector=status.phase=Running -o jsonpath='{.items[0].metadata.name}')
NGB=$(kubectl -n $NS exec "$POD" -- python3 -c "import urllib.request,re;raw=urllib.request.urlopen('http://localhost:8000/metrics',timeout=10).read().decode();l=[x for x in raw.splitlines() if 'cache_config_info' in x and not x.startswith('#')][0];print(re.search(r'num_gpu_blocks=\"(\d+)\"',l).group(1))" 2>/dev/null)
[ "$((NGB*16))" = "24064" ] || { echo "STOP (gate 1): C=$((NGB*16)) != 24064" >&2; exit 1; }
echo "  C = $((NGB*16)) tokens"

echo "=== preflight: worker and submit API must share an image (the one that stamps submitted_at) ==="
_w=$(kubectl -n $NS get deploy async-worker -o jsonpath='{.spec.template.spec.containers[0].image}')
_s=$(kubectl -n $NS get deploy submit-api  -o jsonpath='{.spec.template.spec.containers[0].image}')
[ -n "$_w" ] && [ "$_w" = "$_s" ] || { echo "PREFLIGHT FAIL: images differ ($_w vs $_s)" >&2; exit 1; }
echo "  image: $_w"

DLQ_URL="$(aws sqs get-queue-url --queue-name inference-dlq --query QueueUrl --output text 2>/dev/null || echo '')"

echo "=== controls: pin batch at $N_REPLICAS, bring the worker to $WORKER_REPLICAS, SQS_BATCH_SIZE=1 ==="
kubectl -n $NS annotate scaledobject vllm-llama3-batch autoscaling.keda.sh/paused-replicas="$N_REPLICAS" --overwrite
kubectl -n $NS annotate scaledobject async-worker      autoscaling.keda.sh/paused-replicas="$WORKER_REPLICAS" --overwrite
kubectl -n $NS set env deploy/async-worker SQS_BATCH_SIZE=1
kubectl -n $NS scale deploy/async-worker --replicas="$WORKER_REPLICAS"
kubectl -n $NS rollout status deploy/async-worker --timeout=300s 2>&1 | tail -1

kubectl -n $NS port-forward svc/submit-api 8080:8080 >/dev/null 2>&1 &
PF=$!; sleep 5
SUBMIT_URL_LOCAL="http://localhost:8080/submit"
# The pins OUTLIVE this script by design; only the port-forward is cleaned up.
trap 'kill $PF 2>/dev/null; echo ">> run ended; KEDA pins DELIBERATELY LEFT IN PLACE (no-release order)"' EXIT

assert_n() { local n; n=$(kubectl -n $NS get pod -l app=vllm-llama3,tier=batch --field-selector=status.phase=Running -o name | wc -l | tr -d ' '); echo "  batch replicas visible: $n"; }

run_cell() { # cell mode
  local cell="$1" mode="$2" rep out
  for rep in $(seq 1 "$REPS"); do
    assert_n
    out="$OUT/cell_${cell}_r${rep}.json"
    echo ">> QUEUE cell $cell ($mode) rep $rep -> $out"
    ./.venv/bin/python loadtest/lt8_cell.py --cell "$cell" --arm epp --mode "$mode" \
      --selector app=vllm-llama3,tier=batch --tag "q3-${cell}-r${rep}" \
      --groups "$P" --n "$JOBS" --prefix-reps "$PREFIX_REPS" --max-tokens "$MAXTOK" \
      --submit-url "$SUBMIT_URL_LOCAL" --dlq-url "$DLQ_URL" --drain-timeout "$DRAIN_TIMEOUT" \
      --out "$out" || echo "  cell $cell rep $rep FAILED (recorded, not retried)"
  done
}

run_cell Aq grouped
run_cell Bq interleaved

./.venv/bin/python - "$OUT" <<'PY'
import glob,json,sys,statistics as st
out=sys.argv[1]
def m(c):
    xs=[json.load(open(f)).get("prefix_cache_hit_rate") for f in sorted(glob.glob(f"{out}/cell_{c}_r*.json"))]
    xs=[x for x in xs if x is not None]
    return round(st.mean(xs),4) if xs else None
A,B=m("Aq"),m("Bq")
print("QUEUE PATH grouped(Aq):",A," interleaved(Bq):",B)
print("DIRECT PATH, experiment 4 at rho 1.0: grouped 0.8938  interleaved 0.1116")
if A is not None and B is not None:
    print("SIGN:", "grouped > interleaved -> MATCHES the direct path" if A>B
           else "grouped <= interleaved -> DOES NOT MATCH: escalate, thesis-changing")
PY
