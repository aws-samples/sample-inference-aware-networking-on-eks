#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# Offline proof that `make cluster-down` deletes only on THIS project's cluster (deep security scan
# finding M2). It runs the REAL Makefile target with fake `aws`, `kubectl` and `eksctl` on PATH that
# record every call and change nothing, so it needs no cluster and no credentials.
#
# Every branch of infra/cluster/require-cluster-context.sh is exercised, the passing one included:
#   wrong-cluster  current context reaches another API server   -> refuse, no delete, no eksctl
#   no-context     kubectl has no current context               -> refuse, no delete, no eksctl
#   aws-error      describe-cluster fails (expired credentials) -> refuse, no delete, no eksctl
#   match          current context reaches this cluster         -> every kubectl call carries
#                                                                  --context <it>, then eksctl runs
#   absent         AWS reports no such cluster                  -> kubectl steps skipped, eksctl runs
#   hostile names  a context named with make or shell syntax    -> refuse, nothing it names runs
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1
REPO=$(pwd)
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
mkdir -p "$WORK/bin"
OURS="https://OURS.gr7.us-west-2.eks.amazonaws.com"

cat > "$WORK/bin/kubectl" <<'EOF'
#!/usr/bin/env bash
if [ "${1:-}" = config ] && [ "${2:-}" = current-context ]; then
  [ -n "${FAKE_CTX:-}" ] || { echo "error: current-context is not set" >&2; exit 1; }
  printf '%s\n' "$FAKE_CTX"; exit 0
fi
if [ "${1:-}" = config ] && [ "${2:-}" = view ]; then printf '%s' "${FAKE_SERVER:-}"; exit 0; fi
printf 'kubectl %s\n' "$*" >> "$FAKE_LOG"
EOF
cat > "$WORK/bin/aws" <<'EOF'
#!/usr/bin/env bash
case "$1 $2" in
  "eks describe-cluster")
    case "$FAKE_AWS" in
      ok) echo "$FAKE_OURS" ;;
      absent) echo "An error occurred (ResourceNotFoundException) when calling the DescribeCluster operation: No cluster found" >&2; exit 254 ;;
      *) echo "An error occurred (ExpiredTokenException) when calling the DescribeCluster operation" >&2; exit 254 ;;
    esac ;;
  "ec2 describe-instances"|"elbv2 describe-load-balancers") echo 0 ;;
  "sts get-caller-identity") echo 111122223333 ;;
  *) printf 'aws %s\n' "$*" >> "$FAKE_LOG" ;;
esac
EOF
cat > "$WORK/bin/eksctl" <<'EOF'
#!/usr/bin/env bash
printf 'eksctl %s\n' "$*" >> "$FAKE_LOG"
EOF
chmod +x "$WORK/bin/"*

fail=0
run() {   # name, FAKE_CTX, FAKE_SERVER, FAKE_AWS
  log="$WORK/$1.log"; : > "$log"
  out=$(env PATH="$WORK/bin:/usr/bin:/bin:/usr/sbin:/sbin" HOME="$WORK" FAKE_LOG="$log" \
        FAKE_CTX="$2" FAKE_SERVER="$3" FAKE_AWS="$4" FAKE_OURS="$OURS" \
        make -s -C "$REPO" cluster-down 2>&1)
  rc=$?
}
expect_refused() {
  if [ "$rc" -ne 0 ] && ! grep -qE 'delete|scale|eksctl' "$log" && printf '%s' "$out" | grep -q REFUSING; then
    echo "OK  $1: refused (exit $rc), no kubectl delete or scale, eksctl not run"
  else
    echo "FAIL $1: rc=$rc"; sed 's/^/       /' "$log"; printf '%s\n' "$out" | tail -5 | sed 's/^/       /'; fail=1
  fi
}

run wrong-cluster "prod-ctx" "https://OTHER.gr7.us-east-1.eks.amazonaws.com" ok; expect_refused wrong-cluster
run no-context    ""         ""                                              ok; expect_refused no-context
run aws-error     "ours-ctx" "$OURS"                                         expired; expect_refused aws-error

run match "ours-ctx" "$OURS" ok
kc=$(grep -c '^kubectl ' "$WORK/match.log")
unbound=$(grep '^kubectl ' "$WORK/match.log" | grep -vc -- "--context ours-ctx")
if [ "$rc" -eq 0 ] && [ "$kc" -eq 5 ] && [ "$unbound" -eq 0 ] && grep -q '^eksctl delete cluster' "$WORK/match.log"; then
  echo "OK  match: all $kc kubectl calls carry --context ours-ctx, then eksctl deletes the cluster"
else
  echo "FAIL match: rc=$rc kubectl=$kc unbound=$unbound"; sed 's/^/       /' "$WORK/match.log"; fail=1
fi

run absent "prod-ctx" "https://OTHER.gr7.us-east-1.eks.amazonaws.com" absent
if [ "$rc" -eq 0 ] && ! grep -q '^kubectl ' "$WORK/absent.log" && grep -q '^eksctl delete cluster' "$WORK/absent.log" \
   && printf '%s' "$out" | grep -q 'skipped (the cluster no longer exists)'; then
  echo "OK  absent: no kubectl call at all (even with a foreign context current), eksctl and IAM cleanup still run"
else
  echo "FAIL absent: rc=$rc"; sed 's/^/       /' "$WORK/absent.log"; fail=1
fi

# HOSTILE CONTEXT NAMES. The guard's output is re-expanded by make, so a name carrying make or shell
# syntax must be refused before it is printed, even when its server is this cluster's endpoint.
for hostile in "ours\$(shell touch $WORK/pwned)" "ours'ctx" "ours ABSENT" "ABSENT"; do
  rm -f "$WORK/pwned"
  run hostile "$hostile" "$OURS" ok
  if [ "$rc" -ne 0 ] && [ ! -e "$WORK/pwned" ] && ! grep -qE 'delete|scale|eksctl' "$WORK/hostile.log"; then
    echo "OK  hostile context name refused, nothing run: $(printf '%q' "$hostile")"
  else
    echo "FAIL hostile context name: $(printf '%q' "$hostile") rc=$rc pwned=$([ -e "$WORK/pwned" ] && echo yes || echo no)"; fail=1
  fi
done

[ "$fail" -eq 0 ] && echo "PASS: cluster-down deletes only on the cluster the eksctl config names" || exit 1
