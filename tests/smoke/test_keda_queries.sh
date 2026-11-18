#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# The empty-vector trap: a misspelled metric name returns an empty vector and the
# scaler silently never fires. For each KEDA Prometheus trigger, run its query and
# assert a NON-EMPTY result. Requires kubectl access to Prometheus.
set -euo pipefail
PROM=${PROM:-http://localhost:9090}
qs=$(grep -h 'query:' manifests/04-autoscaling/*.yaml | sed 's/.*query: //')
rc=0
while IFS= read -r q; do
  [ -z "$q" ] && continue
  n=$(curl -sG "$PROM/api/v1/query" --data-urlencode "query=$q" | jq '.data.result | length')
  if [ "$n" -gt 0 ]; then echo "OK   non-empty: $q"; else echo "FAIL empty vector: $q"; rc=1; fi
done <<< "$qs"
exit $rc
