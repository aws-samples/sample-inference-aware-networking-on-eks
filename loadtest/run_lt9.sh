#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# LT-9: durability across a batch-inference-path outage (async's headline contribution, measured).
#
# LT-3 and LT-6 proved exactly-once under duplicate delivery and pod death. LT-9 tests the failure an
# operator most expects a queue to absorb: a sustained outage of the downstream inference path.
#
# DEPENDS ON B.1. Before the fix, _backoff_message shortened visibility and drove every in-flight
# message to the DLQ in ~8-10 min. The fix (leave the message untouched, keep the queue's 900s
# visibility) is exactly what this measures, so LT-9 is B.1's regression test.
#
# This reports the ENVELOPE, not a binary pass: "zero loss for outages up to X; beyond X, work lands
# in the DLQ where redrive recovers it (not lost)." X is bounded by maxReceiveCount x visibility, but
# the orphan sweeper interacts (re-enqueue resets ApproximateReceiveCount, extending the horizon; the
# 300s sweep cooldown amplifies queue depth; the 60s margin between the 900s redelivery and the 960s
# orphan grace is tight). Do NOT predict this: instrument it. If amplification looks unbounded, report
# it as a finding, do not patch mid-run.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1
# Respect caller credentials. Only fall back to the named profile when the caller
# supplied neither a profile nor explicit keys, so temporary session credentials work.
if [ -z "${AWS_PROFILE:-}" ] && [ -z "${AWS_ACCESS_KEY_ID:-}" ]; then
  export AWS_PROFILE=eksinference
fi
NS=inference
OUT=loadtest/results/lt9
mkdir -p "$OUT"

K="${K:-40}"                 # jobs held in the queue (meaningful backlog)
# Not GROUPS: bash reserves that name for the user's group IDs and ignores assignments to it,
# so the driver would silently fall back to its default. Passed to the driver through env below.
LT9_GROUPS="${LT9_GROUPS:-4}"
OUTAGE_SECONDS="${OUTAGE_SECONDS:-1200}"   # >= 20 min: well beyond the ~8-10 min pre-fix DLQ horizon
SAMPLE_SECONDS="${SAMPLE_SECONDS:-15}"
DRAIN_TIMEOUT="${DRAIN_TIMEOUT:-1800}"
TAG="lt9-$(date +%s 2>/dev/null || echo run)"   # date is fine in a client-side script

QUEUE_URL="$(aws sqs get-queue-url --queue-name inference --query QueueUrl --output text)"
DLQ_URL="$(aws sqs get-queue-url --queue-name inference-dlq --query QueueUrl --output text)"
DEPTH_CSV="$OUT/queue_depth.csv"
echo "epoch,visible,in_flight,dlq" > "$DEPTH_CSV"

# Preflight: fail if the deployed image is stale relative to the repository. async-worker and submit-api
# build from the same Dockerfile, so their deployed tags must match; a mismatch means one role runs old
# code and corrupts records silently (this is how submitted_at came back empty on a prior run). Set
# EXPECTED_IMAGE to also assert both equal the image built for this run.
_wimg=$(kubectl -n "$NS" get deploy async-worker -o jsonpath='{.spec.template.spec.containers[0].image}' 2>/dev/null)
_simg=$(kubectl -n "$NS" get deploy submit-api  -o jsonpath='{.spec.template.spec.containers[0].image}' 2>/dev/null)
echo ">> preflight images: async-worker=$_wimg submit-api=$_simg"
[ -n "$_wimg" ] && [ "$_wimg" = "$_simg" ] || { echo "PREFLIGHT FAIL: async-worker and submit-api images differ or are unset; redeploy before running." >&2; exit 1; }
if [ -n "${EXPECTED_IMAGE:-}" ] && [ "$_wimg" != "$EXPECTED_IMAGE" ]; then
  echo "PREFLIGHT FAIL: deployed image $_wimg != EXPECTED_IMAGE $EXPECTED_IMAGE." >&2; exit 1
fi

sample() { # one queue-depth sample row
  local v n d
  v=$(aws sqs get-queue-attributes --queue-url "$QUEUE_URL" --attribute-names ApproximateNumberOfMessages --query 'Attributes.ApproximateNumberOfMessages' --output text 2>/dev/null || echo -1)
  n=$(aws sqs get-queue-attributes --queue-url "$QUEUE_URL" --attribute-names ApproximateNumberOfMessagesNotVisible --query 'Attributes.ApproximateNumberOfMessagesNotVisible' --output text 2>/dev/null || echo -1)
  d=$(aws sqs get-queue-attributes --queue-url "$DLQ_URL" --attribute-names ApproximateNumberOfMessages --query 'Attributes.ApproximateNumberOfMessages' --output text 2>/dev/null || echo -1)
  echo "$(date +%s),$v,$n,$d" >> "$DEPTH_CSV"
}

sampler() { while true; do sample; sleep "$SAMPLE_SECONDS"; done; }

# submit-api port-forward
kubectl -n $NS port-forward svc/submit-api 8080:8080 >/dev/null 2>&1 &
PF_PID=$!
sampler &
SAMPLER_PID=$!
trap 'kill $PF_PID $SAMPLER_PID 2>/dev/null; kubectl -n '"$NS"' scale deploy/vllm-llama3-batch-epp --replicas=1 >/dev/null 2>&1 || true' EXIT
sleep 3
export SUBMIT_URL="http://localhost:8080/submit" TAG="$TAG" N="$K" MODE=interleaved

echo "=== [1/4] submit $K jobs ==="
env GROUPS="$LT9_GROUPS" ./.venv/bin/python loadtest/phased_batch.py

echo "=== [2/4] batch inference-path outage for ${OUTAGE_SECONDS}s (scale batch EPP to 0) ==="
kubectl -n $NS scale deploy/vllm-llama3-batch-epp --replicas=0
OUTAGE_START=$(date +%s)
sleep "$OUTAGE_SECONDS"
# Snapshot the DLQ at the end of the outage window: this is the horizon evidence.
DLQ_AT_OUTAGE_END=$(aws sqs get-queue-attributes --queue-url "$DLQ_URL" --attribute-names ApproximateNumberOfMessages --query 'Attributes.ApproximateNumberOfMessages' --output text)
echo "   DLQ depth at end of ${OUTAGE_SECONDS}s outage: $DLQ_AT_OUTAGE_END (expect 0 post-B.1; >0 means the horizon was crossed)"

echo "=== [3/4] restore the EPP, drain to completion ==="
kubectl -n $NS scale deploy/vllm-llama3-batch-epp --replicas=1
kubectl -n $NS rollout status deploy/vllm-llama3-batch-epp --timeout=120s
DRAIN_START=$(date +%s)
./.venv/bin/python - "$TAG" "$K" "$DRAIN_TIMEOUT" <<'PY'
import sys, time, os, boto3
tag, K, timeout = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
ddb = boto3.client("dynamodb"); table = os.environ.get("DDB_TABLE", "inference-jobs")
def items():
    out, kw = [], {}
    while True:
        r = ddb.scan(TableName=table, FilterExpression="begins_with(job_id, :t)",
                     ExpressionAttributeValues={":t": {"S": tag + "-g"}}, **kw)
        out += r.get("Items", [])
        if "LastEvaluatedKey" not in r: return out
        kw["ExclusiveStartKey"] = r["LastEvaluatedKey"]
deadline = time.time() + timeout
while time.time() < deadline:
    it = items()
    done = [i for i in it if i.get("status", {}).get("S") in ("complete", "failed")]
    if len(done) >= K: break
    time.sleep(10)
print(f"drained {len(done)}/{K}")
PY
DRAIN_END=$(date +%s)

echo "=== [4/4] verdict (envelope + exactly-once gates) ==="
kubectl -n $NS logs -l app=sweeper --tail=2000 --prefix 2>/dev/null | grep -ciE "orphan|enqueue" | sed 's/^/   sweeper orphan/enqueue log lines: /' || true
./.venv/bin/python - "$TAG" "$K" "$DLQ_URL" "$OUTAGE_START" "$DRAIN_START" "$DRAIN_END" "$OUTAGE_SECONDS" "$DLQ_AT_OUTAGE_END" "$OUT" <<'PY'
import sys, os, json, boto3
tag, K, dlq_url = sys.argv[1], int(sys.argv[2]), sys.argv[3]
outage_start, drain_start, drain_end = map(int, sys.argv[4:7])
outage_seconds, dlq_at_outage_end = int(sys.argv[7]), int(sys.argv[8]); out = sys.argv[9]
ddb = boto3.client("dynamodb"); sqs = boto3.client("sqs"); table = os.environ.get("DDB_TABLE", "inference-jobs")
def items():
    o, kw = [], {}
    while True:
        r = ddb.scan(TableName=table, FilterExpression="begins_with(job_id, :t)",
                     ExpressionAttributeValues={":t": {"S": tag + "-g"}}, **kw)
        o += r.get("Items", [])
        if "LastEvaluatedKey" not in r: return o
        kw["ExclusiveStartKey"] = r["LastEvaluatedKey"]
it = items()
status = {}
for i in it:
    s = i.get("status", {}).get("S", "absent"); status[s] = status.get(s, 0) + 1
claim = sum(int(i.get("claim_count", {}).get("N", "0")) for i in it)
infra = sum(int(i.get("infra_rejections", {}).get("N", "0")) for i in it)
dlq_now = int(sqs.get_queue_attributes(QueueUrl=dlq_url, AttributeNames=["ApproximateNumberOfMessages"])["Attributes"]["ApproximateNumberOfMessages"])
verdict = {
    "K": K, "statuses": status,
    "zero_loss": status.get("complete", 0) == K and status.get("failed", 0) == 0,
    "no_job_failed": status.get("failed", 0) == 0,
    "dlq_at_outage_end": dlq_at_outage_end, "dlq_now": dlq_now, "zero_dlq": dlq_at_outage_end == 0 and dlq_now == 0,
    "claim_count_sum": claim, "infra_rejections_sum": infra, "real_gpu_runs": claim - infra,
    "exactly_once": (claim - infra) == K,
    "outage_seconds": outage_seconds, "time_to_drain_s": max(0, drain_end - drain_start),
    "horizon_note": ("no DLQ within the outage: horizon > %ds" % outage_seconds) if dlq_at_outage_end == 0
                    else ("horizon crossed inside %ds: %d messages reached the DLQ (recoverable by redrive)" % (outage_seconds, dlq_at_outage_end)),
}
json.dump(verdict, open(os.path.join(out, "LT-9-verdict.json"), "w"), indent=2)
print("LT9_VERDICT " + json.dumps(verdict))
allpass = verdict["zero_loss"] and verdict["zero_dlq"] and verdict["exactly_once"] and verdict["no_job_failed"]
print("LT-9:", "PASS (zero loss, zero DLQ, exactly-once, no job failed)" if allpass
      else "SEE VERDICT (envelope crossed or a gate failed; report as measured, do not patch mid-run)")
PY
echo ">> queue-depth series: $DEPTH_CSV ; verdict: $OUT/LT-9-verdict.json ; fill LT-9-REPORT.md"
