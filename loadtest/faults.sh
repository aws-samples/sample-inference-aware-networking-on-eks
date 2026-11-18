#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# Named, repeatable fault-injection actions for the load-test suite (Phase 2.4).
# Requires AWS_PROFILE set and kubectl context on the target cluster.
# Usage: bash loadtest/faults.sh <action> [args]
set -uo pipefail
NS=inference
action="${1:-help}"; shift || true

# BOUND TO THIS PROJECT'S CLUSTER (deep security scan finding M2). These actions force-delete pods,
# uninstall releases and drain a node, so run with kubectl pointed elsewhere they would injure
# another cluster. The guard verifies the current context against the cluster endpoint, and every
# kubectl and helm call below, including those of child scripts, is pinned to that context.
case "$action" in
  kill-workers|drain-batch-node|kill-epp|restore-epp)
    REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
    ctx=$(bash "$REPO/infra/cluster/require-cluster-context.sh" "$REPO/infra/cluster/eksctl-cluster.yaml") || exit 1
    [ "$ctx" != ABSENT ] || { echo "REFUSING: the cluster does not exist" >&2; exit 1; }
    kubectl() { command kubectl --context "$ctx" "$@"; }
    helm() { command helm --kube-context "$ctx" "$@"; }
    export ctx
    export -f kubectl helm
    ;;
esac

case "$action" in
  kill-workers)            # kill a FRACTION of async-worker pods mid-generation (LT-6)
    frac="${1:-0.5}"
    pods=(); while IFS= read -r p; do pods+=("$p"); done < <(kubectl -n "$NS" get pods -l app=async-worker -o name)
    n=${#pods[@]}; k=$(python3 -c "import math;print(max(1,math.floor($n*$frac)))")
    echo "killing $k of $n worker pods"
    for p in "${pods[@]:0:$k}"; do kubectl -n $NS delete "$p" --grace-period=0 --force; done
    ;;
  drain-batch-node)        # simulate a Spot reclaim of one batch node (LT-6)
    node=$(kubectl get nodes -l karpenter.sh/nodepool=gpu-batch -o name | head -1)
    [ -z "$node" ] && { echo "no batch node"; exit 1; }
    echo "cordon+drain $node"
    kubectl cordon "$node"
    kubectl drain "$node" --ignore-daemonsets --delete-emptydir-data --force --grace-period=30 --timeout=120s
    ;;
  kill-epp)                # delete an EPP to simulate an outage (LT-7): interactive|batch
    tier="${1:?tier interactive|batch}"
    echo "deleting EPP for $tier (helm release vllm-llama3-$tier)"
    helm -n $NS uninstall "vllm-llama3-$tier" 2>/dev/null || \
      kubectl -n $NS delete deploy "vllm-llama3-$tier-epp"
    ;;
  restore-epp)             # reinstall the EPP (LT-7 recovery): interactive|batch
    tier="${1:?tier interactive|batch}"
    fmode=$([ "$tier" = interactive ] && echo FailClose || echo FailOpen)
    bash infra/addons/install-gateway-api-inference.sh "epp-$tier"
    echo "restored EPP for $tier ($fmode)"
    ;;
  force-dup)
    echo "duplicate submission is driven by the async load generator: set DUP=<D> and"
    echo "run make loadtest-lt3. Each distinct job is submitted D times concurrently."
    ;;
  *)
    echo "actions: kill-workers <frac> | drain-batch-node | kill-epp <tier> | restore-epp <tier> | force-dup"
    ;;
esac
