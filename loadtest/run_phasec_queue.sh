#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# Phase C: long-request queueing under real conditions (no artificial batch cap).
# Drives a burst of genuinely long generations (high max_tokens) onto the async path at a
# rate that outpaces the drain, so the SQS queue depth rises on its own and is sustained
# while the long requests are in flight, then drains to zero after submission stops, with
# every job reaching complete and zero lost. The batch tier runs its NORMAL configuration
# (the vLLM running-batch cap is not shrunk). The verdict is a durable machine count
# (SQS depth samples + DynamoDB complete count), not an assessment.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1
# Respect caller credentials. Only fall back to the named profile when the caller
# supplied neither a profile nor explicit keys, so temporary session credentials work.
if [ -z "${AWS_PROFILE:-}" ] && [ -z "${AWS_ACCESS_KEY_ID:-}" ]; then
  export AWS_PROFILE=eksinference
fi
N="${N_JOBS:-80}"; MAXTOK="${MAX_TOKENS:-512}"; RUN_TAG="phasec-$(date +%s)"
QURL="https://sqs.us-west-2.amazonaws.com/111122223333/inference"
OUT="loadtest/results/phasec_queue_depth.csv"
mkdir -p loadtest/results

kubectl -n inference port-forward svc/submit-api 8080:8080 >/tmp/submit-pf.log 2>&1 & PF=$!
trap 'kill $PF 2>/dev/null' EXIT
sleep 8
depth(){ aws sqs get-queue-attributes --queue-url "$QURL" \
  --attribute-names ApproximateNumberOfMessages ApproximateNumberOfMessagesNotVisible \
  --query 'Attributes.[ApproximateNumberOfMessages,ApproximateNumberOfMessagesNotVisible]' --output text 2>/dev/null; }
complete(){ DDB_TABLE=inference-jobs ./.venv/bin/python -c "import loadtest.analyze as a; print(a.ddb_complete_count('$RUN_TAG'))" 2>/dev/null || echo 0; }

echo "elapsed_s,visible,not_visible,depth,complete" > "$OUT"
START=$(date +%s)
echo ">> baseline SQS depth"; depth

echo ">> submit N=$N distinct LONG jobs (max_tokens=$MAXTOK, D=1) as a burst; batch tier at NORMAL config"
SUBMIT_URL=http://localhost:8080/submit N_JOBS="$N" DUP=1 RUN_TAG="$RUN_TAG" MAX_TOKENS="$MAXTOK" CONCURRENCY=40 \
  ./.venv/bin/python loadtest/lt3_submit.py

PEAK=0
echo ">> monitor queue depth rise + drain, and completion (cap 18 min)"
for i in $(seq 1 108); do
  read -r VIS NVIS <<<"$(depth)"; VIS=${VIS:-0}; NVIS=${NVIS:-0}
  D=$((VIS + NVIS)); C=$(complete)
  E=$(( $(date +%s) - START ))
  echo "$E,$VIS,$NVIS,$D,$C" >> "$OUT"
  echo "[$i] t=${E}s visible=$VIS not_visible=$NVIS depth=$D complete=$C/$N"
  [ "$D" -gt "$PEAK" ] && PEAK=$D
  # done when the queue has drained AND all jobs are complete
  [ "$VIS" -eq 0 ] && [ "$NVIS" -eq 0 ] && [ "${C:-0}" -ge "$N" ] && break
  sleep 10
done
END=$(date +%s)
FINAL_C=$(complete); read -r FVIS FNVIS <<<"$(depth)"

echo "=== Phase C verdict (machine count) ==="
echo "  peak queue depth        : $PEAK   (must be > 0: queue built under real load)"
echo "  final queue depth        : $((${FVIS:-0}+${FNVIS:-0}))   (must be 0: drained)"
echo "  jobs complete            : $FINAL_C / $N   (must equal $N: zero lost)"
V=PASS
[ "$PEAK" -gt 0 ] || { echo "  queue never built"; V=FAIL; }
[ "$(( ${FVIS:-0}+${FNVIS:-0} ))" -eq 0 ] || { echo "  queue did not drain"; V=FAIL; }
[ "${FINAL_C:-0}" -ge "$N" ] || { echo "  jobs lost"; V=FAIL; }
echo "  RESULT: $V   RUN_TAG=$RUN_TAG window=$((END-START))s  depth_series=$OUT"
