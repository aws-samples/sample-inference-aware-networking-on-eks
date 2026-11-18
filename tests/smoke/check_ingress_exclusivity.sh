#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# INV12, CLUSTER HALF. Companion to the offline check, and it exists because the offline half was
# caught trusting a proxy.
#
# WHY BOTH HALVES ARE NEEDED. The offline INV12 reads committed files. On 2026-08-04 it reported
# POST-RELAY green while the LIVE cluster still had the gateway data plane exposed on a public load
# balancer, because Envoy Gateway DEFAULTS `envoyService` to LoadBalancer and the manifest had only
# stopped ANNOTATING it. Absence of an exposure marker in a file is not evidence of no exposure in a
# cluster. The offline half now pins the type explicitly; this half checks what is actually running,
# which is the only thing that can catch a default, an operator's mutation, or a manual `kubectl
# expose` that no file records.
#
# WHAT IT ASSERTS: across ALL namespaces, exactly one Service is externally exposed (LoadBalancer or
# NodePort), and it is the relay's, identified by the label the manifest sets. Anything else is the
# bypass INV12 exists to close: a client reaching the interactive default rule unclassified, evading
# classification, the reservation guard and the upgrade-rate cap, with nothing failing.
#
# FAILS CLOSED. If the cluster cannot be reached, it errors rather than passing, because "I could
# not check" must never render as "it is fine".
#
# Cluster-gated: needs credentials, so it runs on the activation list rather than in test-offline.
set -uo pipefail
RELAY_LABEL="inference-aware-eks/ingress-role=relay"

command -v kubectl >/dev/null 2>&1 || { echo "FAIL kubectl is not on PATH; cannot verify, so this does not pass"; exit 1; }
kubectl version >/dev/null 2>&1 || { echo "FAIL cannot reach the cluster; cannot verify, so this does not pass"; exit 1; }

echo "=== INV12 cluster half: exactly one external door, and it must be the relay's ==="

ALL=$(kubectl get svc -A -o json 2>/dev/null) || { echo "FAIL could not list Services"; exit 1; }
[ -n "$ALL" ] || { echo "FAIL empty Service listing; treating as unverifiable rather than clean"; exit 1; }

exposed=$(printf '%s' "$ALL" | python3 -c '
import json,sys
d=json.load(sys.stdin)
rows=[]
for i in d.get("items",[]):
    t=(i.get("spec") or {}).get("type")
    if t in ("LoadBalancer","NodePort"):
        m=i.get("metadata") or {}
        lbl=(m.get("labels") or {}).get("inference-aware-eks/ingress-role","")
        ing=((i.get("status") or {}).get("loadBalancer") or {}).get("ingress") or [{}]
        host=ing[0].get("hostname") or ing[0].get("ip") or "-"
        rows.append("\t".join([m.get("namespace",""), m.get("name",""), t, lbl or "-", host]))
print("\n".join(rows))
')

# TWO LEGITIMATE TOPOLOGIES, both exercised on the 2026-10-31 fresh build. The walkthrough deploys
# no relay, and then the correct answer is ZERO external doors: the gateway and submit API stay
# ClusterIP. Once the relay Deployment exists, it must be the ONE door. Which topology applies is
# read from the cluster (does the relay Deployment exist?), never assumed.
relay_deployed=0
kubectl -n inference get deploy inference-relay >/dev/null 2>&1 && relay_deployed=1
if [ -z "$exposed" ]; then
  if [ "$relay_deployed" -eq 0 ]; then
    echo "PASS: no relay deployed and no externally exposed Service (walkthrough topology)"
    exit 0
  fi
  echo "FAIL no externally exposed Service at all, but the relay Deployment exists. The relay is"
  echo "     supposed to BE the door, so zero doors means its exposure is missing."
  exit 1
fi

relay=0; other=0
printf '%s\n' "$exposed" | while IFS= read -r line; do
  printf "  %s\n" "$line"
done
relay=$(printf '%s\n' "$exposed" | awk -F'\t' '$4=="relay"' | wc -l | tr -d ' ')
other=$(printf '%s\n' "$exposed" | awk -F'\t' '$4!="relay"' | wc -l | tr -d ' ')

echo
if [ "$relay" -eq 1 ] && [ "$other" -eq 0 ]; then
  echo "PASS: exactly one external door and it is the relay's (label $RELAY_LABEL)"
  exit 0
fi
echo "FAIL INV12 cluster half: relay-labelled exposures=$relay, other exposures=$other."
if [ "$other" -gt 0 ]; then
  echo "     The ones above without 'relay' in the label column are UNCLASSIFIED ENTRY POINTS. A"
  echo "     client reaching the gateway data plane directly arrives at the interactive default"
  echo "     rule having bypassed classification, the reservation guard and the upgrade-rate cap,"
  echo "     and nothing fails or alerts. That is the bypass this invariant exists to close."
fi
[ "$relay" -ne 1 ] && echo "     Expected exactly ONE relay-labelled exposure, found $relay."
exit 1
