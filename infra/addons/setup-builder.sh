#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# IAM for the in-cluster BuildKit builder. EKS Pod Identity, no long-lived credentials.
# Least privilege: push and pull on THIS PROJECT'S two repositories only. ecr:GetAuthorizationToken
# is account-scoped by AWS design and cannot be narrowed to a repository, which is stated here so
# the wildcard reads as a documented AWS constraint rather than laziness.
set -euo pipefail
CLUSTER="${CLUSTER_NAME:-inference-aware-eks}"; REGION="${AWS_REGION:-us-west-2}"; NS=inference
ROLE=inference-builder
SD="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
echo ">> [1/3] role ${ROLE}"
aws iam get-role --role-name "$ROLE" >/dev/null 2>&1 \
  || aws iam create-role --role-name "$ROLE" \
       --assume-role-policy-document "file://${SD}/../../async-worker/iam/builder-trust-policy.json" >/dev/null
aws iam put-role-policy --role-name "$ROLE" --policy-name builder-access \
  --policy-document "file://${SD}/../../async-worker/iam/builder-policy.json"
echo ">> [2/3] pod identity association ${NS}/builder"
if ! aws eks list-pod-identity-associations --cluster-name "$CLUSTER" --namespace "$NS" --region "$REGION" \
      --query 'associations[?serviceAccount==`builder`]' --output text | grep -q .; then
  aws eks create-pod-identity-association --cluster-name "$CLUSTER" --namespace "$NS" \
    --service-account builder \
    --role-arn "arn:aws:iam::$(aws sts get-caller-identity --query Account --output text):role/${ROLE}" \
    --region "$REGION" >/dev/null
fi
echo ">> [3/3] done. Builder can push to inference-async-worker and inference-builder only."
