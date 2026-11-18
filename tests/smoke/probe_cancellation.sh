#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# INV13, THE CANCELLATION PROBE. Dormant by default: it needs a serving replica, and there is none.
#
# WHAT IT PROVES. That disconnect-cancellation still works: when a client closes the connection
# mid-generation, the sequence slot frees. Measured 2026-08-03 at 0.55 to 0.86 s across 4 of 4
# cells, adopted as A15.
#
# WHY IT IS WORTH RUNNING REPEATEDLY. Decision D-1 rests on this mechanism: at the promotion
# deadline the relay abandons an in-flight generation by closing the connection, and the slot frees.
# THE REGRESSION IS SILENT. An Envoy Gateway configuration change, a keepalive change, a vLLM pin
# move or a change to the relay's HTTP client can break disconnect propagation with NOTHING failing.
# Promotion would quietly become delivery-only: the caller gets a job id, the replica keeps
# generating, and the tier isolation property LT-4 verified is lost invisibly. That is exactly what
# D-1 exists to prevent, and it is invisible from the caller's side.
#
# WHY SCHEDULED AND NOT ALWAYS-ON (docs/specs/DECISION-stage-1-and-inv13.md, recommendation
# accepted). The probe's value is REPETITION, NOT CONTINUITY: it catches a regression introduced by
# a change, and changes arrive with commits rather than with the clock. Always-on serving to run a
# 20-second check is roughly USD 700 to 900 per month against USD 20 to 30 for a bring-up-and-park
# cycle, about thirty to one, for identical coverage. The 170 s model load (LT-5) is why a cycle is
# minutes rather than seconds.
#
# DORMANT MEANS DORMANT, AND IT MEANS SKIP, NOT PASS. With no serving replica this exits 0 with an
# explicit SKIP line naming what is absent. It is NOT in `make test-offline` and it is NOT in
# `verify_invariants.sh`, so nothing can read its silence as evidence. A probe that returns green
# when it did not run is worse than no probe, because it is a green light nobody earned. Set
# `PROBE_REQUIRE=1` to make absence a FAILURE instead, which is what a scheduled run must do:
# on a schedule, "no replica" means the bring-up failed, and that is a real result.
#
# Usage:
#   bash tests/smoke/probe_cancellation.sh              # dormant: skips cleanly if nothing serves
#   PROBE_REQUIRE=1 bash tests/smoke/probe_cancellation.sh   # scheduled: absence is a failure
#
# Runbook: docs/runbooks/inv13-cancellation-probe.md
set -uo pipefail
NS="${NS:-inference}"
DEPLOY="${DEPLOY:-vllm-llama3-interactive}"
REQUIRE="${PROBE_REQUIRE:-0}"
HARD_BOUND_S="${HARD_BOUND_S:-5}"     # FAIL above this. OPERATIVE: it still discriminates.
WARN_BOUND_S="${WARN_BOUND_S:-2}"     # INOPERATIVE, see below. Kept so its reactivation is a
                                      # one-line change rather than a re-derivation.

# THE WARN TIER IS INOPERATIVE AS OF 2026-08-05, AND THE REASON IS THE INSTRUMENT, NOT THE ENGINE.
#
# Each sample here costs a `kubectl exec` round trip, MEASURED AT 2,625 ms in the same session that
# first ran this probe. The release interval it is trying to observe is about 0.8 s (0b A15). So the
# instrument's RESOLUTION IS THREE TIMES THE THRESHOLD, and the 2-second warn fires on EVERY RUN by
# construction, whatever the engine does.
#
# A warning that cannot fail to fire is not a warning. It trains dismissal, and a dismissed alarm is
# worse than an absent one because it occupies the slot where a real signal would go. This project
# has spent considerable effort designing that decay out of other checks (the PEND that nothing
# forces to PASS, the empty vector that becomes a confident zero), so leaving one here to cry wolf
# would be inconsistent with everything around it.
#
# THE HARD BOUND STAYS OPERATIVE. At 5 s it is still comfortably above the instrument's resolution,
# so it discriminates: a genuinely broken disconnect would not free the slot within one or two
# samples, and that is exactly what it catches.
#
# REACTIVATES WITH THE IN-POD PROBE, which measures without paying a round trip per sample and can
# therefore see a 0.8 s interval. Until then the warn is reported as INOPERATIVE rather than
# silently deleted, because deleting it would lose the derivation.

# Both bounds were derived in the CONCURRENCY-1, SINGLE-REPLICA regime and carry that qualifier.
# They are not general latency claims, and they must be re-derived if the probe is ever run against
# a loaded replica, where queueing time is included in what is measured.

skip_or_fail() {
  if [ "$REQUIRE" = "1" ]; then
    echo "FAIL INV13 probe REQUIRED but could not run: $1"
    echo "     On a scheduled run this is a real result: the bring-up did not produce a serving"
    echo "     replica, so the mechanism is unverified for this cycle."
    exit 1
  fi
  echo "SKIP INV13 cancellation probe: $1"
  echo "     Dormant by design. This is a SKIP and not a PASS: nothing was proven. Bring up a"
  echo "     serving replica, or run a scheduled cycle with PROBE_REQUIRE=1."
  exit 0
}

command -v kubectl >/dev/null 2>&1 || skip_or_fail "kubectl is not on PATH"
kubectl get ns "$NS" >/dev/null 2>&1 || skip_or_fail "namespace $NS is absent"

READY=$(kubectl -n "$NS" get deploy "$DEPLOY" -o jsonpath='{.status.readyReplicas}' 2>/dev/null)
READY=${READY:-0}
[ "$READY" -gt 0 ] 2>/dev/null || skip_or_fail "deployment $DEPLOY has $READY ready replicas (needs at least 1)"

# THE SELECTOR IS THE DEPLOYMENT'S ACTUAL LABELS, not its NAME. Corrected 2026-08-05 on this
# probe's FIRST REAL EXECUTION: it selected `app=<deployment-name>`, and the serving pods carry
# `app=vllm-llama3` with the tier in a SEPARATE `tier` label, so it found nothing and reported
# "the bring-up did not produce a serving replica" while a healthy replica was serving beside it.
#
# This is exactly what DEBT-PASSING-PATH predicts. Every green reading this probe had ever produced
# was a SKIP, so the branch that reads the pod had never run, and a wrong selector cannot be caught
# by a check that never reaches it. INV12 failed the same way at the cutover; this one was found by
# executing the passing path deliberately rather than by being blocked by it later.
TIER="${TIER:-${DEPLOY##*-}}"
POD=$(kubectl -n "$NS" get pods -l "app=vllm-llama3,tier=$TIER" -o name 2>/dev/null | head -1)
[ -n "$POD" ] || skip_or_fail "no pod found for $DEPLOY"

echo "=== INV13: disconnect-cancellation probe ==="
echo "  namespace/deploy : $NS/$DEPLOY"
echo "  bounds           : warn > ${WARN_BOUND_S}s, FAIL > ${HARD_BOUND_S}s (concurrency-1,"
echo "                     single-replica regime; re-derive if run under load)"

metric() {
  # Running sequences, read from the pod's own metrics endpoint. The AUTHORITY for whether a slot
  # freed is the engine's counter, not the client's view of its socket: the client sees its own
  # close succeed whether or not the server noticed, which is precisely the failure being hunted.
  kubectl -n "$NS" exec "$POD" -c vllm -- \
    sh -c 'curl -s localhost:8000/metrics' 2>/dev/null \
    | grep -E '^vllm:num_requests_running' | awk '{print $2}' | head -1
}

BASE=$(metric)
[ -n "$BASE" ] || skip_or_fail "could not read vllm:num_requests_running from $POD"
echo "  baseline running : $BASE"

# Start a long generation and abandon it. `--max-time` closes the connection from the client side,
# which is the exact action the relay takes at the promotion deadline.
#
# THE MODEL ID IS FETCHED IN ITS OWN STEP. The first version built the request body with a `sed`
# nested inside a double-quoted `-d "{...}"` inside `sh -c` inside `kubectl exec`, four levels of
# quoting, and the body arrived malformed so vLLM rejected it INSTANTLY. The probe then reported
# "could not establish a generation to cancel", which was true and useless: the generation never
# started because the request was broken, not because the engine refused it. Found on this probe's
# first real execution.
# FIRST match, not a greedy one. `sed 's/.*"id":"\([^"]*\)".*/\1/'` matched the LAST "id" in the
# document and returned `modelperm-...`, a permission id, which vLLM then rejected as an unknown
# model. Greedy `.*` reaching past the field you want is the same shape as every other
# text-adjacency defect in this repository.
MODEL=$(kubectl -n "$NS" exec "$POD" -c vllm -- sh -c 'curl -s localhost:8000/v1/models' 2>/dev/null \
  | grep -o '"id":"[^"]*"' | head -1 | cut -d'"' -f4)
[ -n "$MODEL" ] || skip_or_fail "could not read the served model id from $POD"
echo "  model            : $MODEL"

kubectl -n "$NS" exec "$POD" -c vllm -- sh -c \
  "curl -s --max-time 6 -X POST localhost:8000/v1/completions -H 'content-type: application/json' -d '{\"model\":\"$MODEL\",\"prompt\":\"Write an extremely long and detailed essay about distributed systems.\",\"max_tokens\":2000}' >/dev/null 2>&1" \
  >/dev/null 2>&1 &
CURL_PID=$!

# POLL for the count to rise rather than sampling once at a fixed instant. A single sample races
# the scheduler: prefill takes a moment, and a probe that looked once and concluded "nothing is
# running" would be measuring its own timing rather than the engine's.
DURING="$BASE"
for _ in $(seq 1 20); do
  DURING=$(metric)
  [ "${DURING:-0}" != "$BASE" ] && break
  sleep 0.5
done
echo "  during generation: $DURING"

if [ "${DURING:-0}" = "$BASE" ]; then
  echo "FAIL INV13 could not establish a generation to cancel: the running count never rose above"
  echo "     the baseline ($BASE). The probe proves nothing about cancellation either way, so this"
  echo "     is reported as a FAILURE OF THE PROBE rather than as a passing cancellation."
  exit 1
fi

# Now let the client's --max-time fire, which closes the connection. THAT is the abandonment.
wait $CURL_PID 2>/dev/null
DISCONNECT_AT=$(date +%s%N)

START=$(date +%s)
FREED=""
while [ $(( $(date +%s) - START )) -le "$HARD_BOUND_S" ]; do
  CUR=$(metric)
  if [ "${CUR:-1}" = "$BASE" ]; then
    FREED=$(( $(date +%s) - START ))
    FREED_MS=$(( ( $(date +%s%N) - DISCONNECT_AT ) / 1000000 ))
    break
  fi
  sleep 0.25
done

if [ -z "$FREED" ]; then
  echo "FAIL INV13 the slot did NOT free within ${HARD_BOUND_S}s of the client disconnecting."
  echo "     Disconnect propagation is broken. Decision D-1 rests on this: without it, promotion"
  echo "     becomes delivery-only, the replica keeps generating after the deadline, and tier"
  echo "     isolation degrades with nothing else failing. Investigate before relying on D-1."
  exit 1
fi

echo "OK   INV13 slot freed after disconnect: ${FREED_MS} ms (baseline $BASE restored)"
echo "     CONCURRENCY-1, SINGLE-REPLICA regime. This is a FLOOR, not a bound under load."
# The warn is REPORTED as inoperative rather than evaluated. Printing it every run keeps the fact
# visible at the point of use, where a reader is deciding what the result means.
echo "NOTE warn tier (${WARN_BOUND_S}s) is INOPERATIVE: this probe's per-sample resolution is about"
echo "     2.6s (one kubectl exec round trip), which exceeds the threshold, so the warn would fire"
echo "     on every run regardless of engine behaviour. The ${HARD_BOUND_S}s hard bound above is"
echo "     operative and did discriminate. Reactivates with the in-pod probe."
exit 0
