#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# Apply the optional NetworkPolicies, but only where they are enforced.
#
# Amazon VPC CNI ships its network policy agent disabled. Until it is enabled, every NetworkPolicy
# applies cleanly and does nothing, so this script checks the agent's own flag first and refuses
# rather than leave inert policies that read as protection.
#   Enable:  aws eks update-addon --cluster-name <cluster-name> --addon-name vpc-cni \
#              --service-account-role-arn <the add-on's current role> \
#              --configuration-values '{"enableNetworkPolicy":"true"}' --region <region>
#   Then confirm the aws-node service account still carries eks.amazonaws.com/role-arn. On the
#   2026-10-31 run the update left it without the annotation, and the restarted CNI pod crash-looped
#   on ec2:DescribeNetworkInterfaces until the annotation was restored.
set -euo pipefail
# The agent container is aws-eks-nodeagent in VPC CNI v1.22 (read on the 1.37 cluster, 2026-10-31);
# older docs call it aws-network-policy-agent. Read whichever exists.
args=$(kubectl -n kube-system get daemonset aws-node \
  -o jsonpath='{.spec.template.spec.containers[?(@.name=="aws-eks-nodeagent")].args}{.spec.template.spec.containers[?(@.name=="aws-network-policy-agent")].args}')
case "$args" in
  *"--enable-network-policy=true"*) echo ">> VPC CNI network policy enforcement: on" ;;
  *) echo "REFUSING: the VPC CNI network policy agent is not enforcing (args: ${args:-none})." >&2
     echo "Enable it with the update-addon command at the top of this script, then re-run." >&2
     exit 1 ;;
esac
kubectl apply -f manifests/09-network-policy/inference-network-policies.yaml
kubectl apply -f infra/addons/gateway-dataplane-networkpolicy.yaml
# Restart the gateway data plane so it opens fresh ext_proc connections to the Endpoint Pickers.
# On the 2026-10-31 run, connections opened before enforcement kept failing ("connection
# termination", HTTP 500) on about one request in ten until the restart; afterwards 12 of 12 passed.
dp=$(kubectl -n envoy-gateway-system get deploy \
  -l gateway.envoyproxy.io/owning-gateway-name=inference-gateway -o name)
if [ -n "$dp" ]; then
  kubectl -n envoy-gateway-system rollout restart $dp
  kubectl -n envoy-gateway-system rollout status $dp --timeout=180s
fi
kubectl get networkpolicy -n inference
kubectl get networkpolicy -n envoy-gateway-system
