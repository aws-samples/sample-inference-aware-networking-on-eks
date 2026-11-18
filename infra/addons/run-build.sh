#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# Build the async-worker image IN-CLUSTER with BuildKit, from a git SHA, and push a SHA-tagged
# revision-stamped image to Amazon ECR. No local Docker daemon, no long-lived credentials.
#
# WHY THE CONTEXT IS A `git archive` TARBALL AND NOT AN IN-POD CLONE. The repository is PRIVATE, so
# a pod with no credentials cannot clone it, and injecting a GitHub token would add exactly the
# long-lived credential the Pod Identity posture exists to avoid. `git archive <SHA>` reads from the
# OBJECT DATABASE, so uncommitted changes are STRUCTURALLY EXCLUDED rather than merely asserted
# against: a dirty tree cannot enter the context at all. That is a stronger guarantee than the
# clone-and-checkout assertion it replaces, and it is what prevents another v9 (BUILD-NOTES
# 2026-08-02, an image built from a dirty tree that matched no commit).
set -euo pipefail
SD="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NS=inference; REGION="${AWS_REGION:-us-west-2}"
ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
REGISTRY="${ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com"
BUILDER="${REGISTRY}/inference-builder@sha256:cd04bc0c17a68703683d100109bbc1f8389fc5bb5e2946f0d2af062d1dc83238"
SHA="${GIT_SHA:-$(git rev-parse HEAD)}"
SHORT="${SHA:0:12}"
TAG="sha-${SHORT}"

# The SHA must exist on a remote, or the image would reference a commit nobody else can fetch.
git branch -r --contains "$SHA" >/dev/null 2>&1 && [ -n "$(git branch -r --contains "$SHA" 2>/dev/null)" ] \
  || { echo "ERROR: ${SHA} is not on any remote. Push before building, or the image records an unreachable commit."; exit 1; }

echo ">> building ${REGISTRY}/inference-async-worker:${TAG} from ${SHA}"
kubectl -n "$NS" delete job build-async-worker --ignore-not-found >/dev/null 2>&1
kubectl -n "$NS" delete configmap build-context --ignore-not-found >/dev/null 2>&1
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
git archive --format=tar "$SHA" -- async-worker | gzip -9 > "$TMP/ctx.tar.gz"
kubectl -n "$NS" create configmap build-context --from-file=ctx.tar.gz="$TMP/ctx.tar.gz"

sed -e "s|__BUILDER_IMAGE__|${BUILDER}|g" -e "s|__SHA__|${SHA}|g" -e "s|__TAG__|${TAG}|g" \
    -e "s|__REGISTRY__|${REGISTRY}|g" -e "s|__REGION__|${REGION}|g" \
    "${SD:-infra/addons}/buildkit-job.template.yaml" | kubectl -n "$NS" apply -f -

# Wait for EITHER terminal condition. Waiting only on `complete` blocks for the full timeout when
# the job FAILS, which cost ten minutes on the first attempt: the job had failed in seconds and the
# wait sat there regardless. Verify against the authoritative status, not a proxy for it.
echo ">> waiting for the build job to reach a terminal state"
for _ in $(seq 1 180); do
  C=$(kubectl -n "$NS" get job build-async-worker -o jsonpath='{.status.conditions[?(@.type=="Complete")].status}' 2>/dev/null || true)
  F=$(kubectl -n "$NS" get job build-async-worker -o jsonpath='{.status.conditions[?(@.type=="Failed")].status}' 2>/dev/null || true)
  [ "$C" = "True" ] && { echo ">> build COMPLETE"; break; }
  [ "$F" = "True" ] && { echo ">> build FAILED"; kubectl -n "$NS" logs job/build-async-worker --all-containers --tail=40; exit 1; }
  sleep 5
done
[ "$(kubectl -n "$NS" get job build-async-worker -o jsonpath='{.status.conditions[?(@.type=="Complete")].status}')" = "True" ] \
  || { echo "BUILD did not complete in time"; kubectl -n "$NS" logs job/build-async-worker --all-containers --tail=40; exit 1; }
echo ">> pushed ${REGISTRY}/inference-async-worker:${TAG}"
