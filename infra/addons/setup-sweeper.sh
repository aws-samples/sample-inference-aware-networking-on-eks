#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# IAM + Pod Identity for the orphan sweeper (DESIGN.md section 3.4). Creates a
# least-privilege role inference-sweeper and associates it to the `sweeper` SA in the
# inference namespace. Idempotent. The sweeper is the ONLY component that scans the job
# table, so it is the only one granted dynamodb:Scan. It also needs sqs:SendMessage (to
# re-enqueue an orphan) and sqs:GetQueueAttributes (to read the visibility timeout so its
# grace period always exceeds it). Nothing more.
set -euo pipefail
CLUSTER_NAME="inference-aware-eks"
REGION="us-west-2"
NS="inference"
QUEUE="inference"
TABLE="inference-jobs"
ROLE="inference-sweeper"
SA="sweeper"
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"

QUEUE_URL="$(aws sqs get-queue-url --queue-name "${QUEUE}" --region "${REGION}" --query QueueUrl --output text)"
QUEUE_ARN="$(aws sqs get-queue-attributes --queue-url "${QUEUE_URL}" --attribute-names QueueArn --region "${REGION}" --query 'Attributes.QueueArn' --output text)"
TABLE_ARN="arn:aws:dynamodb:${REGION}:${ACCOUNT_ID}:table/${TABLE}"

echo ">> [1/3] sweeper IAM role ${ROLE} (Pod Identity trust)"
cat > /tmp/sweeper-trust.json <<JSON
{ "Version": "2012-10-17", "Statement": [ {
  "Effect": "Allow",
  "Principal": { "Service": "pods.eks.amazonaws.com" },
  "Action": [ "sts:AssumeRole", "sts:TagSession" ] } ] }
JSON
aws iam get-role --role-name "${ROLE}" >/dev/null 2>&1 || \
  aws iam create-role --role-name "${ROLE}" --assume-role-policy-document file:///tmp/sweeper-trust.json >/dev/null

echo ">> [2/3] least-privilege inline policy (Scan+UpdateItem, SendMessage+GetQueueAttributes)"
cat > /tmp/sweeper-policy.json <<JSON
{ "Version": "2012-10-17", "Statement": [
  { "Effect": "Allow",
    "Action": ["sqs:SendMessage","sqs:GetQueueAttributes"],
    "Resource": "${QUEUE_ARN}" },
  { "Effect": "Allow",
    "Action": ["dynamodb:Scan","dynamodb:Query","dynamodb:UpdateItem"],
    "Resource": "${TABLE_ARN}" } ] }
JSON
aws iam put-role-policy --role-name "${ROLE}" --policy-name sweeper-access --policy-document file:///tmp/sweeper-policy.json

echo ">> [3/3] Pod Identity association (${SA} SA -> ${ROLE})"
if ! aws eks list-pod-identity-associations --cluster-name "${CLUSTER_NAME}" --namespace "${NS}" --region "${REGION}" \
      --query "associations[?serviceAccount=='${SA}']" --output text | grep -q .; then
  aws eks create-pod-identity-association --cluster-name "${CLUSTER_NAME}" \
    --namespace "${NS}" --service-account "${SA}" \
    --role-arn "arn:aws:iam::${ACCOUNT_ID}:role/${ROLE}" --region "${REGION}" >/dev/null
fi
echo "ROLE_ARN=arn:aws:iam::${ACCOUNT_ID}:role/${ROLE}"
echo "done"
