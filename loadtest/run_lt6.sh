#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# LT-6 durability under worker kill. Submit a batch of async jobs, kill half the
# workers mid-generation, and assert ZERO jobs lost (all reach complete) with re-runs
# bounded (only interrupted jobs re-run). To fit the budget the claim TTL and SQS
# visibility are shortened to 60s (default 600/900) so a killed job's claim lapses and
# its message redelivers within the window; both are restored after. Generations are
# kept short so gen time stays well under the 60s TTL (no TTL-induced double runs).
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1
# Respect caller credentials. Only fall back to the named profile when the caller
# supplied neither a profile nor explicit keys, so temporary session credentials work.
if [ -z "${AWS_PROFILE:-}" ] && [ -z "${AWS_ACCESS_KEY_ID:-}" ]; then
  export AWS_PROFILE=eksinference
fi
N="${N_JOBS:-300}"; RUN_TAG="lt6-$(date +%s)"
# DERIVED, never a literal. This line carried the sanitised placeholder account, so the script
# could not run against the real account at all: the queue URL simply did not exist. Same class
# as the other placeholder sites; asking AWS for the URL cannot go stale or leak an account id.
QURL="$(aws sqs get-queue-url --queue-name inference --region us-west-2 --query QueueUrl --output text)"

echo ">> shorten SQS visibility to 60s and worker CLAIM_TTL (lease) to 25s"
# CRITICAL (design section 6.2): the claim lease MUST be < the SQS visibility timeout.
# If they are equal, a killed worker's message redelivers exactly as its lease expires,
# and the re-claim (lease_expires < now) can be borderline-refused, so the message is
# deleted as a duplicate and the interrupted job is orphaned. lease 25s < visibility 60s
# guarantees the lease is well-expired by the time the message redelivers.
#
# Ordering rule (INV10) during the test window: the jobs are short (max_tokens 64, gen a few
# seconds), so the gateway route timeout (600s) and worker HTTP timeout (660s) never fire and
# are not the active ceiling. The chain that governs recovery here is
#   max gen (~5s) < claim lease (25s) < SQS visibility (60s) < sweeper grace (120s)
# which holds. INV10 checks the production manifest values (600 < 660 < 750 < 900) and passes
# throughout the run, since these overrides use kubectl set-env and set-queue-attributes, not
# manifest edits.
OLD_VIS=$(aws sqs get-queue-attributes --queue-url "$QURL" --attribute-names VisibilityTimeout --query 'Attributes.VisibilityTimeout' --output text)
aws sqs set-queue-attributes --queue-url "$QURL" --attributes VisibilityTimeout=60 >/dev/null
kubectl -n inference set env deploy/async-worker CLAIM_TTL_SECONDS=25 >/dev/null
kubectl -n inference rollout status deploy/async-worker --timeout=90s >/dev/null
PF=""; PF2=""      # set below; initialised so the trap is safe if we exit before then
restore(){ aws sqs set-queue-attributes --queue-url "$QURL" --attributes VisibilityTimeout="${OLD_VIS:-900}" >/dev/null
           kubectl -n inference set env deploy/async-worker CLAIM_TTL_SECONDS=750 >/dev/null
           kill ${PF:-} ${PF2:-} 2>/dev/null; }   # PF2 (submit-api) was previously leaked
trap restore EXIT
kubectl -n monitoring port-forward svc/kps-kube-prometheus-stack-prometheus 9090:9090 >/tmp/prom-pf.log 2>&1 & PF=$!
kubectl -n inference port-forward svc/submit-api 8080:8080 >/tmp/submit-pf.log 2>&1 & PF2=$!
sleep 8
START=$(date +%s)

echo ">> submit N=$N distinct jobs (medium gen), then kill half the workers mid-flight"
SUBMIT_URL=http://localhost:8080/submit N_JOBS="$N" DUP=1 RUN_TAG="$RUN_TAG" MAX_TOKENS=64 CONCURRENCY=40 \
  ./.venv/bin/python loadtest/lt3_submit.py
sleep 20   # let jobs get in-flight and KEDA scale workers up
echo ">> FAULT: kill 50% of worker pods"
bash loadtest/faults.sh kill-workers 0.5

# Wait for all to complete, then take the durable count BEFORE the trap restores the
# production configuration (visibility 900, which would raise the sweeper grace to 960).
# The count is taken while the shortened test values are in effect, so the sweeper acts on
# its OWN schedule with the short grace and the loop observes the recovery.
#
# Cap derivation (sequencing, not weakened patience): the last orphan can appear up to one
# SQS visibility (60s) after the worker kill (a redelivery race), or later as KEDA scales
# workers down and a scaled-down worker leaves a claim. The sweeper recovers a message-less
# orphan within its grace (visibility 60 + margin 60 = 120s) plus one sweeper interval (60s)
# plus a re-claim and complete margin (60s). The cap must exceed the kill time plus that
# recovery budget: 100 + 60 + 120 + 60 + 60 = 400s. The cap is set to 1200s (20 min) as a
# generous safety bound; the loop breaks as soon as all N complete (typically 5 to 6 min).
echo ">> wait for all $N to complete on the sweeper's own schedule (cap 20 min; breaks on $N)"
for i in $(seq 1 80); do
  c=$(DDB_TABLE=inference-jobs ./.venv/bin/python -c "import loadtest.analyze as a; print(a.ddb_complete_count('$RUN_TAG'))" 2>/dev/null || echo 0)
  echo "[$i] complete=$c/$N"
  [ "${c:-0}" -ge "$N" ] && break
  sleep 15
done
END=$(date +%s)
echo "=== LT-6 verdict (analyze.py lt6d, durable DynamoDB claim_count) ==="
# Durable count survives the killed pods (unlike the per-pod Prometheus counter).
# max-interrupted is a generous bound on jobs in-flight when the workers were killed.
DDB_TABLE=inference-jobs ./.venv/bin/python loadtest/analyze.py lt6d \
  --run "$RUN_TAG" --n "$N" --max-interrupted 80
echo "RUN_TAG=$RUN_TAG START=$START END=$END"
