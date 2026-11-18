#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# Phase D: routing-to-the-right-tier, traced. Drives interactive (synchronous) and batch
# (asynchronous, shared-prefix) traffic concurrently, kills a worker mid-flight so one batch
# job is interrupted and recovered, then decides pass or fail from the durable trace:
#   D.1: every batch request served by a batch-tier pod, every interactive by an interactive
#        pod, zero cross-tier (loadtest/phased_verdict.py, from the Envoy access log).
#   D.2: shared-prefix batch jobs co-locate on one pod (inference-aware placement), visible
#        as a per-group upstream concentration and a batch prefix-cache hit rate.
#   E.3: one batch job is interrupted (worker killed) and recovered (claim_count > 1), traced
#        end to end on a batch pod.
# Short claim lease + SQS visibility (25/60) so the interrupted job redelivers and completes
# within the window; both are restored on exit.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1
# Respect caller credentials. Only fall back to the named profile when the caller
# supplied neither a profile nor explicit keys, so temporary session credentials work.
if [ -z "${AWS_PROFILE:-}" ] && [ -z "${AWS_ACCESS_KEY_ID:-}" ]; then
  export AWS_PROFILE=eksinference
fi
TAG="phased-$(date +%s)"
QURL="https://sqs.us-west-2.amazonaws.com/111122223333/inference"
GW="http://$(kubectl -n inference get gateway inference-gateway -o jsonpath='{.status.addresses[0].value}')"
[ "$GW" = "http://" ] && { echo "STOP: gateway has no external address" >&2; exit 1; }
: "${N_BATCH:=30}"
: "${NGROUPS:=3}"
: "${N_INT:=120}"
mkdir -p loadtest/results

OLD_VIS=$(aws sqs get-queue-attributes --queue-url "$QURL" --attribute-names VisibilityTimeout --query 'Attributes.VisibilityTimeout' --output text)
aws sqs set-queue-attributes --queue-url "$QURL" --attributes VisibilityTimeout=60 >/dev/null
kubectl -n inference set env deploy/async-worker CLAIM_TTL_SECONDS=25 >/dev/null
kubectl -n inference rollout status deploy/async-worker --timeout=120s >/dev/null
restore(){ aws sqs set-queue-attributes --queue-url "$QURL" --attributes VisibilityTimeout="${OLD_VIS:-900}" >/dev/null
           kubectl -n inference set env deploy/async-worker CLAIM_TTL_SECONDS=600 >/dev/null
           kill "${PF1:-0}" "${PF2:-0}" "${DINT:-0}" 2>/dev/null; }
trap restore EXIT

kubectl -n inference port-forward svc/submit-api 8080:8080 >/tmp/submit-pf.log 2>&1 & PF1=$!
kubectl -n monitoring port-forward svc/kps-kube-prometheus-stack-prometheus 9090:9090 >/tmp/prom-pf.log 2>&1 & PF2=$!
sleep 8
q(){ curl -sG http://localhost:9090/api/v1/query --data-urlencode "query=$1" | jq -r '.data.result[0].value[1] // "0"'; }

echo ">> TAG=$TAG  batch=$N_BATCH ($NGROUPS prefix-groups)  interactive=$N_INT"
HIT0=$(q 'sum(vllm:prefix_cache_hits_total{tier="batch"})'); QRY0=$(q 'sum(vllm:prefix_cache_queries_total{tier="batch"})')

echo ">> start interactive traced load (bg, synchronous, no objective header -> interactive)"
GATEWAY="$GW" TAG="$TAG" N="$N_INT" CONCURRENCY=8 MAX_TOKENS=16 ./.venv/bin/python loadtest/phased_interactive.py >/tmp/phased_int.out 2>&1 & DINT=$!

echo ">> submit batch shared-prefix jobs (async -> worker -> gateway batch)"
SUBMIT_URL=http://localhost:8080/submit TAG="$TAG" N="$N_BATCH" GROUPS="$NGROUPS" MAX_TOKENS=128 \
  ./.venv/bin/python loadtest/phased_batch.py

echo ">> FAULT: kill one worker mid-flight (interrupted + recovered row for E.3)"
sleep 12
victim=$(kubectl -n inference get pods -l app=async-worker -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
kubectl -n inference delete pod "$victim" --grace-period=0 --force >/dev/null 2>&1 && echo "killed $victim"

echo ">> wait for all $N_BATCH batch jobs to complete (cap 10 min)"
for i in $(seq 1 60); do
  c=$(aws dynamodb scan --table-name inference-jobs --region us-west-2 \
    --filter-expression "begins_with(job_id, :p) AND #s = :c" \
    --expression-attribute-names '{"#s":"status"}' \
    --expression-attribute-values "{\":p\":{\"S\":\"$TAG-\"},\":c\":{\"S\":\"complete\"}}" \
    --select COUNT --query Count --output text 2>/dev/null)
  echo "[$i] batch complete=$c/$N_BATCH"
  [ "${c:-0}" -ge "$N_BATCH" ] && break
  sleep 10
done
wait "$DINT" 2>/dev/null; echo "interactive: $(cat /tmp/phased_int.out 2>/dev/null)"

HIT1=$(q 'sum(vllm:prefix_cache_hits_total{tier="batch"})'); QRY1=$(q 'sum(vllm:prefix_cache_queries_total{tier="batch"})')

echo "=== D.1 cross-tier verdict (from the Envoy access log) ==="
./.venv/bin/python loadtest/phased_verdict.py --tag "$TAG"

echo "=== per-request trace + async routing verdict (build_trace) ==="
./.venv/bin/python loadtest/build_trace.py --run "$TAG-" --objective batch --out loadtest/results/phased_trace.md

echo "=== D.2 inference-aware placement ==="
DH=$(awk "BEGIN{h=$HIT1-$HIT0; qd=$QRY1-$QRY0; if(qd>0) printf \"%.1f\", 100*h/qd; else print \"n/a\"}")
echo "  batch prefix-cache hit rate over the run: ${DH}%  (shared prefixes -> cache reuse from co-location)"
echo "  per-group backend concentration (distinct pods each shared-prefix group landed on):"
./.venv/bin/python - "$TAG" <<'PY'
import json,sys,collections
tag=sys.argv[1]
d=json.load(open("loadtest/results/phased_trace.json"))
g=collections.defaultdict(set)
for r in d["rows"]:
    cid=r["correlation_id"]           # tag-g<n>#hash
    grp=cid.split("#")[0]
    if r.get("backend_pod") and r["upstream"]:
        g[grp].add(r["backend_pod"])
for grp in sorted(g):
    pods=sorted(g[grp])
    print(f"    {grp}: {len(pods)} pod(s) -> {pods}   {'CO-LOCATED' if len(pods)==1 else 'spread'}")
PY

echo "=== E.3 interrupted + recovered job (claim_count > 1, completed on a batch pod) ==="
aws dynamodb scan --table-name inference-jobs --region us-west-2 \
  --filter-expression "begins_with(job_id, :p) AND claim_count > :one" \
  --expression-attribute-values "{\":p\":{\"S\":\"$TAG-\"},\":one\":{\"N\":\"1\"}}" \
  --query 'Items[].{job:job_id.S,status:status.S,claim_count:claim_count.N}' --output json 2>/dev/null | head -30
echo "RUN TAG=$TAG"
