#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# Install Karpenter (pinned version) on an existing eksctl cluster.
# Controller auth uses EKS Pod Identity (simpler than IRSA, no OIDC thumbprints).
# Idempotent: safe to re-run. Requires AWS_PROFILE set and kubectl context on
# the target cluster. See DESIGN.md prime directive 1 (versions from versions.yaml).
set -euo pipefail

VER="1.15.1"                       # versions.yaml karpenter_install. K8s 1.37 needs 1.15.x.
CLUSTER_NAME="inference-aware-eks"
REGION="us-west-2"
NAMESPACE="kube-system"
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
PARTITION="aws"

echo ">> Karpenter ${VER} on ${CLUSTER_NAME} (${REGION}), account ${ACCOUNT_ID}"

# 1. IAM + interruption queue via the pinned getting-started CloudFormation.
echo ">> [1/8] deploy Karpenter IAM/interruption CloudFormation"
# UPSTREAM TEMPLATE AS A PINNED EXTERNAL DEPENDENCY. It became load-bearing the moment the
# superset assertion started comparing against it, and it is AWS's published artifact rather than
# a local file, so it gets what every other external artifact here gets: a recorded source, a fetch
# date, and a CHECKSUM that is verified rather than trusted. Without this the merge guard compares
# the merged template against whatever upstream happens to serve today, which would make the
# strongest check in this path silently dependent on an unpinned input.
#   source:     the URL below, tag v${VER}
#   fetched:    2026-10-31 (v1.15.1: one line added since v1.14.0, ec2:RebootInstances in AllowScopedDeletion)
#   sha256:     8ee49d6de4f2397797aea5c961706c90491a291b6a961268000cb2daadbd958f
# A mismatch is NOT auto-accepted: it means upstream changed the template at a pinned tag, which is
# a fact to investigate, not a checksum to bump.
KARPENTER_CFN_URL="https://raw.githubusercontent.com/aws/karpenter-provider-aws/v${VER}/website/content/en/preview/getting-started/getting-started-with-karpenter/cloudformation.yaml"
KARPENTER_CFN_SHA256="8ee49d6de4f2397797aea5c961706c90491a291b6a961268000cb2daadbd958f"
curl -fsSL "${KARPENTER_CFN_URL}" -o /tmp/karpenter-cfn.yaml
GOT_SHA="$(shasum -a 256 /tmp/karpenter-cfn.yaml | awk '{print $1}')"
if [ "${GOT_SHA}" != "${KARPENTER_CFN_SHA256}" ]; then
  echo "ERROR: upstream CloudFormation template checksum mismatch at pinned tag v${VER}."
  echo "       expected ${KARPENTER_CFN_SHA256}"
  echo "       got      ${GOT_SHA}"
  echo "       Upstream changed a file at a pinned tag. Investigate before bumping the checksum."
  exit 1
fi
echo "   upstream template checksum verified (${GOT_SHA})"
# OVERLAY (added 2026-08-03): append the controller role and the node instance profile so THIS
# STACK owns them. Both were previously created outside it, which is why the teardown needed four
# attempts: CloudFormation could not delete policies attached to a role it did not own, nor a role
# held by instance profiles Karpenter minted per EC2NodeClass. Acceptance criterion for the fix is
# the next teardown: one stack delete, zero manual steps. AWS's template is never forked, only
# appended to, so it stays upgradeable. See infra/addons/karpenter-cfn-overlay.yaml.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
grep -q "^Resources:" /tmp/karpenter-cfn.yaml || { echo "ERROR: upstream template has no Resources section; overlay cannot be applied safely"; exit 1; }
grep -q "KarpenterControllerRole" /tmp/karpenter-cfn.yaml && { echo "ERROR: upstream template now defines KarpenterControllerRole itself; re-verify the overlay before continuing"; exit 1; }
# AWS's template ends WITHOUT a trailing newline, so a bare `cat >>` concatenates the first
# overlay byte onto the last template byte and produces garbage like "Arn# Overlay appended".
# That is exactly what happened on the first rebuild attempt: CloudFormation reported
# "Requested attribute Arn# Overlay ... does not exist in schema for AWS::SQS::Queue" and the
# whole stack rolled back. Force the separator rather than assuming the upstream file has one.
PRISTINE=/tmp/karpenter-cfn-pristine.yaml
cp /tmp/karpenter-cfn.yaml "${PRISTINE}"   # keep the untouched upstream for the superset assertion
printf '\n' >> /tmp/karpenter-cfn.yaml
cat "${SCRIPT_DIR}/karpenter-cfn-overlay.yaml" >> /tmp/karpenter-cfn.yaml
# STRUCTURAL CHECK on the merged artifact, and it is the ONLY layer that works. The obvious guard,
# `aws cloudformation validate-template`, was tested by injection on 2026-08-03 and ACCEPTED BOTH
# failure modes this merge can produce: the fused-newline artifact that actually rolled the stack
# back, and a duplicate logical ID. It checks syntax and returns parameters, but it does not
# resolve GetAtt attribute names (that happens at changeset time) and a duplicate key is collapsed
# by YAML last-key-wins before CloudFormation ever sees it. So the local checker is not a second
# opinion, it is the check. Offline, no credentials.
PYBIN="./.venv/bin/python"; [ -x "$PYBIN" ] || PYBIN=python3
cp /tmp/karpenter-cfn.yaml /tmp/karpenter-cfn-merged.yaml
"$PYBIN" "${SCRIPT_DIR}/../../tests/check_cfn_merge.py" /tmp/karpenter-cfn-merged.yaml \
  "${PRISTINE}" "${SCRIPT_DIR}/karpenter-cfn-overlay.yaml" \
  || { echo "ERROR: merged CloudFormation template failed the structural check"; exit 1; }
aws cloudformation deploy \
  --stack-name "Karpenter-${CLUSTER_NAME}" \
  --template-file /tmp/karpenter-cfn.yaml \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides "ClusterName=${CLUSTER_NAME}" \
  --region "${REGION}"

NODE_ROLE="KarpenterNodeRole-${CLUSTER_NAME}"
CONTROLLER_ROLE="KarpenterControllerRole-${CLUSTER_NAME}"
QUEUE_NAME="${CLUSTER_NAME}"

# 2. Controller role: now CREATED BY THE STACK via the overlay (2026-08-03). This step only
# verifies it exists, instead of creating it outside the stack as it used to. That out-of-stack
# creation is precisely what made the stack undeletable (BUILD-NOTES 2026-08-03, correction 7).
echo ">> [2/8] verify controller role ${CONTROLLER_ROLE} is stack-owned"
aws iam get-role --role-name "${CONTROLLER_ROLE}" >/dev/null 2>&1 || {
  echo "ERROR: ${CONTROLLER_ROLE} absent. The overlay should have created it in the stack."; exit 1; }
ATTACHED="$(aws iam list-attached-role-policies --role-name "${CONTROLLER_ROLE}" --query 'length(AttachedPolicies)' --output text)"
[ "${ATTACHED}" -eq 6 ] || { echo "ERROR: ${CONTROLLER_ROLE} has ${ATTACHED} attached policies, expected 6"; exit 1; }
echo "   ok: role present with 6 managed policies, all owned by the stack"

# 3. Pod Identity agent addon.
echo ">> [3/8] eks-pod-identity-agent addon"
aws eks create-addon --cluster-name "${CLUSTER_NAME}" --addon-name eks-pod-identity-agent --region "${REGION}" 2>/dev/null || echo "   (addon already present)"
aws eks wait addon-active --cluster-name "${CLUSTER_NAME}" --addon-name eks-pod-identity-agent --region "${REGION}" 2>/dev/null || true

# 4. Pod Identity association: kube-system/karpenter SA -> controller role.
echo ">> [4/8] pod identity association for SA karpenter"
if ! aws eks list-pod-identity-associations --cluster-name "${CLUSTER_NAME}" --namespace "${NAMESPACE}" --region "${REGION}" \
      --query 'associations[?serviceAccount==`karpenter`]' --output text | grep -q .; then
  aws eks create-pod-identity-association --cluster-name "${CLUSTER_NAME}" \
    --namespace "${NAMESPACE}" --service-account karpenter \
    --role-arn "arn:${PARTITION}:iam::${ACCOUNT_ID}:role/${CONTROLLER_ROLE}" --region "${REGION}"
else
  echo "   (association already exists)"
fi

# 5. EKS access entry so Karpenter-launched nodes (node role) can join.
echo ">> [5/8] EKS access entry for ${NODE_ROLE}"
NODE_ROLE_ARN="arn:${PARTITION}:iam::${ACCOUNT_ID}:role/${NODE_ROLE}"
aws eks create-access-entry --cluster-name "${CLUSTER_NAME}" --principal-arn "${NODE_ROLE_ARN}" \
  --type EC2_LINUX --region "${REGION}" 2>/dev/null || echo "   (access entry already exists)"

# 6. Tag private subnets and the cluster security group for discovery.
echo ">> [6/8] tag subnets + SG with karpenter.sh/discovery=${CLUSTER_NAME}"
VPC_ID="$(aws eks describe-cluster --name "${CLUSTER_NAME}" --region "${REGION}" --query 'cluster.resourcesVpcConfig.vpcId' --output text)"
CLUSTER_SG="$(aws eks describe-cluster --name "${CLUSTER_NAME}" --region "${REGION}" --query 'cluster.resourcesVpcConfig.clusterSecurityGroupId' --output text)"
PRIV_SUBNETS="$(aws ec2 describe-subnets --region "${REGION}" \
  --filters "Name=vpc-id,Values=${VPC_ID}" "Name=tag:Name,Values=*Private*" \
  --query 'Subnets[].SubnetId' --output text)"
if [ -z "${PRIV_SUBNETS}" ]; then
  echo "   no *Private* subnets found; tagging all cluster subnets instead"
  PRIV_SUBNETS="$(aws eks describe-cluster --name "${CLUSTER_NAME}" --region "${REGION}" --query 'cluster.resourcesVpcConfig.subnetIds' --output text)"
fi
aws ec2 create-tags --region "${REGION}" --resources ${PRIV_SUBNETS} ${CLUSTER_SG} \
  --tags "Key=karpenter.sh/discovery,Value=${CLUSTER_NAME}"
echo "   tagged subnets: ${PRIV_SUBNETS}"
echo "   tagged SG: ${CLUSTER_SG}"

# 7. Install/upgrade Karpenter via Helm (OCI). Public ECR needs an auth token for
#    OCI pulls (anonymous tokens expire); log in first.
echo ">> [7/8] helm registry login public.ecr.aws + helm upgrade --install karpenter ${VER}"
aws ecr-public get-login-password --region us-east-1 | helm registry login --username AWS --password-stdin public.ecr.aws
helm upgrade --install karpenter "oci://public.ecr.aws/karpenter/karpenter" \
  --version "${VER}" --namespace "${NAMESPACE}" \
  --set "settings.clusterName=${CLUSTER_NAME}" \
  --set "settings.interruptionQueue=${QUEUE_NAME}" \
  --set controller.resources.requests.cpu=1 \
  --set controller.resources.requests.memory=1Gi \
  --wait

# 8. Verify.
echo ">> [8/8] rollout status"
kubectl -n "${NAMESPACE}" rollout status deploy/karpenter --timeout=180s
echo ">> EC2NodeClass uses spec.instanceProfile: KarpenterNodeInstanceProfile-${CLUSTER_NAME}"
echo ">> DONE. Stack owns the controller role AND the node instance profile, so teardown is one stack delete. Discovery tag=${CLUSTER_NAME}."
