#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# Stage 2 AWS infra for the async front door: SQS queue (+ DLQ redrive), the DynamoDB
# idempotency/job-state table, one IAM role per service account (worker, submit API, relay) plus
# the KEDA-operator role, all wired via EKS Pod Identity, and the gateway-endpoint ConfigMap the
# worker resolves at deploy time.
# Idempotent. Prints outputs used to fill the manifests.
# Table schema matches async-worker/worker.py: PK job_id (S), TTL attribute ttl.
set -euo pipefail
CLUSTER_NAME="inference-aware-eks"
REGION="us-west-2"
NS="inference"
QUEUE="inference"
DLQ="inference-dlq"
TABLE="inference-jobs"
ROLE="inference-async-worker"
# The committed IAM documents under async-worker/iam/ are the SINGLE SOURCE OF TRUTH for the
# worker's trust and access policy (C.2). This script applies THOSE files (substituting the
# resolved ARNs into policy.json), so an auditor reading the repo sees exactly what is deployed,
# instead of a second inline copy that can drift.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IAM_DIR="${SCRIPT_DIR}/../../async-worker/iam"
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
VIS_TIMEOUT=900     # > max job duration; worker CLAIM_TTL_SECONDS=600

echo ">> [1/7] DLQ + main SQS queue with redrive"
# Encryption at rest is SET, not inherited: SQS-managed SSE is the default for new queues, but a
# default is a reading of the platform, and the PCSR threat model (M-011) asserts the property.
DLQ_URL="$(aws sqs create-queue --queue-name "${DLQ}" --region "${REGION}" \
  --attributes '{"SqsManagedSseEnabled":"true"}' --query QueueUrl --output text)"
aws sqs set-queue-attributes --queue-url "${DLQ_URL}" --region "${REGION}" \
  --attributes '{"SqsManagedSseEnabled":"true"}'
DLQ_ARN="$(aws sqs get-queue-attributes --queue-url "${DLQ_URL}" --attribute-names QueueArn --region "${REGION}" --query 'Attributes.QueueArn' --output text)"
REDRIVE="{\"deadLetterTargetArn\":\"${DLQ_ARN}\",\"maxReceiveCount\":\"5\"}"
QUEUE_URL="$(aws sqs create-queue --queue-name "${QUEUE}" --region "${REGION}" \
  --attributes "{\"VisibilityTimeout\":\"${VIS_TIMEOUT}\",\"SqsManagedSseEnabled\":\"true\",\"RedrivePolicy\":\"$(echo "${REDRIVE}" | sed 's/"/\\"/g')\"}" \
  --query QueueUrl --output text 2>/dev/null || aws sqs get-queue-url --queue-name "${QUEUE}" --region "${REGION}" --query QueueUrl --output text)"
# Ensure attributes on re-run.
aws sqs set-queue-attributes --queue-url "${QUEUE_URL}" --region "${REGION}" \
  --attributes "{\"VisibilityTimeout\":\"${VIS_TIMEOUT}\",\"SqsManagedSseEnabled\":\"true\",\"RedrivePolicy\":\"$(echo "${REDRIVE}" | sed 's/"/\\"/g')\"}"
QUEUE_ARN="$(aws sqs get-queue-attributes --queue-url "${QUEUE_URL}" --attribute-names QueueArn --region "${REGION}" --query 'Attributes.QueueArn' --output text)"

echo ">> [2/7] DynamoDB table ${TABLE} (PK job_id S, PAY_PER_REQUEST, TTL=ttl)"
if ! aws dynamodb describe-table --table-name "${TABLE}" --region "${REGION}" >/dev/null 2>&1; then
  aws dynamodb create-table --table-name "${TABLE}" --region "${REGION}" \
    --attribute-definitions AttributeName=job_id,AttributeType=S \
    --key-schema AttributeName=job_id,KeyType=HASH \
    --billing-mode PAY_PER_REQUEST >/dev/null
  aws dynamodb wait table-exists --table-name "${TABLE}" --region "${REGION}"
  aws dynamodb update-time-to-live --table-name "${TABLE}" --region "${REGION}" \
    --time-to-live-specification "Enabled=true,AttributeName=ttl" >/dev/null || true
fi
TABLE_ARN="arn:aws:dynamodb:${REGION}:${ACCOUNT_ID}:table/${TABLE}"

echo ">> [3/7] IAM roles, ONE PER SERVICE ACCOUNT (Pod Identity trust)"
# One role per component, each with only the calls that component makes. Until 2026-10-31 a single
# role served the worker, the submit API and the relay, so a compromise of any one of them carried
# the union of all three. The calls, read from the code:
#   worker + sqs-exporter (SA async-worker): ReceiveMessage, DeleteMessage, GetQueueAttributes,
#                                            DynamoDB GetItem and UpdateItem (the claim and the
#                                            terminal write). It never sends.
#   submit API (SA submit-api):              SendMessage, DynamoDB GetItem (the redemption route).
#                                            It writes no DynamoDB item: the worker's claim does.
#   relay (SA inference-relay):              SendMessage only, for the promotion re-enqueue.
# Trust policy: applied verbatim from the committed source of truth (Pod Identity), shared by all
# three because Pod Identity scopes the role to the association, not to the trust document.
cp "${IAM_DIR}/trust-policy.json" /tmp/aw-trust.json
apply_role(){  # apply_role <role-name> <policy-file> <inline-policy-name>
  local role="$1" policy="$2" pname="$3"
  aws iam get-role --role-name "${role}" >/dev/null 2>&1 || \
    aws iam create-role --role-name "${role}" --assume-role-policy-document file:///tmp/aw-trust.json >/dev/null
  # The committed policy with the resolved ARNs substituted in. ARNs contain no '|' so the sed
  # delimiter is safe; the ${...} placeholders are literal in the file.
  sed -e "s|[$]{QUEUE_ARN}|${QUEUE_ARN}|g" \
      -e "s|[$]{DLQ_ARN}|${DLQ_ARN}|g" \
      -e "s|[$]{TABLE_ARN}|${TABLE_ARN}|g" \
      "${IAM_DIR}/${policy}" > "/tmp/${role}-policy.json"
  aws iam put-role-policy --role-name "${role}" --policy-name "${pname}" \
    --policy-document "file:///tmp/${role}-policy.json"
}
SUBMIT_ROLE="inference-submit-api"
RELAY_ROLE="inference-relay"
apply_role "${ROLE}"        policy.json            async-worker-access
apply_role "${SUBMIT_ROLE}" submit-api-policy.json submit-api-access
apply_role "${RELAY_ROLE}"  relay-policy.json      relay-access

echo ">> [4/7] Pod Identity associations, each service account to its own role"
# Create the association if it is missing, and REPOINT it if it exists with another role. The repoint
# matters on any cluster built before the split: there the submit-api association still names the
# worker role, and a create-if-missing check alone would leave that broader grant in place.
while IFS=' ' read -r SA SA_ROLE; do
  WANT="arn:aws:iam::${ACCOUNT_ID}:role/${SA_ROLE}"
  ASSOC_ID="$(aws eks list-pod-identity-associations --cluster-name "${CLUSTER_NAME}" --namespace "${NS}" \
    --region "${REGION}" --query "associations[?serviceAccount=='${SA}'].associationId | [0]" --output text)"
  if [ -z "${ASSOC_ID}" ] || [ "${ASSOC_ID}" = "None" ]; then
    aws eks create-pod-identity-association --cluster-name "${CLUSTER_NAME}" \
      --namespace "${NS}" --service-account "${SA}" --role-arn "${WANT}" --region "${REGION}" >/dev/null
    echo "   created ${SA} -> ${SA_ROLE}"
  else
    HAVE="$(aws eks describe-pod-identity-association --cluster-name "${CLUSTER_NAME}" \
      --association-id "${ASSOC_ID}" --region "${REGION}" --query 'association.roleArn' --output text)"
    if [ "${HAVE}" != "${WANT}" ]; then
      aws eks update-pod-identity-association --cluster-name "${CLUSTER_NAME}" \
        --association-id "${ASSOC_ID}" --role-arn "${WANT}" --region "${REGION}" >/dev/null
      echo "   repointed ${SA}: ${HAVE##*/} -> ${SA_ROLE}"
    else
      echo "   ok ${SA} -> ${SA_ROLE}"
    fi
  fi
done <<EOF
async-worker ${ROLE}
submit-api ${SUBMIT_ROLE}
inference-relay ${RELAY_ROLE}
EOF

echo ">> [5/7] resolve the inference gateway Service, publish it as the gateway-endpoint ConfigMap"
# The async worker reads GATEWAY_ENDPOINT from this ConfigMap. Envoy Gateway names the Service per
# Gateway as envoy-<namespace>-<gateway>-<hash>, and the <hash> is cluster-specific, so it is
# discovered here rather than committed. Stage 1 creates the Gateway; if it is not up, fail loud
# instead of shipping a worker that cannot resolve the endpoint.
GW_SVC="$(kubectl get svc -n envoy-gateway-system \
  -l gateway.envoyproxy.io/owning-gateway-name=inference-gateway \
  -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
if [ -z "${GW_SVC}" ]; then
  echo "ERROR: no Envoy Service for gateway 'inference-gateway' in namespace envoy-gateway-system." >&2
  echo "       Stage 1 creates that Gateway. Bring up Stage 1 first, then re-run Stage 2." >&2
  exit 1
fi
GW_BASE="http://${GW_SVC}.envoy-gateway-system.svc.cluster.local"
GW_URL="${GW_BASE}/v1"
# TWO KEYS FROM ONE DISCOVERY, because the two consumers need different shapes and neither should
# derive the other by string surgery. The worker posts to a fixed path, so it wants the full `/v1`
# URL. The relay FORWARDS the caller's own path, so it wants the base and would otherwise produce
# `/v1/v1/completions`. Publishing both here keeps the cluster-specific hash discovered in exactly
# one place; having the relay strip a suffix would be a second place the URL's shape is known.
kubectl create configmap gateway-endpoint -n "${NS}" \
  --from-literal=url="${GW_URL}" --from-literal=base="${GW_BASE}" \
  --dry-run=client -o yaml | kubectl apply -f -
echo "   gateway-endpoint url=${GW_URL} base=${GW_BASE}"

# THE ASYNC ENDPOINTS, published for the same reason the gateway's are: the queue URL embeds the
# ACCOUNT ID, which is cluster-specific. The worker's manifest carried it as a committed literal
# and the literal was the SANITIZATION PLACEHOLDER 111122223333, so the worker crash-looped on
# InvalidAddress the first time anything needed it to run. A discovered value cannot be
# sanitized into a broken one.
kubectl create configmap async-endpoints -n "${NS}" \
  --from-literal=sqs_queue_url="${QUEUE_URL}" \
  --dry-run=client -o yaml | kubectl apply -f -
echo "   async-endpoints sqs_queue_url=${QUEUE_URL}"

echo ">> [6/7] KEDA operator IAM role and Pod Identity association (keda-async-auth)"
# The aws-sqs-queue scaler reads queue depth with the keda-operator pod's identity. Least-privilege:
# sqs:GetQueueAttributes on this queue only, a role separate from inference-async-worker. keda-operator
# is the SA the KEDA chart creates in namespace keda (install-keda.sh). If that chart's serviceAccount
# name is ever overridden, confirm with 'kubectl get sa -n keda' and update KEDA_SA.
KEDA_ROLE="inference-keda-sqs-read"
KEDA_SA="keda-operator"
cp "${IAM_DIR}/keda-trust-policy.json" /tmp/keda-trust.json
aws iam get-role --role-name "${KEDA_ROLE}" >/dev/null 2>&1 || \
  aws iam create-role --role-name "${KEDA_ROLE}" --assume-role-policy-document file:///tmp/keda-trust.json >/dev/null
sed -e "s|[$]{QUEUE_ARN}|${QUEUE_ARN}|g" "${IAM_DIR}/keda-policy.json" > /tmp/keda-policy.json
aws iam put-role-policy --role-name "${KEDA_ROLE}" --policy-name keda-sqs-read --policy-document file:///tmp/keda-policy.json
if ! aws eks list-pod-identity-associations --cluster-name "${CLUSTER_NAME}" --namespace keda --region "${REGION}" \
      --query "associations[?serviceAccount=='${KEDA_SA}']" --output text | grep -q .; then
  aws eks create-pod-identity-association --cluster-name "${CLUSTER_NAME}" \
    --namespace keda --service-account "${KEDA_SA}" \
    --role-arn "arn:aws:iam::${ACCOUNT_ID}:role/${KEDA_ROLE}" --region "${REGION}" >/dev/null
fi
# EKS Pod Identity injects credentials when a POD IS CREATED. The KEDA operator started during
# `make addons`, before this association existed, so it would keep using the node instance role and
# every SQS trigger would fail with AccessDenied (found on the 2026-10-31 fresh build). Restart it.
if kubectl -n keda get deploy/keda-operator >/dev/null 2>&1; then
  kubectl -n keda rollout restart deploy/keda-operator >/dev/null
  kubectl -n keda rollout status deploy/keda-operator --timeout=180s
fi

echo ">> [7/7] outputs"
echo "SQS_QUEUE_URL=${QUEUE_URL}"
echo "QUEUE_ARN=${QUEUE_ARN}"
echo "TABLE_ARN=${TABLE_ARN}"
echo "ROLE_ARN=arn:aws:iam::${ACCOUNT_ID}:role/${ROLE}"
echo "SUBMIT_ROLE_ARN=arn:aws:iam::${ACCOUNT_ID}:role/${SUBMIT_ROLE}"
echo "RELAY_ROLE_ARN=arn:aws:iam::${ACCOUNT_ID}:role/${RELAY_ROLE}"
