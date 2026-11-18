#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# INV15, CLUSTER HALF. The committed pin must equal the image ACTUALLY RUNNING.
#
# WHY THIS EXISTS, and it is the sharpest case in the audit. INV15 was created because the manifests
# pinned `:v8` in three places while the cluster ran `:v10`, and reconstructing what had actually
# served took blob-hash archaeology a week later. The offline half asserts that the pins AGREE WITH
# EACH OTHER and are commit-traceable.
#
# Both of those properties were GREEN THROUGHOUT THE ORIGINAL INCIDENT. The three `:v8` pins agreed
# with each other perfectly. The defect lived entirely in the gap between the files and the cluster,
# which is the one place a file-based check cannot look. An invariant that cannot see the only
# location its founding defect ever occupied is not protecting against that defect.
#
# So this half reads the RUNNING pods and compares. It is the authority; the manifest is the claim.
#
# FAILS CLOSED. If the cluster cannot be reached it errors rather than passing, because
# "I could not check" must never render as "it is fine".
#
# Cluster-gated: needs credentials, so it is not in `make test-offline`.
set -uo pipefail
NS="${NS:-inference}"
REPO_RE='[0-9]{12}\.dkr\.ecr\.[a-z0-9-]+\.amazonaws\.com/inference-async-worker'

command -v kubectl >/dev/null 2>&1 || { echo "FAIL kubectl is not on PATH; cannot verify, so this does not pass"; exit 1; }
kubectl version >/dev/null 2>&1 || { echo "FAIL cannot reach the cluster; cannot verify, so this does not pass"; exit 1; }

echo "=== INV15 cluster half: the committed pin must equal what is RUNNING ==="

# The committed pin, derived the same way the offline half derives it, so the two cannot disagree
# about what the claim even is.
PIN=$(grep -rhoE "${REPO_RE}:[A-Za-z0-9._-]+" manifests async-worker/*.yaml 2>/dev/null | sed 's/.*://' | sort -u)
NPIN=$(printf '%s\n' "$PIN" | grep -c . || true)
if [ "${NPIN:-0}" -ne 1 ]; then
  echo "FAIL the committed manifests do not carry exactly one pin for this image (found: $(printf '%s ' $PIN))."
  echo "     The offline half owns that property; this half cannot proceed without a single claim to check."
  exit 1
fi
echo "  committed pin : $PIN"

# What is ACTUALLY running. Pods rather than Deployments: a Deployment records intent, a pod records
# what was pulled. Terminating pods are excluded on purpose, for the same reason the conventions now
# require selecting on phase: a pod of the previous generation is not evidence about this one.
RUNNING=$(kubectl -n "$NS" get pods --field-selector=status.phase=Running -o json 2>/dev/null \
  | python3 -c '
import json,sys
d=json.load(sys.stdin)
seen=set()
for p in d.get("items",[]):
    for c in (p.get("status") or {}).get("containerStatuses") or []:
        img=c.get("image","")
        if "inference-async-worker" in img:
            seen.add((p["metadata"]["name"], img.rsplit(":",1)[-1]))
for n,t in sorted(seen):
    print(f"{n}\t{t}")
')

if [ -z "$RUNNING" ]; then
  echo "  running       : none"
  echo "SKIP no pod in namespace $NS is running this project's image, so there is nothing to compare."
  echo "     This is a SKIP and not a PASS: agreement was not demonstrated."
  exit 0
fi

printf '%s\n' "$RUNNING" | while IFS=$'\t' read -r pod tag; do
  printf "  running       : %-42s %s\n" "$pod" "$tag"
done
MISMATCH=$(printf '%s\n' "$RUNNING" | awk -F'\t' -v p="$PIN" '$2!=p')

echo
if [ -z "$MISMATCH" ]; then
  N=$(printf '%s\n' "$RUNNING" | grep -c .)
  echo "PASS: all $N running pods carry the committed pin $PIN"
  exit 0
fi
echo "FAIL INV15 cluster half: the manifests describe an image that is NOT what is running."
printf '%s\n' "$MISMATCH" | while IFS=$'\t' read -r pod tag; do
  echo "     $pod runs $tag, manifests pin $PIN"
done
echo "     This is the v8-versus-v10 defect exactly: the committed files describe one image and the"
echo "     cluster serves another, so every later question about what produced a measurement has to"
echo "     be answered by archaeology instead of by reading the repository."
exit 1
