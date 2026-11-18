#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# LT-7 routing-layer failure modes. Take each EPP down (scale to 0) under light load
# and observe the per-pool failureMode, then restore. Both tiers are FailClose on this stack
# (expect a clean 5xx on each). Batch was designed FailOpen, but the gateway does not honor
# FailOpen for InferencePool (DESIGN.md invariant 6), so the manifests assert FailClose.
# Probes are counted directly (status codes), which is the machine check here.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1
# Respect caller credentials. Only fall back to the named profile when the caller
# supplied neither a profile nor explicit keys, so temporary session credentials work.
if [ -z "${AWS_PROFILE:-}" ] && [ -z "${AWS_ACCESS_KEY_ID:-}" ]; then
  export AWS_PROFILE=eksinference
fi
GW="http://$(kubectl -n inference get gateway inference-gateway -o jsonpath='{.status.addresses[0].value}')"
[ "$GW" = "http://" ] && { echo "STOP: gateway has no external address" >&2; exit 1; }
M="NousResearch/Meta-Llama-3.1-8B-Instruct"

probe() { # tier n  -> prints "<2xx> <non2xx>"
  local tier="$1" n="$2" ok=0 bad=0 c d
  d="{\"model\":\"$M\",\"messages\":[{\"role\":\"user\",\"content\":\"probe\"}],\"max_tokens\":6}"
  for _ in $(seq 1 "$n"); do
    if [ "$tier" = batch ]; then
      c=$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 -X POST "$GW/v1/chat/completions" \
          -H 'content-type: application/json' -H 'x-gateway-inference-objective: batch' -d "$d")
    else
      c=$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 -X POST "$GW/v1/chat/completions" \
          -H 'content-type: application/json' -d "$d")
    fi
    if [ "$c" = 200 ]; then ok=$((ok+1)); else bad=$((bad+1)); fi
  done
  echo "$ok $bad"
}

echo "=== baseline: both EPPs up ==="
echo "  interactive $(probe interactive 6)  (ok bad)"
echo "  batch       $(probe batch 6)  (ok bad)"

echo "=== FailClose: interactive EPP down ==="
kubectl -n inference scale deploy vllm-llama3-interactive-epp --replicas=0 >/dev/null; sleep 25
read IC_OK IC_BAD <<<"$(probe interactive 8)"
echo "  interactive with EPP down: ok=$IC_OK bad=$IC_BAD  (expect bad>0, FailClose 5xx)"
kubectl -n inference scale deploy vllm-llama3-interactive-epp --replicas=1 >/dev/null
kubectl -n inference rollout status deploy/vllm-llama3-interactive-epp --timeout=90s >/dev/null; sleep 10
read IR_OK IR_BAD <<<"$(probe interactive 6)"
echo "  interactive after restore: ok=$IR_OK bad=$IR_BAD  (expect ok=6)"

# Batch is FailClose on this stack too (Envoy AI Gateway v1.0.0 does not honor FailOpen
# for InferencePool; see Phase A). So a batch EPP outage is expected to return clean
# errors, exactly like interactive, and this asserts the corrected (delivered) behavior.
echo "=== FailClose: batch EPP down (corrected: FailOpen not honored by AI Gateway v1.0.0) ==="
kubectl -n inference scale deploy vllm-llama3-batch-epp --replicas=0 >/dev/null; sleep 25
read BC_OK BC_BAD <<<"$(probe batch 8)"
echo "  batch with EPP down: ok=$BC_OK bad=$BC_BAD  (expect bad>0, FailClose clean 5xx)"
kubectl -n inference scale deploy vllm-llama3-batch-epp --replicas=1 >/dev/null
kubectl -n inference rollout status deploy/vllm-llama3-batch-epp --timeout=90s >/dev/null; sleep 10
read BR_OK BR_BAD <<<"$(probe batch 6)"
echo "  batch after restore: ok=$BR_OK bad=$BR_BAD  (expect ok=6)"

echo "=== LT-7 verdict (both tiers FailClose on this stack, both recover) ==="
V=PASS
[ "$IC_BAD" -gt 0 ] || { echo "  interactive FailClose did NOT produce errors"; V=FAIL; }
[ "$IR_OK" -ge 5 ] || { echo "  interactive did not recover"; V=FAIL; }
[ "$BC_BAD" -gt 0 ] || { echo "  batch FailClose did NOT produce errors (unexpected serving)"; V=FAIL; }
[ "$BR_OK" -ge 5 ] || { echo "  batch did not recover"; V=FAIL; }
echo "  => $V  (interactive FailClose bad=$IC_BAD recover ok=$IR_OK; batch FailClose bad=$BC_BAD recover ok=$BR_OK)"
