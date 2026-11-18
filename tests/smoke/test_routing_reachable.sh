#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# Stage 1 gate: a /v1 request reaches a vLLM pod through the EPP and returns a
# completion. GATEWAY is the ALB or gateway address. The model name is read from the
# live interactive Deployment so this stays correct if the served model changes (a
# hardcoded stale name yields a 404 from vLLM, which masks a working route as a failure).
set -euo pipefail
# DISCOVERED, not required. `make stage-1` calls this test, and demanding an environment variable
# the published walkthrough never mentions made stage-1 fail AS PRINTED for a reader on 2026-08-07.
# The address is discoverable; GATEWAY remains as an override. See tests/lib/discover.sh.
. "$(dirname "$0")/../lib/discover.sh"
GATEWAY=$(gateway_reachable) || exit 1
trap gateway_cleanup EXIT
echo "  gateway (reachable): $GATEWAY"
MODEL=${MODEL:-$(kubectl -n inference get deploy vllm-llama3-interactive \
  -o jsonpath='{.spec.template.spec.containers[0].args}' \
  | grep -o 'model=[^"]*' | head -1 | cut -d= -f2)}
: "${MODEL:?could not resolve served model from the interactive Deployment}"
code=$(curl -s -o /tmp/resp.json -w '%{http_code}' \
  -X POST "$GATEWAY/v1/chat/completions" \
  -H 'content-type: application/json' \
  -d "{\"model\":\"$MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"ping\"}],\"max_tokens\":8}")
[ "$code" = "200" ] && echo "OK routing reachable (200)" || { echo "FAIL http $code"; cat /tmp/resp.json; exit 1; }
