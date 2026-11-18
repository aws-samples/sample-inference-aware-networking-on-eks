#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# Guard against unpinned addon installs (DESIGN.md prime directive 2: pin everything in
# versions.yaml). Fails if any infra/addons script fetches from a mutable git ref
# (master / main / HEAD) or pulls a :latest image tag. This is the check that would have
# caught the Neuron device plugin being applied from `master` (defect B.5), and it also
# asserts the three device-plugin versions are present in versions.yaml as the source of truth.
#
# Comments are stripped before matching, so prose that merely mentions "master" or ":latest"
# (for example a note explaining that a version was pinned away from master) does not trip it.
#
# Run:  bash tests/test_addon_pins.sh
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ADDONS="${ROOT}/infra/addons"
VERSIONS="${ROOT}/versions.yaml"
fail=0

scan() {  # $1 = regex, $2 = human description
  local hits=""
  local f
  for f in "${ADDONS}"/*.sh; do
    # Strip comments first so only executable references are matched.
    local m
    m="$(sed 's/#.*//' "$f" | grep -nE "$1" || true)"
    if [ -n "${m}" ]; then
      hits+="$(printf '%s\n' "${m}" | sed "s|^|  ${f##*/}: |")"$'\n'
    fi
  done
  if [ -n "${hits}" ]; then
    echo "FAIL unpinned reference (${2}):"
    printf '%s' "${hits}"
    fail=1
  fi
}

scan 'githubusercontent\.com/[^ "'"'"']*/(master|main|HEAD)/' "mutable git ref in a fetch URL"
scan ':latest([ "'"'"']|$)' "a :latest image tag"

for key in nvidia_device_plugin efa_device_plugin_chart neuron_device_plugin_sdk_tag; do
  if ! grep -qE "^[[:space:]]*${key}:" "${VERSIONS}"; then
    echo "FAIL device-plugin pin '${key}' missing from versions.yaml"
    fail=1
  fi
done

# THE WORKER BASE IMAGE comes from ECR Public and is pinned by digest. A tag alone moved between
# a Trivy scan and the next build (2026-11-02), so a tag is not a pin. The Dockerfile and the
# promotion stub must both carry the exact reference recorded in versions.yaml.
base="$(sed -nE 's/^[[:space:]]*worker_base_image:[[:space:]]*"([^"]+)".*/\1/p' "${VERSIONS}")"
case "${base}" in
  public.ecr.aws/*@sha256:*) ;;
  *) echo "FAIL worker_base_image in versions.yaml is missing, not from public.ecr.aws, or not pinned by digest: '${base}'"; fail=1 ;;
esac
from="$(sed -nE 's/^FROM[[:space:]]+([^[:space:]]+).*/\1/p' "${ROOT}/async-worker/Dockerfile")"
if [ -z "${base}" ] || [ "${from}" != "${base}" ]; then
  echo "FAIL async-worker/Dockerfile FROM '${from}' does not equal worker_base_image"
  fail=1
fi
if ! grep -qF "image: ${base}" "${ROOT}/tests/validation/promotion-stub.yaml"; then
  echo "FAIL tests/validation/promotion-stub.yaml does not use worker_base_image"
  fail=1
fi
# EVERY PYTHON MANIFEST IS PINNED EXACTLY. A range (>=, ~=, <) lets a rebuild resolve a release
# nobody tested. Allowed lines: blank, comment, `-r <file>`, and `name==version`.
for req in "${ROOT}"/requirements*.txt "${ROOT}"/*/requirements*.txt; do
  [ -e "${req}" ] || continue
  bad="$(sed 's/#.*//' "${req}" | grep -vE '^[[:space:]]*$|^-r[[:space:]]+[^[:space:]]+[[:space:]]*$|^[A-Za-z0-9._-]+(\[[A-Za-z0-9,._-]+\])?==[A-Za-z0-9.+!_-]+[[:space:]]*$' || true)"
  if [ -n "${bad}" ]; then
    echo "FAIL ${req#"${ROOT}"/} has a line that is not an exact pin:"
    printf '%s\n' "${bad}" | sed 's/^/  /'
    fail=1
  fi
done
if [ "${fail}" -eq 0 ]; then
  echo "OK  addon installs use pinned references; device-plugin versions sourced from versions.yaml"
  echo "OK  worker base image is the digest-pinned ECR Public reference in versions.yaml; Python manifests are exact pins"
fi
exit "${fail}"
