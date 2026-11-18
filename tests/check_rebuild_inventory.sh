#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# THE REBUILD INVENTORY. Every object the running system requires, its COMMITTED SOURCE, and how to
# verify it exists. The IAM map generalised from identities to everything.
#
# WHY IT EXISTS: THREE RECURRENCES OF ONE SHAPE.
#
#   1. Karpenter's controller role and its minted instance profiles were created OUTSIDE the
#      CloudFormation stack that was supposed to own them. Cost: a teardown that needed four
#      attempts. Fixed structurally by the CFN overlay and INV16.
#   2. The relay's Pod Identity association was absent after the rebuild. Cost: promotion failed at
#      its last step with a 504 that read like an upstream timeout. Fixed by the IAM map recording
#      the coupling, since the script that creates it was already committed.
#   3. The cost safety net and the NVIDIA device plugin were both absent after the rebuild. Cost:
#      22 minutes of a paid g6e idling while a pod sat Pending, and an unbounded-spend exposure
#      that nobody would have noticed until a bill.
#
# THE TWO CAUSES ARE DIFFERENT AND THE DISTINCTION IS THE POINT, found by READING THE SEQUENCE
# rather than remembering the session:
#
#   NO COMMITTED SOURCE      the cost safety net was in no `make` target at all. A genuine gap.
#   SOURCE NOT EXERCISED     `install-device-plugins.sh nvidia` HAS been in `make addons` all
#                            along. The rebuild ran the addon scripts a la carte and skipped it. A
#                            sequence that contains the right step does not help when the sequence
#                            is not what runs.
#
# So this checks the RESULT, never the recipe. "It is in the Makefile" is exactly the reasoning
# that failed.
#
# FAILS CLOSED. Unreachable cluster errors rather than passing.
set -uo pipefail
NS="${NS:-inference}"
fail=0; n=0

command -v kubectl >/dev/null 2>&1 || { echo "FAIL kubectl not on PATH; cannot verify, so this does not pass"; exit 1; }
kubectl version >/dev/null 2>&1 || { echo "FAIL cannot reach the cluster; cannot verify, so this does not pass"; exit 1; }

echo "=== REBUILD INVENTORY: every required object, its source, and whether it is here ==="
printf "%-34s %-46s %s\n" "OBJECT" "COMMITTED SOURCE" "STATE"
printf "%-34s %-46s %s\n" "------" "----------------" "-----"

# check <label> <committed source> <test command>
chk() {
  local label="$1" src="$2"; shift 2
  n=$((n+1))
  if "$@" >/dev/null 2>&1; then
    printf "%-34s %-46s OK\n" "$label" "$src"
  else
    printf "%-34s %-46s MISSING\n" "$label" "$src"
    fail=$((fail+1))
  fi
}

# ---- cluster-scoped platform ----------------------------------------------------------------
chk "karpenter controller"      "infra/addons/install-karpenter.sh"        kubectl -n kube-system get deploy karpenter
chk "karpenter CFN-owned IAM"   "infra/addons/karpenter-cfn-overlay.yaml"  kubectl get ec2nodeclass gpu
chk "keda operator"             "infra/addons/install-keda.sh"             kubectl -n keda get deploy keda-operator
chk "envoy gateway"             "infra/addons/install-gateway-api-inference.sh crds" kubectl -n envoy-gateway-system get deploy envoy-gateway
chk "inference CRDs"            "infra/addons/install-gateway-api-inference.sh crds" kubectl get crd inferencepools.inference.networking.k8s.io
# THE THIRD RECURRENCE, half one. In `make addons` all along; skipped by an a-la-carte rebuild.
chk "nvidia device plugin"      "infra/addons/install-device-plugins.sh nvidia (make addons)" \
    sh -c 'kubectl get ds -A -o name | grep -q nvidia-device-plugin'

# ---- namespace-scoped ------------------------------------------------------------------------
chk "inference namespace + RBAC" "manifests/00-namespace-rbac.yaml"        kubectl get ns "$NS"
# THE THIRD RECURRENCE, half two. Was in NO target. Added to stage-1 on 2026-08-05.
chk "cost safety net"            "manifests/07-safety-net/safety-net.yaml (stage-1)" \
    kubectl -n "$NS" get cronjob cost-safety-net
chk "gateway"                    "manifests/03-routing/gateway.yaml"       kubectl -n "$NS" get gateway inference-gateway
chk "httproute"                  "manifests/03-routing/httproute.yaml"     kubectl -n "$NS" get httproute llm-route
chk "chain-b timeouts"           "manifests/03-routing/chain-b-timeouts.yaml" kubectl -n "$NS" get configmap chain-b-timeouts
chk "relay"                      "manifests/08-relay/relay.yaml"           kubectl -n "$NS" get deploy inference-relay
chk "gateway-endpoint configmap"  "infra/addons/setup-async-infra.sh [5/7]" kubectl -n "$NS" get configmap gateway-endpoint

# ---- identities, the IAM map's rows -----------------------------------------------------------
# THE SECOND RECURRENCE. The relay declares no IAM of its own, so its dependence on this is
# invisible from its manifest. Absent, promotion fails at its last step with a 504.
chk "pod identity: async-worker"  "infra/addons/setup-async-infra.sh [4/7]" \
    sh -c 'aws eks list-pod-identity-associations --cluster-name inference-aware-eks --region us-west-2 --query "associations[?serviceAccount==\`async-worker\`]" --output text | grep -q .'

# ADDED 2026-09-24. THE SWEEPER'S IDENTITY WAS NEVER INVENTORIED, and that is why it was dead for
# an unknown number of batches while every offline gate stayed green: its CronJob ran every minute
# and failed AccessDenied on sqs:GetQueueAttributes as the node instance role, because the role
# inference-sweeper and this association had never been created. infra/addons/setup-sweeper.sh
# creates both and NOTHING CALLED IT. That is the "cure nothing calls" shape in DESIGN.md rule 6,
# and this row is the cure's caller-side proof: it verifies the RESULT, never the recipe.
chk "pod identity: sweeper"       "infra/addons/setup-sweeper.sh [3/3]" \
    sh -c 'aws eks list-pod-identity-associations --cluster-name inference-aware-eks --region us-west-2 --query "associations[?serviceAccount==\`sweeper\`]" --output text | grep -q .'

echo
if [ "$fail" -eq 0 ]; then
  echo "PASS: all $n inventoried objects present"
  exit 0
fi
echo "REBUILD INVENTORY: $fail of $n objects MISSING."
echo "  Each MISSING row names the committed source that should have created it. If a row has no"
echo "  source, that is the finding: an object the system depends on with nothing that recreates it."
echo "  Finding it here costs a command. Finding it at bring-up costs paid idle time."
exit 1
