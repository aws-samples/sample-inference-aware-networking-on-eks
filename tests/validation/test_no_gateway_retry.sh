#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# Proves the gateway does NOT retry inference requests (ARCHITECTURE-REVIEW.md R.2, 1.2).
#
# A gateway-level retry is a duplication surface BELOW the DynamoDB claim: the claim already
# succeeded, so a retry that re-issues a long inference request to a second backend produces a
# second GPU run for one claimed job, and the claim cannot prevent it. The architecture keeps
# retries off on the inference routes. This test reads the running Envoy config and asserts no
# retry_policy is present on any route, so a future platform-default change that enables retries
# is caught. It fails closed: if the Envoy admin cannot be reached, it errors rather than passing.
set -euo pipefail
NS=envoy-gateway-system
POD="$(kubectl -n "$NS" get pods -o name 2>/dev/null | grep envoy-inference | head -1)"
[ -n "$POD" ] || { echo "FAIL: no envoy-inference proxy pod found"; exit 1; }
kubectl -n "$NS" port-forward "$POD" 19000:19000 >/tmp/envoy-admin-retry.log 2>&1 &
PF=$!; trap 'kill $PF 2>/dev/null' EXIT
sleep 5
DUMP="$(curl -s --max-time 15 http://localhost:19000/config_dump)"
[ -n "$DUMP" ] || { echo "FAIL: could not read Envoy config_dump"; exit 1; }
N="$(printf '%s' "$DUMP" | grep -c 'retry_policy' || true)"
if [ "${N:-0}" -eq 0 ]; then
  echo "PASS: no retry_policy on any inference route (gateway does not retry; duplication surface closed)"
  exit 0
else
  echo "FAIL: found $N retry_policy entries in the running Envoy config. A gateway retry can"
  echo "      re-issue a claimed inference request to a second backend, producing a duplicate GPU"
  echo "      run below the claim. Disable retries on the inference routes or scope them to"
  echo "      connect failures only (never timeouts or a backend that has begun generating)."
  exit 1
fi
