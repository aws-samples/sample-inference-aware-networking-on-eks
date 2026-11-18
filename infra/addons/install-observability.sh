#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# Observability stack (design section 11): kube-prometheus-stack + a vLLM PodMonitor
# (tier label) + a `prometheus` service alias. Enables the Prometheus-driven KEDA
# ScaledObjects and the seven-KPI dashboards. Idempotent.
# Pin from versions.yaml choices.prometheus_stack.
set -euo pipefail
PROM_VER="87.17.0"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

helm repo add prometheus-community https://prometheus-community.github.io/helm-charts >/dev/null 2>&1 || true
helm repo update prometheus-community

echo ">> kube-prometheus-stack ${PROM_VER} (release kps, namespace monitoring)"
# NilUsesHelmValues=false makes Prometheus select PodMonitors/ServiceMonitors in
# ALL namespaces (default would restrict to the release's own labels), so the vLLM
# PodMonitor in the inference namespace is discovered.
helm upgrade --install kps prometheus-community/kube-prometheus-stack \
  --version "${PROM_VER}" --namespace monitoring --create-namespace \
  --set prometheus.prometheusSpec.podMonitorSelectorNilUsesHelmValues=false \
  --set prometheus.prometheusSpec.serviceMonitorSelectorNilUsesHelmValues=false \
  --set prometheus.prometheusSpec.podMonitorNamespaceSelector= \
  --set prometheus.prometheusSpec.serviceMonitorNamespaceSelector= \
  --wait --timeout 10m

echo ">> vLLM PodMonitor + prometheus service alias"
# The PodMonitor lives in the inference namespace, which stage-1 creates. On a FRESH cluster this
# step runs first, so it creates the namespace from its own manifest (idempotent). Found on the
# 2026-10-31 rebuild: earlier runs passed only because the namespace survived from a previous one.
kubectl apply -f "${ROOT}/manifests/00-namespace-rbac.yaml"
kubectl apply -f "${ROOT}/manifests/05-observability/vllm-podmonitor.yaml"

echo ">> waiting for Prometheus to scrape vllm metrics (tier label)..."
echo "   verify: kubectl -n monitoring port-forward svc/kps-kube-prometheus-stack-prometheus 9090:9090"
echo "   then:   curl -s 'http://localhost:9090/api/v1/query?query=vllm:num_requests_waiting' | jq ."
echo ">> observability installed."
