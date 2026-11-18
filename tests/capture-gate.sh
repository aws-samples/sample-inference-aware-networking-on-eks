#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# GATE OUTPUT PERSISTS ON FAILURE. Wraps a gate command; on a non-zero exit the full output is
# written to `.gate-failures/<gate>-<utc timestamp>.log` and the exit code is passed through
# unchanged, so the wrapper cannot turn a red gate green.
#
# WHY. In batch 2 `make dry-run` returned Error 1 once and three later runs exited 0. The cause
# remains unexplained, because the output was gone by the time anyone went looking: it had scrolled
# past in a terminal and nothing had written it down. An unexplained transient is expensive exactly
# in proportion to how little of it survives.
#
# WHY A TIMESTAMP IN THE FILENAME RATHER THAN ONE FILE PER GATE. A fixed name has to be either
# overwritten or deleted on the next run. Overwriting destroys the transient, which is the one
# capture worth having; deleting on success means a genuine failure log vanishes the moment someone
# re-runs the gate. Both defeat the purpose. Distinct files never collide, and each one names the
# moment it belongs to, so no log can be misread as describing the current state.
#
# `.gate-failures/` is GITIGNORED: it is a local scratch area that accumulates, and committing every
# red gate would bury the repository in noise. PROMOTION IS THE DELIBERATE STEP: a capture that is
# worth keeping, an unexplained transient above all, is COPIED INTO `evidence/`, which IS tracked.
# The default is local and cheap; keeping something is a decision someone makes.
#
# Usage:  bash tests/capture-gate.sh <gate-name> <command> [args...]
set -uo pipefail
[ "$#" -ge 2 ] || { echo "usage: capture-gate.sh <gate-name> <command> [args...]" >&2; exit 2; }
name="$1"; shift

cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1
mkdir -p .gate-failures
tmp=$(mktemp); trap 'rm -f "$tmp"' EXIT

"$@" 2>&1 | tee "$tmp"
rc=${PIPESTATUS[0]}

if [ "$rc" -ne 0 ]; then
  log=".gate-failures/${name}-$(date -u +%Y%m%dT%H%M%SZ).log"
  {
    echo "# gate     : ${name}"
    echo "# command  : $*"
    echo "# exit     : ${rc}"
    echo "# when     : $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "# head     : $(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
    echo "# tree     : $(git status --porcelain 2>/dev/null | wc -l | tr -d ' ') modified paths"
    echo "# ---"
    cat "$tmp"
  } > "$log"
  echo ">> gate '${name}' FAILED (exit ${rc}). Output captured to ${log}"
  echo ">> If this is an unexplained transient, PROMOTE it: cp '${log}' evidence/"
fi
exit "$rc"
