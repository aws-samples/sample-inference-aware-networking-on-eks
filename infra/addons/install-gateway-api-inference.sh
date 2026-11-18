#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# Install the routing stack: Envoy Gateway, Agent Router (formerly Envoy AI Gateway), the Gateway API
# Inference Extension CRDs, and one Endpoint Picker per pool.
#
# Supported path for the gateway/pool/EPP trio (DESIGN.md directive 4): a chart OWNS the
# InferencePool + EPP + RBAC. manifests/03-routing/inferencepools.yaml and epp-reference.yaml are
# REFERENCE SHAPES to diff against, not applied live.
#
# MOVED 2026-10-31 for Amazon EKS 1.37 (read from the upstream release notes that day):
#   - Gateway API Inference Extension v1.6.0 moved the full Endpoint Picker, its chart and its
#     InferenceObjective API to llm-d. v1.6.2 ships only the InferencePool CRD (still
#     inference.networking.k8s.io/v1), so the EPP now comes from the llm-d router chart v1.0.0 and
#     InferenceObjective from llm-d's CRDs (llm-d.ai/v1; v1alpha2 is deprecated there).
#   - Agent Router v1.2.0 (repository theagentrouter/agent-router; charts keep the ai-gateway names)
#     needs Envoy Gateway v1.9.2 or later and Inference Extension v1.6.2.
#   - The llm-d EPP reads x-llm-d-inference-objective and still accepts x-gateway-inference-objective
#     as a deprecated alias. The HTTPRoute matches the old header at the gateway, so it is kept.
# The chart still names the pool after the release and the EPP <release>-epp on port 9002, so the
# HTTPRoute and invariants INV1 to INV3 are unchanged (checked with `helm template` on 2026-10-31).
set -euo pipefail
GIE_VER="v1.6.2"            # versions.yaml choices.gateway_api_inference_extension_crds
GIE_CRDS_SHA256="38bfba9c452ab54b1afd1f4df15e75b3238c788d8ab007d2f469e6f7ae16fc9f"
LLMD_VER="v1.0.0"           # versions.yaml choices.llmd_router_version (EPP chart + InferenceObjective CRDs)
LLMD_CRDS_SHA256="bae0ae787ec97494ff21933882810fb49bdec88b2cf6fe14539ffeedd2ba21e3"
EG_VER="v1.9.3"             # versions.yaml choices.envoy_gateway_version (bundles Gateway API v1.6.1)
AIEG_VER="v1.2.0"           # versions.yaml choices.envoy_ai_gateway_version (Agent Router)
NS="inference"
EPP_CHART="oci://ghcr.io/llm-d/charts/llm-d-router-gateway"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Pin by content as well as by URL: a changed file at a pinned tag is investigated, never accepted.
apply_pinned() {  # apply_pinned <url> <sha256>
  local url="$1" want="$2" tmp
  tmp="$(mktemp)"
  curl -fsSL "${url}" -o "${tmp}"
  local got
  got="$(shasum -a 256 "${tmp}" | awk '{print $1}')"
  if [ "${got}" != "${want}" ]; then
    echo "ERROR: checksum mismatch for ${url}"; echo "       expected ${want}"; echo "       got      ${got}"
    rm -f "${tmp}"; exit 1
  fi
  kubectl apply --server-side -f "${tmp}"
  rm -f "${tmp}"
}

# deploy_epp <tier> <failureMode>
# Release name = vllm-llama3-<tier>  -> InferencePool vllm-llama3-<tier>,
#                                       EPP Deployment and Service vllm-llama3-<tier>-epp.
# The chart defaults to FailOpen and to 8 CPU / 8Gi requests with a 16Gi limit, so all three are set.
# Resources are sized small for a smoke build. Production: raise them, run >=2 replicas, add a PDB.
deploy_epp() {
  local tier="$1" fmode="$2"
  helm upgrade --install "vllm-llama3-${tier}" "${EPP_CHART}" --version "${LLMD_VER}" \
    --namespace "${NS}" \
    --set provider.name=none \
    --set "router.inferencePool.failureMode=${fmode}" \
    --set router.modelServers.type=vllm \
    --set "router.modelServers.matchLabels.app=vllm-llama3" \
    --set "router.modelServers.matchLabels.tier=${tier}" \
    --set router.epp.resources.requests.cpu=500m \
    --set router.epp.resources.requests.memory=1Gi \
    --set router.epp.resources.limits.memory=4Gi
}

case "${1:-all}" in
  crds)
    # Base Envoy Gateway does NOT accept an InferencePool backendRef on its own (ResolvedRefs=False,
    # reason=InvalidKind, 500). The Agent Router controller runs an extension server that Envoy
    # Gateway's extensionManager calls; backendResources then registers InferencePool v1 as a
    # routable backend. Order matters: the controller must exist before Envoy Gateway is pointed at
    # it, or Envoy Gateway xDS translation fails.
    echo ">> [1] Envoy Gateway ${EG_VER} (plain; bundles Gateway API CRDs)"
    helm upgrade --install envoy-gateway oci://docker.io/envoyproxy/gateway-helm \
      --version "${EG_VER}" --namespace envoy-gateway-system --create-namespace --wait
    echo ">> [2] Inference Extension CRDs ${GIE_VER} (InferencePool v1) and llm-d CRDs ${LLMD_VER} (InferenceObjective llm-d.ai/v1)"
    apply_pinned "https://github.com/kubernetes-sigs/gateway-api-inference-extension/releases/download/${GIE_VER}/manifests.yaml" "${GIE_CRDS_SHA256}"
    apply_pinned "https://github.com/llm-d/llm-d-router/releases/download/${LLMD_VER}/manifests.yaml" "${LLMD_CRDS_SHA256}"
    kubectl wait --for=condition=Established --timeout=60s \
      crd/inferencepools.inference.networking.k8s.io \
      crd/inferenceobjectives.llm-d.ai
    echo ">> [3] Agent Router ${AIEG_VER} (CRDs + controller = the extension server)"
    helm upgrade -i aieg-crd oci://docker.io/envoyproxy/ai-gateway-crds-helm --version "${AIEG_VER}" \
      --namespace envoy-ai-gateway-system --create-namespace
    helm upgrade -i aieg oci://docker.io/envoyproxy/ai-gateway-helm --version "${AIEG_VER}" \
      --namespace envoy-ai-gateway-system
    kubectl wait --timeout=180s -n envoy-ai-gateway-system deployment/ai-gateway-controller --for=condition=Available
    echo ">> [4] reconfigure Envoy Gateway with Agent Router integration + InferencePool backend, restart"
    helm upgrade -i envoy-gateway oci://docker.io/envoyproxy/gateway-helm --version "${EG_VER}" \
      --namespace envoy-gateway-system -f "${SCRIPT_DIR}/envoy-gateway-values.yaml" --wait
    kubectl -n envoy-gateway-system rollout restart deploy/envoy-gateway
    kubectl -n envoy-gateway-system rollout status deploy/envoy-gateway --timeout=180s
    ;;
  epp-interactive) deploy_epp interactive FailClose ;;   # invariant 6
  # batch is FailClose too: Agent Router v1.0.0 through v1.2.0 does not honor FailOpen for
  # InferencePool (it never sets failure_mode_allow on the injected ext_proc filter, and that filter
  # is not reachable by EnvoyPatchPolicy; upstream issue #2502, still open on 2026-10-31). Proven by
  # LT-7 and the Envoy config_dump at v1.0.0. Design intent for batch is FailOpen; revisit when the
  # stack supports it.
  epp-batch)       deploy_epp batch      FailClose ;;    # invariant 6 (see note)
  epp)             deploy_epp "${2:?usage: $0 epp <tier>}" FailClose ;;   # premium, economy
  all)
    "$0" crds
    "$0" epp-interactive
    echo ">> Apply the GatewayClass + Gateway: kubectl apply -f manifests/03-routing/gateway.yaml"
    ;;
  *) echo "usage: $0 [crds|epp-interactive|epp-batch|epp <tier>|all]"; exit 1;;
esac
