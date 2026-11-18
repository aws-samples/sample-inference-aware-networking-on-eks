#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# Every document generator must PARSE. Offline, no cluster, no credentials, milliseconds.
#
# Why this exists as a gate rather than as care: the v6.2.1 pass introduced a mismatched quote in
# a table row, and the build failed with a SyntaxError instead of emitting a wrong document. That
# save was the generator pattern working, but it depended on someone running the build. This makes
# it structural. A generator that does not parse cannot produce the committed artifact, so a
# non-parsing generator in the tree means the .docx beside it is unreproducible.
set -uo pipefail
PY_BIN="${PY_BIN:-./.venv/bin/python}"
[ -x "$PY_BIN" ] || PY_BIN=python3
fail=0; n=0
while IFS= read -r f; do
  n=$((n+1))
  if "$PY_BIN" -c "import ast,sys; ast.parse(open(sys.argv[1]).read())" "$f" 2>/dev/null; then
    printf "OK   parses  %s\n" "$f"
  else
    printf "FAIL syntax  %s\n" "$f"
    "$PY_BIN" -c "import ast,sys; ast.parse(open(sys.argv[1]).read())" "$f" 2>&1 | tail -3
    fail=1
  fi
# Enumerated from GIT-TRACKED files, not from a filesystem walk with an exclusion list.
# Rooting at the repo with --not-path exclusions found five generators that a docs/-rooted
# glob had missed, which was the right result, but an exclusion list rots the moment a new
# virtualenv name or vendored directory appears. Tracked files are inherently correct and
# there is nothing to maintain: an untracked generator is not part of the repository, and a
# newly tracked one is picked up with no edit here.
done < <(git ls-files '*build_*.py' | sort)
echo
if [ "$fail" -eq 0 ]; then echo "ALL $n GENERATORS PARSE"; else echo "GENERATOR SYNTAX FAILURES ABOVE"; fi
exit $fail
