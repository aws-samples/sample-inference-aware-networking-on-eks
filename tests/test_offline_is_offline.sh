#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# HARDENING 4: THE OFFLINE PROMISE BECOMES A TEST.
#
# `tests/test_invariant_efficacy.sh` says "Offline. No cluster, no credentials." That was true when
# written and stopped being true the moment INV19 was added, silently, because every gate stayed
# green: a check that reaches a cluster SUCCEEDS when a cluster is reachable. The repair was
# INV19_SKIP, and INV19_SKIP DEPENDS ON SOMEBODY REMEMBERING TO SET IT. The next cluster-reaching
# invariant will not be covered by it.
#
# THIS GATE DEPENDS ON NOTHING BEING REMEMBERED. It strips the environment of every credential and
# of KUBECONFIG, points HOME at an empty directory so ~/.kube and ~/.aws cannot be found, and runs
# the offline suite. If the suite still passes, it was genuinely offline. If it fails or hangs
# reaching for a cluster, it was not.
#
# THE INVERSE IS PROVEN TOO, in the same run: a deliberately cluster-reaching variant must FAIL
# under the same stripping. Without that, a gate that passes because it silently skips everything
# would look identical to a gate that passes because the suite is genuinely offline.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1
REPO=$(pwd)
fail=0

STRIP_HOME=$(mktemp -d)
strip(){
  env -i PATH="/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin" HOME="$STRIP_HOME" \
      KUBECONFIG=/nonexistent AWS_CONFIG_FILE=/nonexistent \
      AWS_SHARED_CREDENTIALS_FILE=/nonexistent \
      "$@"
}

echo "=== the offline suite must pass with NO credentials and NO kubeconfig ==="
echo "    HOME=$STRIP_HOME (empty), KUBECONFIG=/nonexistent, no AWS_* in the environment"
if strip "$REPO/.venv/bin/python" -c 'pass' 2>/dev/null; then :; fi
out=$(strip bash "$REPO/tests/verify_invariants.sh" 2>&1)
rc=$?
if [ "$rc" -eq 0 ]; then
  echo "OK  verify_invariants passes with the environment stripped, so it is genuinely offline"
else
  echo "FAIL verify_invariants does NOT pass with the environment stripped. Something in it reaches"
  echo "     for a cluster or for credentials, and every green reading so far was green because"
  echo "     those happened to be available."
  printf '%s\n' "$out" | grep -E '^FAIL' | sed 's/^/       /' | head -5
  fail=1
fi
# INV19 must report PEND, not OK and not FAIL: a cluster-gated check with no cluster has not checked.
if printf '%s' "$out" | grep -qE '^PEND +INV19'; then
  echo "OK  INV19 reports PEND under stripping, so 'could not check' never renders as 'it is fine'"
else
  echo "FAIL INV19 did not PEND under stripping. It either passed without reading a cluster, which"
  echo "     is the reassuring-proxy failure, or it failed, which would make the offline suite"
  echo "     depend on credentials."
  fail=1
fi

echo
echo "=== THE INVERSE: a deliberately cluster-reaching check MUST fail under the same stripping ==="
# Without this, a gate that passes because everything silently skipped is indistinguishable from a
# gate that passes because the suite is genuinely offline. This is the same reason the efficacy
# harness asserts that its injection changed the bytes.
PROBE=$(mktemp -d)/probe.sh
cat > "$PROBE" <<'PROBEEOF'
#!/usr/bin/env bash
kubectl get nodes >/dev/null 2>&1 || exit 1
exit 0
PROBEEOF
chmod +x "$PROBE"
if strip bash "$PROBE"; then
  echo "FAIL the control probe REACHED a cluster under stripping, so the stripping does not strip."
  echo "     Every OK above is therefore meaningless and must not be believed."
  fail=1
else
  echo "OK  the control probe cannot reach a cluster under stripping, so the stripping is real"
fi
rm -rf "$STRIP_HOME" "$(dirname "$PROBE")"

echo
if [ "$fail" -eq 0 ]; then
  echo "PASS: the offline suite is offline BY MEASUREMENT rather than by promise, and the"
  echo "      measurement is itself controlled against a probe that must fail"
else
  echo "FAILURES ABOVE"
fi
exit $fail
