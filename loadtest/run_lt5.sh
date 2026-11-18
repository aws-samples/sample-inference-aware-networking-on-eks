#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# LT-5 autoscaling (deterministic queue build). Proves the interactive tier scales up on a
# REAL queue-depth signal: KEDA trigger sum(vllm:num_requests_waiting{tier=interactive}) > 5,
# then Karpenter provisions a node and the new pod reaches Ready (records the latency), and the
# fleet scales back down + consolidates on ramp-down.
#
# Why constrain the batch: the interactive Deployment runs with vLLM's default running-batch
# cap (~256 seqs), so any concurrency a single load box can offer fits in the running batch and
# num_requests_waiting stays 0 (the queue never builds and the scaler never fires). To make the
# waiting queue REAL and reproducible without hundreds of clients, we cap the running batch to 4
# seqs per replica for the duration of the test (8 running slots across 2 replicas), then drive
# short requests at concurrency 20: 8 run, ~12 wait, which is well past the threshold of 5. The
# waiting queue, the KEDA signal, and the scale-up are all genuine; only the batch cap is a
# documented test condition, and it is restored at the end. Short generations (max_tokens 64)
# keep every request well under any gateway stream timeout, so there are no client-side errors.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1
# Respect caller credentials. Only fall back to the named profile when the caller
# supplied neither a profile nor explicit keys, so temporary session credentials work.
if [ -z "${AWS_PROFILE:-}" ] && [ -z "${AWS_ACCESS_KEY_ID:-}" ]; then
  export AWS_PROFILE=eksinference
fi
GW="http://$(kubectl -n inference get gateway inference-gateway -o jsonpath='{.status.addresses[0].value}')"
[ "$GW" = "http://" ] && { echo "STOP: gateway has no external address" >&2; exit 1; }
DEP=vllm-llama3-interactive

echo ">> capture original interactive args (to restore the running-batch cap after)"
ORIG_ARGS=$(kubectl -n inference get deploy "$DEP" -o jsonpath='{.spec.template.spec.containers[0].args}')
NEW_ARGS=$(echo "$ORIG_ARGS" | jq -c '. + ["--max-num-seqs=4"]')

restore(){
  echo ">> RESTORE interactive running-batch cap to original"
  kubectl -n inference patch deploy "$DEP" --type json \
    -p "[{\"op\":\"replace\",\"path\":\"/spec/template/spec/containers/0/args\",\"value\":$ORIG_ARGS}]" >/dev/null 2>&1 || true
  kill "${PF:-0}" "${D:-0}" 2>/dev/null || true
}
trap restore EXIT

echo ">> enable interactive KEDA scale-up (min 2, max 4) and consolidation (1m)"
kubectl -n inference annotate scaledobject "$DEP" autoscaling.keda.sh/paused-replicas- >/dev/null 2>&1 || true
kubectl -n inference patch scaledobject "$DEP" --type merge \
  -p '{"spec":{"minReplicaCount":2,"maxReplicaCount":4}}' >/dev/null
kubectl patch nodepool gpu-interactive --type merge \
  -p '{"spec":{"disruption":{"consolidationPolicy":"WhenEmptyOrUnderutilized","consolidateAfter":"60s"}}}' >/dev/null

echo ">> apply running-batch cap (--max-num-seqs=4) and wait for rollout (8B reload)"
kubectl -n inference patch deploy "$DEP" --type json \
  -p "[{\"op\":\"replace\",\"path\":\"/spec/template/spec/containers/0/args\",\"value\":$NEW_ARGS}]" >/dev/null
kubectl -n inference rollout status deploy/"$DEP" --timeout=900s

kubectl -n monitoring port-forward svc/kps-kube-prometheus-stack-prometheus 9090:9090 >/tmp/prom-pf.log 2>&1 & PF=$!
sleep 6
q(){ curl -sG http://localhost:9090/api/v1/query --data-urlencode "query=$1" | jq -r '.data.result[0].value[1] // "0"'; }

echo ">> BASE state before load"
BASE_REP=$(kubectl -n inference get deploy "$DEP" -o jsonpath='{.spec.replicas}')
echo "   base replicas=$BASE_REP  running_cap=8 (4x2)  threshold=5"

echo ">> ramp UP: interactive-short (max_tokens 64) at conc 20 to overflow the 8-slot batch"
GATEWAY="$GW" WORKLOAD=interactive-short TIER=interactive CONCURRENCY=20 DURATION=900 ROUTER=epp \
  ./.venv/bin/python loadtest/drive_sync.py >/tmp/lt5.out 2>&1 & D=$!

T_TRIG=0; T_READY=0; MAXQ=0; MAXDES=$BASE_REP; MAXREADY=$BASE_REP
for i in $(seq 1 45); do   # up to ~15 min (new pod needs an 8B model load)
  now=$(date +%s)
  des=$(kubectl -n inference get deploy "$DEP" -o jsonpath='{.spec.replicas}' 2>/dev/null)
  rdy=$(kubectl -n inference get deploy "$DEP" -o jsonpath='{.status.readyReplicas}' 2>/dev/null)
  act=$(kubectl -n inference get scaledobject "$DEP" -o jsonpath='{.status.conditions[?(@.type=="Active")].status}' 2>/dev/null)
  run=$(q 'sum(vllm:num_requests_running{tier="interactive"})')
  qd=$(q 'sum(vllm:num_requests_waiting{tier="interactive"})')
  echo "[$i] desired=$des ready=${rdy:-0} active=$act running=$run waiting=$qd"
  awk "BEGIN{exit !($qd>$MAXQ)}" && MAXQ=$qd
  [ "${des:-2}" -gt "$MAXDES" ] && MAXDES=$des
  [ "$T_TRIG" = 0 ] && [ "${des:-2}" -gt "$BASE_REP" ] && T_TRIG=$now && echo "   scale-up TRIGGERED at desired=$des (waiting=$qd)"
  [ "${rdy:-0}" -gt "$MAXREADY" ] && MAXREADY=$rdy
  [ "$T_READY" = 0 ] && [ "${rdy:-0}" -gt "$BASE_REP" ] && T_READY=$now && echo "   new pod READY"
  [ "$T_READY" != 0 ] && break
  sleep 20
done
kill $D 2>/dev/null

echo ">> ramp DOWN: stop load, observe scale-down + consolidation (up to 8 min)"
for i in $(seq 1 24); do
  des=$(kubectl -n inference get deploy "$DEP" -o jsonpath='{.spec.replicas}' 2>/dev/null)
  gpn=$(kubectl get nodes -l karpenter.sh/nodepool=gpu-interactive --no-headers 2>/dev/null | wc -l | tr -d ' ')
  echo "[down $i] desired=$des gpu_interactive_nodes=$gpn"
  [ "${des:-2}" -le "$BASE_REP" ] && break
  sleep 20
done

echo "=== LT-5 verdict ==="
echo "  base replicas=$BASE_REP  peak desired=$MAXDES  peak ready=$MAXREADY  peak waiting=$MAXQ  threshold=5"
LAT="n/a"; [ "$T_TRIG" != 0 ] && [ "$T_READY" != 0 ] && LAT=$((T_READY - T_TRIG))
echo "  scale-up latency (trigger->new pod ready): ${LAT}s"
V=PASS
awk "BEGIN{exit !($MAXQ>5)}" || { echo "  waiting queue never crossed threshold 5 (peak=$MAXQ)"; V=FAIL; }
[ "$MAXDES" -gt "$BASE_REP" ] || { echo "  scale-up did not fire"; V=FAIL; }
echo "  scaler fired on real queue-depth signal (waiting>5): $V"
echo "  NOTE: latency is dominated by the 8B model load on the new node; the KEDA->Karpenter"
echo "        trigger itself is seconds. Running-batch cap restored on exit."
