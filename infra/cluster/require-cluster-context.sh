#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# Print the kubectl context that reaches THIS project's cluster, or refuse.
#
# Why (deep security scan finding M2, 2026-11-05): `make cluster-down` ran `kubectl delete` against
# whatever cluster the current context named. Run with kubectl pointed at another cluster, it would
# delete every LoadBalancer Service and every Karpenter NodeClaim in THAT cluster.
#
# The check compares the API server URL of the current context with the endpoint AWS reports for
# the cluster named in the eksctl config, in the region named there. Context names and aliases are
# not trusted: two kubeconfigs can reuse a name, and a name says nothing about which server it reaches.
#
# Usage: bash infra/cluster/require-cluster-context.sh [eksctl-config]
# Stdout, exactly one line:
#   <context>   the current context reaches this cluster; pass it to kubectl as --context
#   ABSENT      AWS reports no such cluster in this account and region, so there is nothing for
#               kubectl to delete
# Every other case exits 1 with the reason on stderr, including any error reading either side.
set -euo pipefail

cfg="${1:-infra/cluster/eksctl-cluster.yaml}"
meta() { awk -v k="$1:" '/^metadata:/{m=1;next} m&&/^[^ #]/{exit} m&&$1==k{print $2;exit}' "$cfg"; }
name=$(meta name)
region=$(meta region)
if [ -z "$name" ] || [ -z "$region" ]; then
  echo "REFUSING: cannot read metadata.name and metadata.region from $cfg" >&2
  exit 1
fi

rc=0
endpoint=$(aws eks describe-cluster --region "$region" --name "$name" \
             --query cluster.endpoint --output text 2>&1) || rc=$?
if [ "$rc" -ne 0 ]; then
  case "$endpoint" in
    *ResourceNotFoundException*) echo ABSENT; exit 0 ;;
  esac
  echo "REFUSING: aws eks describe-cluster failed for $name in $region:" >&2
  printf '  %s\n' "$endpoint" >&2
  exit 1
fi
case "$endpoint" in
  https://*) ;;
  *) echo "REFUSING: unexpected endpoint for $name in $region: '$endpoint'" >&2; exit 1 ;;
esac

ctx=$(kubectl config current-context 2>/dev/null) || ctx=""
if [ -z "$ctx" ]; then
  echo "REFUSING: kubectl has no current context." >&2
  echo "  Run: aws eks update-kubeconfig --region $region --name $name" >&2
  exit 1
fi
# THE NAME IS PRINTED INTO A MAKEFILE, which re-expands it, and into shell command lines, so only
# characters that are inert in both are accepted: `$`, quotes, spaces and backslashes are refused.
# That covers the names AWS and eksctl write (an EKS cluster ARN, user@cluster.region.eksctl.io).
# ABSENT is refused too, because it is this script's own "no cluster" answer.
safe='^[A-Za-z0-9._:@/+=-]{1,253}$'
if ! [[ "$ctx" =~ $safe ]] || [ "$ctx" = ABSENT ]; then
  echo "REFUSING: the current kubectl context name contains characters this script does not pass on." >&2
  echo "  Run: aws eks update-kubeconfig --region $region --name $name --alias $name" >&2
  exit 1
fi
server=$(kubectl config view --minify --context "$ctx" \
           -o jsonpath='{.clusters[0].cluster.server}' 2>/dev/null) || server=""
if [ "$server" != "$endpoint" ]; then
  echo "REFUSING: the current kubectl context '$ctx' reaches '${server:-nothing}'," >&2
  echo "  not $name in $region ($endpoint)." >&2
  echo "  Run: aws eks update-kubeconfig --region $region --name $name" >&2
  exit 1
fi
printf '%s\n' "$ctx"
