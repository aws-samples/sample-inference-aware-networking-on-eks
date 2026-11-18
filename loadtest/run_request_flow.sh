#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# Combined demonstration for the Request Flow dashboard: both request paths at once, long enough
# to be legible (sustained well past 20 Prometheus scrape intervals). The synchronous path drives
# short requests straight through the gateway to the interactive tier and never touches the queue.
# The asynchronous path submits a burst of long requests that visibly builds SQS depth and drains
# through the batch tier. Records the exact epoch window for the dashboard and the reconciliation.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1
# Respect caller credentials. Only fall back to the named profile when the caller
# supplied neither a profile nor explicit keys, so temporary session credentials work.
if [ -z "${AWS_PROFILE:-}" ] && [ -z "${AWS_ACCESS_KEY_ID:-}" ]; then
  export AWS_PROFILE=eksinference
fi
: "${TAG:=flow-$(date +%s)}"
: "${N_ASYNC:=220}"
: "${ASYNC_MAX_TOKENS:=512}"
: "${SYNC_CONC:=8}"
: "${DURATION:=1200}"
: "${WORKER_CAP:=4}"    # cap worker replicas so the queue drains as a sustained curve, not a spike
GW="http://$(kubectl -n inference get gateway inference-gateway -o jsonpath='{.status.addresses[0].value}')"
[ "$GW" = "http://" ] && { echo "STOP: gateway has no external address" >&2; exit 1; }
QURL="https://sqs.us-west-2.amazonaws.com/111122223333/inference"
mkdir -p loadtest/results

# Cap the async worker to WORKER_CAP replicas for the demo so a burst builds a legible, sustained
# queue instead of draining in one scrape interval (KEDA default max is 20). This is a demo shaping
# knob on the scaler, not a change to routing, the claim protocol, or the timeout chain. Restored
# on exit. The worker's max is restored to 20 (its production value).
OLD_MAX=$(kubectl -n inference get scaledobject async-worker -o jsonpath='{.spec.maxReplicaCount}')
kubectl -n inference patch scaledobject async-worker --type merge -p "{\"spec\":{\"maxReplicaCount\":${WORKER_CAP}}}" >/dev/null
kubectl -n inference scale deploy async-worker --replicas="${WORKER_CAP}" >/dev/null 2>&1

kubectl -n inference port-forward svc/submit-api 8080:8080 >/tmp/submit-pf.log 2>&1 & PF=$!
restore(){ kubectl -n inference patch scaledobject async-worker --type merge -p "{\"spec\":{\"maxReplicaCount\":${OLD_MAX:-20}}}" >/dev/null 2>&1
           kill "${PF:-0}" "${DSYNC:-0}" 2>/dev/null; }
trap restore EXIT
sleep 6
START=$(date +%s)
echo ">> TAG=$TAG  window START=$START (UTC $(date -u -r "$START" '+%Y-%m-%d %H:%M:%S'))"

echo ">> start SYNCHRONOUS short load (interactive-short, no tier header -> interactive) for ${DURATION}s"
GATEWAY="$GW" WORKLOAD=interactive-short TIER=interactive CONCURRENCY="$SYNC_CONC" DURATION="$DURATION" ROUTER=epp \
  ./.venv/bin/python loadtest/drive_sync.py >/tmp/flow_sync.out 2>&1 & DSYNC=$!

echo ">> 60s of sync-only (async-path metrics must read zero here)"
sleep 60

echo ">> submit ASYNCHRONOUS long burst: N=$N_ASYNC max_tokens=$ASYNC_MAX_TOKENS (builds SQS depth)"
SUBMIT_URL=http://localhost:8080/submit N_JOBS="$N_ASYNC" DUP=1 RUN_TAG="$TAG" MAX_TOKENS="$ASYNC_MAX_TOKENS" CONCURRENCY=40 \
  ./.venv/bin/python loadtest/lt3_submit.py

echo ">> watch SQS depth build and drain (cap ~18 min; breaks on full drain); record depth series"
echo "elapsed_s,visible,in_flight,complete" > loadtest/results/request_flow_depth.csv
PEAK=0
for i in $(seq 1 108); do
  read -r VIS NVIS <<<"$(aws sqs get-queue-attributes --queue-url "$QURL" --attribute-names ApproximateNumberOfMessages ApproximateNumberOfMessagesNotVisible --query 'Attributes.[ApproximateNumberOfMessages,ApproximateNumberOfMessagesNotVisible]' --output text 2>/dev/null)"
  VIS=${VIS:-0}; NVIS=${NVIS:-0}
  C=$(DDB_TABLE=inference-jobs ./.venv/bin/python -c "import loadtest.analyze as a; print(a.ddb_complete_count('$TAG'))" 2>/dev/null || echo 0)
  E=$(( $(date +%s) - START ))
  echo "$E,$VIS,$NVIS,$C" >> loadtest/results/request_flow_depth.csv
  echo "[$i] t=${E}s visible=$VIS in_flight=$NVIS complete=$C/$N_ASYNC"
  [ "$VIS" -gt "$PEAK" ] && PEAK=$VIS
  # done when the queue has fully drained AND all async complete
  [ "$VIS" -eq 0 ] && [ "$NVIS" -eq 0 ] && [ "${C:-0}" -ge "$N_ASYNC" ] && break
  sleep 10
done

echo ">> queue drained; keep sync running ~60s more so async metrics return to zero on the timeline"
sleep 60
kill $DSYNC 2>/dev/null
END=$(date +%s)
echo "=== Request Flow demo done ==="
echo "  peak SQS visible depth : $PEAK"
echo "  async run tag          : $TAG"
echo "  window START=$START END=$END  (from=${START}000 to=${END}000 epoch-ms for the Grafana preset URL)"
echo "  UTC $(date -u -r "$START" '+%H:%M:%S') to $(date -u -r "$END" '+%H:%M:%S')"
echo "  sync driver summary: $(grep -E 'requests=|err_rate' /tmp/flow_sync.out 2>/dev/null | tr '\n' ' ')"
