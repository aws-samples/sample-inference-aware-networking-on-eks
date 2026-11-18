#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# Install the AWS Load Balancer Controller (pinned in versions.yaml). Idempotent.
#
# WHY THIS ADDON EXISTS, from the primary source rather than from preference. The Amazon EKS user
# guide, "Route TCP and UDP traffic with Network Load Balancers", read 2026-08-07:
#
#   "When you create a Kubernetes Service of type LoadBalancer, the AWS cloud provider load balancer
#    controller creates AWS Classic Load Balancers by default [...] This controller is only
#    receiving critical bug fixes in the future."
#   "We recommend that you use version 2.7.2 or later of the AWS Load Balancer Controller instead
#    [...] The AWS Load Balancer Controller creates AWS Network Load Balancers, but doesn't create
#    AWS Classic Load Balancers."
#
# The relay was provisioning a Classic Load Balancer, and it was doing so by DEFAULT rather than by
# decision: a bare `type: LoadBalancer` with no type annotation selects the in-tree provider. It
# worked, which is exactly why nobody looked. A Classic Load Balancer serves traffic perfectly well
# and is simply not what a production deployment should be built on.
#
# AUTH IS EKS POD IDENTITY, NOT IRSA, per DESIGN.md convention 4, which this project applies to
# Karpenter, the async worker, the submit API and the sweeper. The controller is no exception, and
# the association is created here rather than assumed.
set -euo pipefail

CHART_VER="3.6.2"          # versions.yaml choices.aws_load_balancer_controller_chart
CLUSTER="${CLUSTER:-inference-aware-eks}"
REGION="${REGION:-us-west-2}"
NS=kube-system
SA=aws-load-balancer-controller
POLICY_NAME=AWSLoadBalancerControllerIAMPolicy
ROLE_NAME="${CLUSTER}-alb-controller"

ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
POLICY_ARN="arn:aws:iam::${ACCOUNT}:policy/${POLICY_NAME}"

echo ">> AWS Load Balancer Controller ${CHART_VER} into ${CLUSTER} (${REGION})"

# --- IAM policy -------------------------------------------------------------------------------
# The policy document is AWS-published and is FETCHED at its pinned tag rather than vendored, so it
# cannot drift silently from the chart version it must match. Fetch failure is fatal: a controller
# with a stale or partial policy fails at provisioning time with a permissions error that reads like
# a networking fault.
#
# AND IT IS CHECKSUMMED, like the Karpenter template (release candidate 7): an IAM policy document is
# the one download here that grants permissions, so a substituted file must stop the install rather
# than become a role. The hash is of the v3.6.2 file, fetched twice on 2026-11-07 (16 statements).
# Bump it together with CHART_VER.
LBC_POLICY_SHA256="16f232c9d9f79366fe949c4550ad517a202380058a9e48d45a4e215044a20a6a"
if ! aws iam get-policy --policy-arn "$POLICY_ARN" >/dev/null 2>&1; then
  TMP=$(mktemp)
  curl -fsSL -o "$TMP" \
    "https://raw.githubusercontent.com/kubernetes-sigs/aws-load-balancer-controller/v3.6.2/docs/install/iam_policy.json"
  GOT_SHA="$(shasum -a 256 "$TMP" | awk '{print $1}')"
  if [ "$GOT_SHA" != "$LBC_POLICY_SHA256" ]; then
    echo "REFUSING: iam_policy.json SHA-256 is $GOT_SHA, expected $LBC_POLICY_SHA256" >&2
    rm -f "$TMP"
    exit 1
  fi
  aws iam create-policy --policy-name "$POLICY_NAME" --policy-document "file://$TMP" >/dev/null
  rm -f "$TMP"
  echo "   created IAM policy ${POLICY_NAME}"
else
  echo "   IAM policy ${POLICY_NAME} already exists"
fi

# --- role, trusted by the Pod Identity service principal --------------------------------------
if ! aws iam get-role --role-name "$ROLE_NAME" >/dev/null 2>&1; then
  aws iam create-role --role-name "$ROLE_NAME" --assume-role-policy-document '{
    "Version":"2012-10-17",
    "Statement":[{"Effect":"Allow",
                  "Principal":{"Service":"pods.eks.amazonaws.com"},
                  "Action":["sts:AssumeRole","sts:TagSession"]}]}' >/dev/null
  echo "   created role ${ROLE_NAME}"
fi
aws iam attach-role-policy --role-name "$ROLE_NAME" --policy-arn "$POLICY_ARN"

kubectl -n "$NS" get sa "$SA" >/dev/null 2>&1 || kubectl -n "$NS" create sa "$SA"

# Pod Identity association. Idempotent: the API rejects a duplicate, which is not an error here.
aws eks create-pod-identity-association \
  --cluster-name "$CLUSTER" --namespace "$NS" --service-account "$SA" \
  --role-arn "arn:aws:iam::${ACCOUNT}:role/${ROLE_NAME}" \
  --region "$REGION" >/dev/null 2>&1 \
  && echo "   pod identity association created" \
  || echo "   pod identity association already present"

# --- chart ------------------------------------------------------------------------------------
helm repo add eks https://aws.github.io/eks-charts >/dev/null 2>&1 || true
helm repo update eks >/dev/null
helm upgrade --install aws-load-balancer-controller eks/aws-load-balancer-controller \
  --namespace "$NS" --version "${CHART_VER}" \
  --set clusterName="${CLUSTER}" \
  --set serviceAccount.create=false \
  --set serviceAccount.name="${SA}" \
  --set region="${REGION}" \
  --wait
kubectl -n "$NS" rollout status deploy/aws-load-balancer-controller --timeout=300s

echo ">> AWS Load Balancer Controller ${CHART_VER} installed."
echo
echo "   MIGRATION NOTE, from the same EKS guide and it is not optional reading:"
echo "     'Do not edit the annotations after creating your service. If you need to modify it,"
echo "      delete the service object and create it again with the desired value.'"
echo
echo "   So a Service that already exists as a Classic Load Balancer is NOT migrated by re-applying"
echo "   it. It must be DELETED and RECREATED, which changes the external hostname and briefly"
echo "   removes the external door. See docs/runbooks/relay-nlb-cutover.md."
