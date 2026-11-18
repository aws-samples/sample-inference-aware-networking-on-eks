#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# NVIDIA DCGM Exporter (Phase 2.1): per-GPU utilization, framebuffer used/free,
# power, SM occupancy on the g6e nodes. Publishes DCGM_FI_DEV_GPU_UTIL,
# DCGM_FI_DEV_FB_USED, DCGM_FI_DEV_FB_FREE, DCGM_FI_DEV_POWER_USAGE, etc. Scraped by
# the kube-prometheus-stack Prometheus via the chart's ServiceMonitor.
# Pin: versions.yaml choices.dcgm_exporter. DCGM does not work on Neuron (see the
# neuron exporter); dashboards label by accelerator.
set -euo pipefail
VER="4.8.3"
helm repo add gpu-helm-charts https://nvidia.github.io/dcgm-exporter/helm-charts >/dev/null 2>&1 || true
helm repo update gpu-helm-charts
helm upgrade --install dcgm-exporter gpu-helm-charts/dcgm-exporter --version "${VER}" \
  --namespace gpu-monitoring --create-namespace \
  --set serviceMonitor.enabled=true \
  --set-json 'tolerations=[{"key":"nvidia.com/gpu","operator":"Exists","effect":"NoSchedule"}]' \
  --set-json 'nodeSelector={"karpenter.k8s.aws/instance-gpu-manufacturer":"nvidia"}' \
  --wait --timeout 5m
echo ">> DCGM exporter ${VER} installed. One pod per NVIDIA node; metrics on :9400/metrics."
echo "   verify: DCGM_FI_DEV_GPU_UTIL should appear in Prometheus once a g6e node has a pod."
