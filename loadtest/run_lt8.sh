#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# LT-8: are admission control (queue grouping) and cache-aware routing SUBSTITUTES or COMPLEMENTS?
#
# This script pre-commits to NO direction (standard 4). It measures the full 2x2 and reports the
# sign it finds. Async's contribution (exactly-once durability) is measured elsewhere (LT-3, LT-6,
# LT-9); a null LT-8 is not a failed thesis.
#
#   Cell A: Endpoint Picker + GROUPED admission
#   Cell B: Endpoint Picker + INTERLEAVED admission
#   Cell C: round-robin      + GROUPED admission
#   Cell D: round-robin      + INTERLEAVED admission
# Reported: the 2x2 hit rates, the cold-miss-floor result (cell A), the interaction (A-B) vs (C-D),
# predicted vs measured (pre-registered in loadtest/results/lt8/predictions.json), and the gates.
#
# CRITICAL CONFOUND (handled here, not left manual): the batch KEDA ScaledObject scales on
# num_requests_waiting, which is HIGHER under INTERLEAVED, so N would drift UP with the admission
# arm and bias the interaction toward "substitutes" for the wrong reason. So this script SUSPENDS
# KEDA and pins N for the whole run, and lt8_cell.py asserts N>=2 and stable at the start and end of
# every cell.
#
# Before running: derive the batch-tier cache and size P and L (loadtest/results/lt8/
# cache_derivation_batch.md) so both INTERLEAVED cells are in eviction, and PRE-REGISTER predictions.
# Escalate rather than work around if the regime is unreachable, if N cannot be held, or if the four
# cells cannot run under identical conditions (a contaminated 2x2 is worse than none).
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1
# Respect caller credentials. Only fall back to the named profile when the caller
# supplied neither a profile nor explicit keys, so temporary session credentials work.
if [ -z "${AWS_PROFILE:-}" ] && [ -z "${AWS_ACCESS_KEY_ID:-}" ]; then
  export AWS_PROFILE=eksinference
fi
NS=inference
OUT=loadtest/results/lt8
mkdir -p "$OUT"

# --- configuration (fill P, L, N from the batch cache derivation before running) ---
N_REPLICAS="${N_REPLICAS:-4}"          # batch serving replicas, pinned for the whole run
WORKER_REPLICAS="${WORKER_REPLICAS:-4}"# async workers, fixed across all cells
P="${P:?set P (distinct prefixes) from cache_derivation_batch.md}"
JOBS="${JOBS:?set JOBS (total, a multiple of P)}"
PREFIX_REPS="${PREFIX_REPS:?set PREFIX_REPS (controls prefix length L)}"
MAX_TOKENS="${MAX_TOKENS:-64}"
REPS="${REPS:-2}"                      # clean repetitions per cell
DRAIN_TIMEOUT="${DRAIN_TIMEOUT:-1800}"

DLQ_URL="$(aws sqs get-queue-url --queue-name inference-dlq --query QueueUrl --output text 2>/dev/null || echo '')"

# Preflight: the deployed image must not be stale relative to the repository. The async-worker and
# submit-api build from the SAME Dockerfile, so their deployed tags must match; a mismatch means one
# role is running old code and will corrupt records silently (this is exactly how submitted_at came
# back empty during the first runs: code correct in the repo, submit-api image stale on the cluster).
# Set EXPECTED_IMAGE to also assert both equal the image you built for this run.
preflight_images() {
  local wimg simg
  wimg=$(kubectl -n "$NS" get deploy async-worker -o jsonpath='{.spec.template.spec.containers[0].image}' 2>/dev/null)
  simg=$(kubectl -n "$NS" get deploy submit-api  -o jsonpath='{.spec.template.spec.containers[0].image}' 2>/dev/null)
  echo ">> preflight images: async-worker=$wimg submit-api=$simg"
  [ -n "$wimg" ] && [ "$wimg" = "$simg" ] || { echo "PREFLIGHT FAIL: async-worker and submit-api images differ or are unset; a stale role will corrupt records. Redeploy before running." >&2; exit 1; }
  if [ -n "${EXPECTED_IMAGE:-}" ] && [ "$wimg" != "$EXPECTED_IMAGE" ]; then
    echo "PREFLIGHT FAIL: deployed image $wimg != EXPECTED_IMAGE $EXPECTED_IMAGE." >&2; exit 1
  fi
}
preflight_images

echo "=== LT-8 config: N_replicas=$N_REPLICAS workers=$WORKER_REPLICAS P=$P JOBS=$JOBS prefix_reps=$PREFIX_REPS reps=$REPS ==="

# --- submit-api port-forward ---
kubectl -n $NS port-forward svc/submit-api 8080:8080 >/dev/null 2>&1 &
PF_PID=$!
trap 'kill $PF_PID 2>/dev/null; restore_keda' EXIT
sleep 3
SUBMIT_URL="http://localhost:8080/submit"

suspend_keda() {   # pin N, stop KEDA from moving it (the correlated-drift confound)
  echo ">> suspending KEDA on the batch tier and pinning N=$N_REPLICAS"
  kubectl -n $NS annotate scaledobject vllm-llama3-batch autoscaling.keda.sh/paused-replicas="$N_REPLICAS" --overwrite
  kubectl -n $NS scale deploy/vllm-llama3-batch --replicas="$N_REPLICAS"
  kubectl -n $NS rollout status deploy/vllm-llama3-batch --timeout=600s
  kubectl -n $NS scale deploy/async-worker --replicas="$WORKER_REPLICAS"
  kubectl -n $NS set env deploy/async-worker SQS_BATCH_SIZE=1   # controlled admitted concurrency (A.5.3)
  kubectl -n $NS rollout status deploy/async-worker --timeout=300s
}
restore_keda() {
  echo ">> restoring KEDA + worker env"
  kubectl -n $NS annotate scaledobject vllm-llama3-batch autoscaling.keda.sh/paused-replicas- 2>/dev/null || true
  kubectl -n $NS set env deploy/async-worker PUB_ARM- SQS_BATCH_SIZE- 2>/dev/null || true
}

reset_cache() {    # cold cache WITHOUT a restart: each cell uses unique (salted) prefixes, so the
                   # cell's queries are all against prefixes never seen before. This is the documented
                   # alternative to rollout-restart (A.5.4): verified cold because lt8_cell.py salts the
                   # prefix content with the per-cell tag, and the measured first-request-per-group is a
                   # miss. Saves ~8 model reloads. The replicas started fresh at bring-up, so cell A is
                   # additionally on a genuinely empty cache.
  echo ">> cold cache via unique per-cell prefixes (no restart); replicas stay warm/loaded"
}

set_arm() {        # $1 = epp|rr : the worker adds x-pub-arm=rr for rr, nothing for epp
  if [ "$1" = rr ]; then
    kubectl -n $NS set env deploy/async-worker PUB_ARM=rr
  else
    kubectl -n $NS set env deploy/async-worker PUB_ARM-
  fi
  kubectl -n $NS rollout status deploy/async-worker --timeout=300s
}

run_cell() {       # $1 cell  $2 arm  $3 mode
  local cell="$1" arm="$2" mode="$3" rep out
  set_arm "$arm"
  for rep in $(seq 1 "$REPS"); do
    reset_cache
    out="$OUT/cell_${cell}_r${rep}.json"
    echo ">> cell $cell ($arm/$mode) rep $rep -> $out"
    ./.venv/bin/python loadtest/lt8_cell.py --cell "$cell" --arm "$arm" --mode "$mode" \
      --selector app=vllm-llama3,tier=batch --tag "lt8-${cell}-r${rep}" \
      --groups "$P" --n "$JOBS" --prefix-reps "$PREFIX_REPS" --max-tokens "$MAX_TOKENS" \
      --submit-url "$SUBMIT_URL" --dlq-url "$DLQ_URL" --drain-timeout "$DRAIN_TIMEOUT" \
      --out "$out" || echo "   cell $cell rep $rep FAILED (recorded, not retried)"
  done
}

suspend_keda
run_cell A epp grouped
run_cell B epp interleaved
run_cell C rr  grouped
run_cell D rr  interleaved

echo "=== LT-8 summary (direction reported, not assumed) ==="
./.venv/bin/python - "$OUT" <<'PY'
import glob, json, sys, statistics
out = sys.argv[1]
def hit(cell):
    xs = []
    for f in sorted(glob.glob(f"{out}/cell_{cell}_r*.json")):
        d = json.load(open(f))
        if d.get("prefix_cache_hit_rate") is not None:
            xs.append(d["prefix_cache_hit_rate"])
    return xs
H = {c: hit(c) for c in "ABCD"}
m = {c: (round(statistics.mean(v), 4) if v else None) for c, v in H.items()}
print("mean hit rate:", m)
if all(m[c] is not None for c in "ABCD"):
    ab, cd = m["A"] - m["B"], m["C"] - m["D"]
    print(f"grouping benefit under EPP  (A-B) = {ab:+.4f}")
    print(f"grouping benefit under RR   (C-D) = {cd:+.4f}")
    if ab > cd:
        print("MEASURED SIGN: (A-B) > (C-D). Reads as COMPLEMENTS. Report as measured, verify gates.")
    elif cd > ab:
        print("MEASURED SIGN: (C-D) > (A-B). Reads as SUBSTITUTES. Report as measured, verify gates.")
    else:
        print("MEASURED SIGN: (A-B) == (C-D). Neither; report as measured.")
    print("Anchor: cell A should attain the cold-miss floor (P cold events); confirm B did not tie by fitting.")
else:
    print("incomplete cells; nothing to compare yet")
PY
echo ">> raw per-cell JSON in $OUT/. Pre-registered predictions: $OUT/predictions.json. Fill LT-8-REPORT.md."
