#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# Install KEDA (pinned in versions.yaml). Idempotent.
# The aws-sqs-queue scaler (Stage 2) authenticates via Pod Identity or IRSA; that
# association is created in Stage 2, not here. Stage 1 does not apply a ScaledObject.
set -euo pipefail
CHART_VER="2.21.0"        # versions.yaml choices.keda_version (v2.20.2); chart == app. Moved from
                          # 2.20.1 with the 2026-08-03 rebuild: patch drift is allowed to move with a
                          # rebuild per design v6.2.3 section 2.2.2, unlike the vLLM pin which is frozen.
helm repo add kedacore https://kedacore.github.io/charts >/dev/null 2>&1 || true
helm repo update kedacore
helm upgrade --install keda kedacore/keda \
  --namespace keda --create-namespace \
  --version "${CHART_VER}" --wait
kubectl -n keda rollout status deploy/keda-operator --timeout=180s
echo ">> KEDA ${CHART_VER} installed."
