#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# CLUSTER-HALF of the Karpenter ownership property (design registry: the cluster companion to
# INV16). Asserts that NO IAM role or instance profile matching the Karpenter pattern exists
# OUTSIDE the CloudFormation stack's own resource list.
#
# WHY BOTH HALVES. INV16 is static and offline: it proves the MANIFESTS ask for a stack-owned
# instance profile. It cannot see what is actually in the account. Karpenter mints profiles at
# RUNTIME, so a controller running with an older NodeClass, a hand-created role, or a version whose
# behaviour differs would leave unowned objects that no static check can detect. This is the check
# that would have caught the 2026-08-03 teardown condition BEFORE the teardown rather than during it.
#
# Cluster-gated: needs AWS credentials. Runs on the post-rebuild activation list, not in
# make test-offline, whose no-cluster no-credentials property is deliberately preserved.
set -uo pipefail
CLUSTER="${CLUSTER_NAME:-inference-aware-eks}"
STACK="Karpenter-${CLUSTER}"
fail=0

# What the stack itself owns, by physical id.
OWNED=$(aws cloudformation list-stack-resources --stack-name "$STACK" \
  --query 'StackResourceSummaries[?ResourceType==`AWS::IAM::Role`||ResourceType==`AWS::IAM::InstanceProfile`].PhysicalResourceId' \
  --output text 2>/dev/null | tr '\t' '\n' | sort -u)
if [ -z "$OWNED" ]; then
  echo "FAIL cannot read stack $STACK; cannot establish ownership"
  exit 1
fi
echo "stack-owned IAM objects:"; echo "$OWNED" | sed 's/^/  /'

# Everything in the account matching the Karpenter pattern. The dropped leading K is DELIBERATE so
# the pattern matches both capitalizations; see BUILD-NOTES. Do not "fix" it.
ACCT_ROLES=$(aws iam list-roles --output json | jq -r '.Roles[].RoleName | select(test("arpenter"))' | sort -u)
ACCT_PROFS=$(aws iam list-instance-profiles --output json | jq -r '.InstanceProfiles[].InstanceProfileName | select(test("arpenter") or test("'"$CLUSTER"'"))' | sort -u)

check(){ # $1 = kind, $2 = newline list
  printf '%s\n' "$2" | while IFS= read -r n; do
    [ -z "$n" ] && continue
    if printf '%s\n' "$OWNED" | grep -qxF "$n"; then
      echo "OK   $1 $n is stack-owned"
    else
      echo "FAIL $1 $n exists but is NOT in stack $STACK. Karpenter or a script created IAM the stack cannot delete, which is the 2026-08-03 teardown condition."
      echo "UNOWNED" >> /tmp/karpenter_ownership_fail
    fi
  done
}
rm -f /tmp/karpenter_ownership_fail
check "role"             "$ACCT_ROLES"
check "instance profile" "$ACCT_PROFS"
[ -f /tmp/karpenter_ownership_fail ] && fail=1; rm -f /tmp/karpenter_ownership_fail

echo
if [ "$fail" -eq 0 ]; then echo "PASS: no Karpenter IAM exists outside the stack; teardown is one stack delete"
else echo "FAIL: unowned Karpenter IAM present; teardown would need manual steps"; fi
exit $fail
