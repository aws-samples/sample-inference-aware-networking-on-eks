#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# PHASE3B DEFECT 2, VERIFIED LIVE FOR THE FIRST TIME. ZERO GPU.
#
# THE ASSERTION IS THE SHAPE OF THE RE-PLACEMENT, NOT THE FACT OF IT, and that distinction is the
# whole reason this script exists. Deliverable 1 on 2026-08-05 proved a promoted job completes. It
# could not have proved defect 2 was fixed, because the relay it ran against did not carry the fix:
# the worker half was rolled forward two minutes before the measurement and the relay half was never
# rolled forward at all. The job completed because the relay sent NO route and the worker fell back
# to /chat/completions, which was correct for a request that was already chat-shaped. The 400 was
# shown absent from the case that never had it.
#
# So the test that was needed is: does the ARRIVAL ROUTE TRAVEL? A completions-shaped promotion must
# re-place as completions, and a chat-shaped one as chat. Both directions, or the test proves only
# that a constant is being sent.
#
# THE AUTHORITY IS THE SQS MESSAGE, not the relay's 202 and not a completed job. The message is what
# the worker will read, so it is the thing whose shape decides the outcome. Reading the relay's own
# response would be asking the component under test to grade itself. NO BATCH CAPACITY IS NEEDED:
# the assertion is on the message, so nothing has to serve it.
#
# THE MISSING CONDITION, supplied exactly as Phase 3 supplied it: promotion fires when the upstream
# does not answer within P. The real pools answer INSTANTLY with 503 because they have no endpoints,
# and an immediate failure is not a slow success. tests/validation/promotion-stub.yaml is an
# upstream that accepts and never answers, which is what a long generation looks like from the
# relay's side. Third use of the technique.
#
# MUTATES THE CLUSTER and restores it. The relay is repointed at the stub with P=5 for the duration,
# then the committed manifest is re-applied. Everything it touches is CPU-only.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit 1
NS=inference
CALLER=${CALLER:-phase2-verifier}
fail=0
ok(){ printf "OK  %s\n" "$1"; }
no(){ printf "FAIL %s\n" "$1"; fail=1; }

cleanup(){
  echo
  echo ">> restoring the relay to its committed state"
  kubectl -n "$NS" delete -f tests/validation/promotion-stub.yaml --ignore-not-found >/dev/null 2>&1
  # `kubectl set env` writes a LITERAL over a valueFrom, and a plain apply does not always
  # put the valueFrom back, so the env is explicitly unset first and then the manifest re-applied.
  # Discovered the hard way on this script's first run: the restore reported a warning and the
  # relay was still pointing at the stub.
  kubectl -n "$NS" set env deploy/inference-relay GATEWAY_ENDPOINT- PROMOTION_DEADLINE_SECONDS- >/dev/null 2>&1
  kubectl apply -f manifests/08-relay/relay.yaml >/dev/null 2>&1
  kubectl -n "$NS" rollout status deploy/inference-relay --timeout=180s >/dev/null 2>&1
  # THE RESTORE IS ASSERTED, NOT ASSUMED. A harness that leaves a test topology behind turns the
  # next batch's cluster reading into a lie, and the drift this whole batch is about started as a
  # deployment nobody rolled back.
  live=$(kubectl -n "$NS" get deploy inference-relay -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="GATEWAY_ENDPOINT")].valueFrom.configMapKeyRef.name}')
  [ "$live" = "gateway-endpoint" ] && echo "   restored: GATEWAY_ENDPOINT reads the ConfigMap again" \
                                   || echo "   WARNING: relay did NOT restore cleanly, inspect it"
}
trap cleanup EXIT

QUEUE=$(kubectl -n "$NS" get cm async-endpoints -o jsonpath='{.data.sqs_queue_url}')
[ -n "$QUEUE" ] || { echo "FAIL missing queue url"; exit 1; }

# THE REGION IS DERIVED FROM THE QUEUE URL, NEVER INHERITED FROM THE CLI DEFAULT.
#
# This script's second run failed with "The specified queue does not exist" and that read as a
# system defect: the relay had returned 202 for both promotions, so the natural conclusion was that
# it hands out job ids for jobs it could not enqueue. It does not. The aws CLI default region here
# is eu-west-1 (from ~/.aws/config) while the queue is in us-west-2, so an omitted --region turns a
# present queue into a NonExistentQueue error. Third member of this project's false-negative family,
# after `head -4` on a model list and `wc -l` counting "No resources found": AN INCOMPLETE
# INVOCATION PRODUCED A CONFIDENT WRONG ANSWER ABOUT THE SYSTEM.
REGION=$(printf '%s' "$QUEUE" | sed -n 's|https://sqs\.\([a-z0-9-]*\)\.amazonaws\.com/.*|\1|p')
[ -n "$REGION" ] || { echo "FAIL could not derive a region from the queue url $QUEUE"; exit 1; }
echo ">> queue $QUEUE  (region $REGION, derived from the url rather than inherited)"
sqs(){ aws sqs --region "$REGION" "$@"; }

echo ">> draining the queue first, so every message read back belongs to THIS run"
# Without this a message left by an earlier run could be matched and the test would pass on
# somebody else's evidence.
for _ in 1 2 3; do
  sqs receive-message --queue-url "$QUEUE" --max-number-of-messages 10 --wait-time-seconds 1 \
    --query 'Messages[].ReceiptHandle' --output text 2>/dev/null | tr '\t' '\n' | while read -r h; do
      [ -n "$h" ] && sqs delete-message --queue-url "$QUEUE" --receipt-handle "$h" >/dev/null 2>&1
    done
done

echo ">> applying the hanging stub"
kubectl -n "$NS" apply -f tests/validation/promotion-stub.yaml >/dev/null
kubectl -n "$NS" rollout status deploy/promotion-stub --timeout=180s >/dev/null || {
  echo "FAIL the stub did not become ready"; exit 1; }

echo ">> repointing the relay at the stub with P=5 (committed P is 120 and is untouched)"
kubectl -n "$NS" set env deploy/inference-relay \
  GATEWAY_ENDPOINT="http://promotion-stub.${NS}.svc.cluster.local:8000/v1" \
  PROMOTION_DEADLINE_SECONDS=5 >/dev/null
kubectl -n "$NS" rollout status deploy/inference-relay --timeout=180s >/dev/null || {
  echo "FAIL the relay did not roll"; exit 1; }

echo ">> port-forwarding the relay"
kubectl -n "$NS" port-forward svc/inference-relay 18080:80 >/dev/null 2>&1 &
PF=$!
trap 'kill $PF 2>/dev/null; cleanup' EXIT
for _ in $(seq 1 30); do curl -sf -o /dev/null "http://127.0.0.1:18080/healthz" && break; sleep 1; done

# ---------------------------------------------------------------------------------------------
# THE TWO ARRIVALS. Same relay, same deadline, same caller: only the ROUTE differs, which is what
# makes this a test of the route rather than of anything else.
post(){
  local route="$1" body="$2"
  curl -s -o /dev/null -w '%{http_code}' -m 60 \
    -H "content-type: application/json" \
    -H "x-caller-id: $CALLER" \
    -H "x-gateway-inference-objective: interactive" \
    -X POST "http://127.0.0.1:18080${route}" -d "$body"
}
echo
echo ">> arrival 1: /v1/completions, raw prompt"
C1=$(post /v1/completions '{"prompt":"route shape probe one","max_tokens":8}')
echo "   relay returned $C1"
echo ">> arrival 2: /v1/chat/completions, messages"
C2=$(post /v1/chat/completions '{"messages":[{"role":"user","content":"route shape probe two"}],"max_tokens":8}')
echo "   relay returned $C2"

echo
echo ">> reading the enqueued messages back from SQS (the AUTHORITY, not the relay's claim)"
TMP=$(mktemp)
for _ in 1 2 3 4 5 6; do
  # A SHORT VISIBILITY TIMEOUT, so a message read on one poll is readable again on the next.
  # The queue's own visibility is 900 s: a default read would hide the first message for fifteen
  # minutes and the second poll would report one message, not two.
  sqs receive-message --queue-url "$QUEUE" --max-number-of-messages 10 --wait-time-seconds 2 \
    --visibility-timeout 5 --query 'Messages[].Body' --output json 2>/dev/null \
    | ./.venv/bin/python -c 'import json,sys
rows = json.load(sys.stdin) or []
for r in rows: print(json.dumps(json.loads(r)))' 2>/dev/null >> "$TMP"
  sort -u "$TMP" -o "$TMP"
  [ "$(grep -c . "$TMP")" -ge 2 ] && break
done
grep -c . "$TMP" | xargs -I{} echo "   messages read: {}"

./.venv/bin/python - "$TMP" <<'PY'
import json, sys
rows = []
for line in open(sys.argv[1]):
    line = line.strip()
    if not line:
        continue
    try:
        rows.append(json.loads(line))
    except json.JSONDecodeError:
        pass

payload_shape = lambda p: "completions" if "prompt" in (p or {}) else ("chat" if "messages" in (p or {}) else "?")
seen = {}
for m in rows:
    p = m.get("payload") or {}
    seen[payload_shape(p)] = m.get("path")
    print(f"   payload={payload_shape(p):11s}  path={m.get('path')!r}  "
          f"pair={m.get('sla_class')}/{m.get('capability_class')}")

fails = 0
if seen.get("completions") == "/v1/completions":
    print("OK  a COMPLETIONS-shaped promotion carries path=/v1/completions, so it re-places as what it was")
else:
    print(f"FAIL a completions-shaped promotion carried path={seen.get('completions')!r}. The worker "
          f"would re-submit it to /chat/completions and the gateway would return 400. THIS IS THE "
          f"DEFECT PHASE3B RECORDED AS FIXED.")
    fails += 1
if seen.get("chat") == "/v1/chat/completions":
    print("OK  a CHAT-shaped promotion carries path=/v1/chat/completions, so the route is READ and")
    print("    not merely a constant that happens to match one case")
else:
    print(f"FAIL a chat-shaped promotion carried path={seen.get('chat')!r}")
    fails += 1
sys.exit(1 if fails else 0)
PY
rc=$?
rm -f "$TMP"
[ "$rc" -eq 0 ] || fail=1

echo
if [ "$fail" -eq 0 ]; then
  echo "PASS: the arrival route TRAVELS. Both shapes re-place as themselves, read from the queue"
  echo "      rather than from the relay, which closes phase3b defect 2 on a running system."
else
  echo "FAILURES ABOVE"
fi
exit $fail
