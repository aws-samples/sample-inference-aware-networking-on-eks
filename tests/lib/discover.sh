#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# ADDRESS DISCOVERY. Sourced by any test that needs a cluster address.
#
# THE CLASS THIS CLOSES, and it is the third instance rather than a one-off:
#   1. ENVOY_GATEWAY_SERVICE, a placeholder a manifest carried until preflight refused it.
#   2. The worker's SQS_QUEUE_URL, hardcoded against the sanitization placeholder account
#      111122223333, which crash-looped the first time anything needed it.
#   3. GATEWAY in tests/smoke/test_routing_reachable.sh, which made `make stage-1` fail AS PRINTED
#      for a reader following the published walkthrough on 2026-08-07.
#
# All three are the same shape: A VALUE THAT IS DISCOVERABLE FROM THE CLUSTER WAS REQUIRED TO BE
# CONFIGURED. Configured values go stale, get sanitized into placeholders, and turn a working system
# into a failing instruction. Discovered values cannot.
#
# Precedence, explicit rather than accidental:
#   1. the environment variable, if set and non-empty, AND IT ANNOUNCES ITSELF (see below)
#   2. discovery from the cluster
#   3. a loud failure naming both, never a silent default
set -uo pipefail

# Discovery proper. Separated from the override so the override path can call it to report what it
# is overriding, without recursing.
_discover_gateway_from_cluster() {
  local svc base ns=envoy-gateway-system
  # Matched by LABEL rather than by a generated name this file would have to restate and keep in
  # sync: Envoy Gateway derives the Service name from the Gateway resource.
  svc=$(kubectl -n "$ns" get svc -l gateway.envoyproxy.io/owning-gateway-name=inference-gateway \
          -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
  if [ -n "$svc" ]; then
    printf 'http://%s.%s.svc.cluster.local' "$svc" "$ns"
    return 0
  fi
  # Fallback: the ConfigMap the async worker already reads, published by the setup script. Using it
  # keeps the test and the worker agreeing about the address rather than discovering it two ways.
  base=$(kubectl -n inference get cm gateway-endpoint -o jsonpath='{.data.base}' 2>/dev/null)
  if [ -n "$base" ]; then
    printf '%s' "$base"
    return 0
  fi
  return 1
}

# THE OVERRIDE ANNOUNCES ITSELF, and that is the whole point of this block.
#
# The environment variable is the operator's escape hatch and MUST stay: discovery must not prevent
# pointing a test somewhere else on purpose. But an override is also the exact shape of the defect
# this library closes, a configured value silently defeating a working mechanism, and a stale
# override that quietly wins is indistinguishable from discovery working. That is how the
# ENVOY_GATEWAY_SERVICE placeholder and the 111122223333 queue URL both survived as long as they did.
#
# So the override is PERMITTED AND NAMED rather than prohibited: the run prints that it overrode,
# prints what discovery would have returned, and says so explicitly when the two disagree. INV19's
# treatment (report the disagreement) rather than a ban.
discover_gateway() {
  if [ -n "${GATEWAY:-}" ]; then
    if [ "${DISCOVER_QUIET:-0}" != "1" ]; then
      local would
      would=$(_discover_gateway_from_cluster 2>/dev/null || true)
      {
        echo "  NOTE GATEWAY is set and OVERRIDES discovery."
        echo "       override : $GATEWAY"
        echo "       discovery would have returned: ${would:-<nothing discoverable>}"
        if [ -n "$would" ] && [ "$would" != "$GATEWAY" ]; then
          echo "       THESE DISAGREE. If the override is stale, this line is the only thing that says so."
        fi
      } >&2
    fi
    printf '%s' "$GATEWAY"
    return 0
  fi
  _discover_gateway_from_cluster
}

require_gateway() {
  local g
  g=$(discover_gateway) || true
  if [ -z "${g:-}" ]; then
    {
      echo "FAIL could not discover the inference gateway address, and GATEWAY is unset."
      echo "     Discovery looks for a Service labelled"
      echo "     gateway.envoyproxy.io/owning-gateway-name=inference-gateway in envoy-gateway-system,"
      echo "     then falls back to the gateway-endpoint ConfigMap in the inference namespace."
      echo "     Override with GATEWAY=http://host:port to point at something else on purpose."
    } >&2
    return 1
  fi
  printf '%s' "$g"
}

# REACHABILITY, which is a different question from discovery and the run of 2026-08-08 proved it.
#
# Discovery returned the correct address and the test still failed, with curl exit 6, "could not
# resolve host". The gateway Service is ClusterIP, so `...svc.cluster.local` is resolvable INSIDE
# the cluster and meaningless outside it. A reader running `make stage-1` from a laptop hits exactly
# that: the walkthrough was still not executable as printed, with the defect moved rather than
# removed.
#
# THE FIX IS NOT A THIRD ADDRESS SOURCE. It is to notice that an in-cluster name needs an in-cluster
# path, and to build one: port-forward the Service and hand back a local address.
#
# THE FORWARD'S PID GOES TO A FILE, NOT A VARIABLE (fixed 2026-11-02). Callers use
# GATEWAY=$(gateway_reachable), and a command substitution is a subshell, so an exported
# GATEWAY_PF_PID never reached the caller: gateway_cleanup saw nothing and every run leaked a
# kubectl port-forward. $$ is the CALLER's PID even inside the subshell, so the file is keyed on it.
gateway_reachable() {
  local g pf_port svc ns
  g=$(require_gateway) || return 1
  case "$g" in
    *svc.cluster.local*|*.svc*)
      ns=envoy-gateway-system
      svc=$(printf '%s' "$g" | sed -E 's|^http://([^.]+)\..*|\1|')
      pf_port=${GATEWAY_PF_PORT:-18080}
      echo "  in-cluster address detected; port-forwarding svc/$svc -> 127.0.0.1:$pf_port" >&2
      kubectl -n "$ns" port-forward "svc/$svc" "${pf_port}:80" >/dev/null 2>&1 &
      echo $! > "$(_gateway_pf_pidfile)"
      for _ in $(seq 1 30); do
        if curl -s -o /dev/null -m 2 "http://127.0.0.1:${pf_port}/" 2>/dev/null || \
           nc -z 127.0.0.1 "$pf_port" 2>/dev/null; then break; fi
        sleep 1
      done
      printf 'http://127.0.0.1:%s' "$pf_port"
      ;;
    *)
      printf '%s' "$g"
      ;;
  esac
}

_gateway_pf_pidfile() { printf '%s/gateway-pf.%s.pid' "${TMPDIR:-/tmp}" "$$"; }

gateway_cleanup() {
  local f; f=$(_gateway_pf_pidfile)
  [ -f "$f" ] && kill "$(cat "$f")" 2>/dev/null
  rm -f "$f"
  return 0
}
