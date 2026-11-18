#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# Device plugins. Stage 1 needs only NVIDIA (g6e nodes advertise nvidia.com/gpu).
# Neuron and EFA plugins are installed at their own stages (4 and E).
# The AL2023 accelerated AMI ships the NVIDIA driver + container toolkit but NOT
# the device plugin, so it must be installed separately.
# MOFED: only matters alongside EFA (Stage E). With EFA present on NVIDIA nodes,
# NVIDIA k8s-device-plugin >=0.19.0 defaults MOFED on and fights the EFA plugin
# over /dev/infiniband, so set mofedEnabled=false there. Stage 1 has no EFA.
set -euo pipefail
MODE="${1:-nvidia}"    # nvidia | neuron | efa

# versions.yaml is the single source of truth (DESIGN.md prime directive 2). Read the pins from
# it at runtime instead of hardcoding, so a version can only ever be changed in one place. _ver
# extracts a scalar `key: "value"` (quotes and trailing comment stripped) and HARD-FAILS if the
# key is absent, so the script can never silently fall back to an unpinned default.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VERSIONS_FILE="${VERSIONS_FILE:-${SCRIPT_DIR}/../../versions.yaml}"
_ver() {
  local key="$1" val
  val="$(grep -E "^[[:space:]]*${key}:" "${VERSIONS_FILE}" | head -1 \
        | sed -E 's/^[^:]*:[[:space:]]*//; s/[[:space:]]*#.*$//; s/^"//; s/"[[:space:]]*$//')"
  if [ -z "${val}" ]; then
    echo "ERROR: ${key} not found in ${VERSIONS_FILE} (versions.yaml is the source of truth)" >&2
    exit 1
  fi
  printf '%s' "${val}"
}

case "${MODE}" in
  nvidia)
    NVDP_VER="${NVDP_VER:-$(_ver nvidia_device_plugin)}"   # from versions.yaml choices.nvidia_device_plugin; resolve latest with: helm search repo nvdp/nvidia-device-plugin --versions
    helm repo add nvdp https://nvidia.github.io/k8s-device-plugin >/dev/null 2>&1 || true
    helm repo update nvdp
    # The chart's default affinity REQUIRES Node Feature Discovery labels
    # (feature.node.kubernetes.io/pci-10de.present). We do not run NFD, so override
    # the affinity to target Karpenter's own GPU label instead. Also tolerate the
    # nvidia.com/gpu taint so the plugin lands on the tainted GPU nodes.
    helm upgrade --install nvdp nvdp/nvidia-device-plugin \
      --namespace nvidia-device-plugin --create-namespace \
      --version "${NVDP_VER}" \
      --set-string mofedEnabled=false \
      --set "tolerations[0].key=nvidia.com/gpu,tolerations[0].operator=Exists,tolerations[0].effect=NoSchedule" \
      --set-json 'affinity={"nodeAffinity":{"requiredDuringSchedulingIgnoredDuringExecution":{"nodeSelectorTerms":[{"matchExpressions":[{"key":"karpenter.k8s.aws/instance-gpu-manufacturer","operator":"In","values":["nvidia"]}]}]}}}' \
      --wait
    echo ">> NVIDIA device plugin ${NVDP_VER} installed (mofedEnabled=false)."
    echo "   Verify once a g6e node joins: kubectl get nodes -o=custom-columns=NAME:.metadata.name,GPU:.status.allocatable.'nvidia\\.com/gpu'"
    ;;
  neuron)
    # Stage 4. Advertises aws.amazon.com/neuron (canonical) and aws.amazon.com/neuroncore.
    # NOTE: AWS now ships a Neuron DRA driver (recommended for EKS 1.34+), an alternative to
    # this device plugin. NOT adopted here: it needs Karpenter-compat testing, and this project
    # already found the EFA DRA driver incompatible with Karpenter. Device plugin stays the path.
    # The old name aws.amazon.com/neurondevice was renamed to neuron and removed in Neuron 2.20.
    # Pinned to the v2.31.0 SDK tag (matches choices.vllm_neuron_dlc_image, NOT master), image neuron-device-plugin:2.22.4.0.
    # Pinned from versions.yaml choices.neuron_device_plugin_sdk_tag. This applies the raw manifest
    # at a fixed SDK tag (verified: the tag resolves and deploys neuron-device-plugin:2.22.4.0).
    # FOLLOW-UP: AWS also publishes an aws-neuron/neuron-helm-charts chart; migrating to it would be
    # tidier, but the exact chart version must be verified against its primary source before pinning
    # (prime directive 1), which needs a network check not done here. [Data needed]: chart version.
    NEURON_SDK_TAG="${NEURON_SDK_TAG:-$(_ver neuron_device_plugin_sdk_tag)}"
    kubectl apply -f "https://raw.githubusercontent.com/aws-neuron/aws-neuron-sdk/${NEURON_SDK_TAG}/src/k8/k8s-neuron-device-plugin.yml"
    kubectl apply -f "https://raw.githubusercontent.com/aws-neuron/aws-neuron-sdk/${NEURON_SDK_TAG}/src/k8/k8s-neuron-device-plugin-rbac.yml"
    echo ">> Neuron device plugin applied from tag ${NEURON_SDK_TAG}."
    ;;
  efa)
    # Stage E. NOT DRA under Karpenter. Chart pinned from versions.yaml choices.efa_device_plugin_chart.
    EFA_CHART_VER="$(_ver efa_device_plugin_chart)"
    helm repo add eks https://aws.github.io/eks-charts >/dev/null 2>&1 || true
    helm repo update eks
    helm upgrade --install aws-efa-k8s-device-plugin eks/aws-efa-k8s-device-plugin \
      --namespace kube-system --version "${EFA_CHART_VER}" --wait
    echo ">> EFA device plugin ${EFA_CHART_VER} installed."
    ;;
  *) echo "usage: $0 [nvidia|neuron|efa]"; exit 1;;
esac
