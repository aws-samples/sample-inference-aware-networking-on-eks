#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# Proves the CloudFormation merge guard catches both failure modes, offline.
#
# This exists because `aws cloudformation validate-template` does NOT: injection on 2026-08-03
# showed it ACCEPTS both the fused-newline artifact that rolled the real stack back and a duplicate
# logical ID. The local checker is the only layer that rejects either, so it gets a regression test
# rather than trust.
set -uo pipefail
PY_BIN="${PY_BIN:-./.venv/bin/python}"; [ -x "$PY_BIN" ] || PY_BIN=python3
T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
fail=0

# A minimal template with a CloudFormation short-form intrinsic and no trailing newline, which is
# the shape of AWS's own file.
printf 'Resources:\n  KarpenterControllerRole:\n    Type: AWS::IAM::Role\n  KarpenterNodeInstanceProfile:\n    Type: AWS::IAM::InstanceProfile\n  KarpenterNodeRole:\n    Type: AWS::IAM::Role\n    Properties:\n      Arn: !GetAtt SomeQueue.Arn' > "$T/base.yaml"

# 1. clean merge, with the separator: must PASS
cp "$T/base.yaml" "$T/good.yaml"; printf '\n  Extra:\n    Type: AWS::IAM::Role\n' >> "$T/good.yaml"
if "$PY_BIN" tests/check_cfn_merge.py "$T/good.yaml" >/dev/null 2>&1; then
  echo "OK  1 clean merge accepted"; else echo "FAIL 1 clean merge rejected"; fail=1; fi

# 2. fused newline, no separator: must FAIL
cp "$T/base.yaml" "$T/fused.yaml"; printf '# overlay comment fused onto the last byte\n  Extra:\n    Type: AWS::IAM::Role\n' >> "$T/fused.yaml"
if "$PY_BIN" tests/check_cfn_merge.py "$T/fused.yaml" >/dev/null 2>&1; then
  echo "FAIL 2 fused newline ACCEPTED (this is the defect that rolled the stack back)"; fail=1
else echo "OK  2 fused newline rejected"; fi

# 3. duplicate logical ID: must FAIL
cp "$T/base.yaml" "$T/dup.yaml"; printf '\n  KarpenterNodeRole:\n    Type: AWS::IAM::Role\n' >> "$T/dup.yaml"
if "$PY_BIN" tests/check_cfn_merge.py "$T/dup.yaml" >/dev/null 2>&1; then
  echo "FAIL 3 duplicate logical ID ACCEPTED (last-key-wins would silently drop one)"; fail=1
else echo "OK  3 duplicate logical ID rejected"; fi

# The two SUPERSET cases, which need the inputs supplied. These are the dangerous ones: valid
# YAML, no duplicate resource ID, and a clean deploy of the WRONG template.
printf '  A:\n    Type: AWS::IAM::Role\n  B:\n    Type: AWS::IAM::Role\n' > "$T/ov.yaml"
printf 'Resources:\n  KarpenterControllerRole:\n    Type: AWS::IAM::Role\n  KarpenterNodeInstanceProfile:\n    Type: AWS::IAM::InstanceProfile\n  KarpenterNodeRole:\n    Type: AWS::IAM::Role\n' > "$T/pristine.yaml"

# 4. a SECOND top-level Resources: key, which last-key-wins would make replace the first entirely
{ cat "$T/pristine.yaml"; printf '\nResources:\n'; cat "$T/ov.yaml"; } > "$T/second.yaml"
if "$PY_BIN" tests/check_cfn_merge.py "$T/second.yaml" "$T/pristine.yaml" "$T/ov.yaml" >/dev/null 2>&1; then
  echo "FAIL 4 second Resources key ACCEPTED (it would replace every upstream resource)"; fail=1
else echo "OK  4 second Resources key rejected"; fi

# 5. overlay at the WRONG INDENTATION, nesting under an existing resource instead of beside it
{ cat "$T/pristine.yaml"; sed 's/^  /      /' "$T/ov.yaml"; } > "$T/indent.yaml"
if "$PY_BIN" tests/check_cfn_merge.py "$T/indent.yaml" "$T/pristine.yaml" "$T/ov.yaml" >/dev/null 2>&1; then
  echo "FAIL 5 wrong indentation ACCEPTED (overlay resources silently absent)"; fail=1
else echo "OK  5 wrong indentation rejected (drop + cardinality)"; fi

# 6. clean merge WITH the inputs supplied must still pass
{ cat "$T/pristine.yaml"; printf '\n'; cat "$T/ov.yaml"; } > "$T/union.yaml"
if "$PY_BIN" tests/check_cfn_merge.py "$T/union.yaml" "$T/pristine.yaml" "$T/ov.yaml" >/dev/null 2>&1; then
  echo "OK  6 clean union accepted"; else echo "FAIL 6 clean union rejected"; fail=1; fi

echo
[ "$fail" -eq 0 ] && echo "PASS: the CloudFormation merge guard catches all four failure modes" || echo "MERGE GUARD FAILURES ABOVE"
exit $fail
