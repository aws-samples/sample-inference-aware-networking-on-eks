#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# Reproducible runner for the headline idempotency test. Port-forwards the submit
# API, runs the test in the project venv, tears the forward down. Requires the
# Stage 2 workloads deployed and AWS_PROFILE set for the DynamoDB poll.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${ROOT}"

# Project venv, built by the one definition in the Makefile (pinned pip, exact-pinned manifests),
# so this script cannot drift from `make venv` or silently pull a different boto3.
[ -x .venv/bin/python ] || make venv

kubectl -n inference rollout status deploy/submit-api --timeout=120s
kubectl -n inference port-forward svc/submit-api 8080:8080 >/tmp/submit-pf.log 2>&1 &
PF_PID=$!
trap 'kill ${PF_PID} 2>/dev/null || true' EXIT

# WAIT FOR THE FORWARD TO BE LISTENING, do not sleep and hope.
#
# `sleep 5` was a RACE, and it lost on 2026-08-08 in the first end-to-end walkthrough run:
#
#   requests.exceptions.ConnectionError: ('Connection aborted.',
#     RemoteDisconnected('Remote end closed connection without response'))
#
# A fixed sleep encodes a guess about how long another process takes to become ready, and the guess
# is wrong exactly when the cluster is busy, which is when a walkthrough reader is most likely to be
# running it. The failure is also MISLEADING: RemoteDisconnected reads as the submit API rejecting
# the request, so the debugger goes to the worker logs (healthy) before the port-forward.
#
# Poll the port instead, with a bound and a loud failure. Same class as the gateway reachability
# fix in tests/lib/discover.sh, which polls rather than sleeping for the same reason.
for _ in $(seq 1 40); do
  if nc -z 127.0.0.1 8080 2>/dev/null; then break; fi
  sleep 1
done
if ! nc -z 127.0.0.1 8080 2>/dev/null; then
  echo "FAIL the port-forward to svc/submit-api never began listening on 127.0.0.1:8080 after 40s."
  echo "     This is the forward, not the submit API. Its log:"
  sed 's/^/       /' /tmp/submit-pf.log 2>/dev/null | tail -5
  exit 1
fi

SUBMIT_URL=http://localhost:8080/submit \
DDB_TABLE=inference-jobs \
AWS_DEFAULT_REGION=us-west-2 \
  ./.venv/bin/python tests/validation/test_idempotency.py
