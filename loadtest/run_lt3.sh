#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# LT-3 idempotency under load with duplicate delivery (the headline correctness test).
# Submits N distinct jobs, each DUP times concurrently, then asserts:
#   distinct jobs complete == N   AND   worker_claims_total delta == N   (exactly).
# Needs: the scaled cluster warm, the submit-api reachable, Prometheus scraping the
# worker PodMonitor, and AWS creds. Records START/END and runs analyze.py.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
: "${SUBMIT_URL:?set SUBMIT_URL (e.g. via kubectl port-forward svc/submit-api 8080:8080)}"
N_JOBS="${N_JOBS:-5000}"; DUP="${DUP:-3}"; RUN_TAG="lt3-$(date +%s)"
USERS="${USERS:-60}"; SPAWN="${SPAWN:-30}"

echo ">> LT-3 run=$RUN_TAG  N=$N_JOBS  DUP=$DUP"
START=$(date +%s)

# Drive the async submissions (headless Locust, stops itself at N_JOBS*DUP).
SUBMIT_URL="$SUBMIT_URL" N_JOBS="$N_JOBS" DUP="$DUP" RUN_TAG="$RUN_TAG" \
  locust -f loadtest/locust_async.py --headless -u "$USERS" -r "$SPAWN" \
         --run-time 30m --only-summary || true

echo ">> submissions done; waiting for the queue to drain and jobs to complete..."
# Wait until SQS is empty (best-effort) and DynamoDB shows N complete, or a timeout.
for _ in $(seq 1 120); do
  done_n=$(PROM_URL="${PROM_URL:-http://localhost:9090}" DDB_TABLE=inference-jobs \
    ./.venv/bin/python -c "import loadtest.analyze as a; print(a.ddb_complete_count('$RUN_TAG'))" 2>/dev/null || echo 0)
  echo "   complete=$done_n / $N_JOBS"
  [ "${done_n:-0}" -ge "$N_JOBS" ] && break
  sleep 15
done
END=$(date +%s)

echo ">> analysis:"
PROM_URL="${PROM_URL:-http://localhost:9090}" DDB_TABLE=inference-jobs \
  ./.venv/bin/python loadtest/analyze.py lt3 --run "$RUN_TAG" --n "$N_JOBS" --start "$START" --end "$END"
