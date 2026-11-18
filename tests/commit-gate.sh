#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# THE COMMIT GATE. Makes two observed failure modes INEXPRESSIBLE rather than discouraged.
#
# WHY IT EXISTS, and it is not a general tidiness measure. On 2026-08-05 two commits landed on RED
# gates. The second time was inside the commit whose message NAMED the first slip and said "gates
# green before commit means before". And the same command used `git commit -m` with backticks,
# which the shell EXECUTED, leaving 51968ea reading "the worker's signature gained ." with the word
# missing.
#
# The diagnosis is not carelessness, it is that DISCIPLINE HELD IN MEMORY FAILS UNDER FATIGUE. This
# project's answer to that has always been structure: the marker rather than the reminder, the
# positive assertion rather than the absent annotation, the printed threshold rather than the
# remembered one. This is that answer applied to committing.
#
#   1. Gates run BEFORE the commit, and a red gate REFUSES. Not warns.
#   2. Messages come from a FILE via `-F`. An inline message is refused outright, so a backtick
#      can never reach the shell.
#
# Usage:  make commit MSG=.commitmsg-foo.txt
set -uo pipefail
MSG="${1:-}"

if [ -z "$MSG" ]; then
  echo "REFUSED: no message file. Usage: make commit MSG=path/to/message.txt"
  echo "  An INLINE message is not accepted, ever. That is the point: 51968ea's message was"
  echo "  mangled because the shell executed a backticked word inside 'git commit -m'."
  exit 1
fi
[ -f "$MSG" ] || { echo "REFUSED: message file '$MSG' does not exist."; exit 1; }
[ -s "$MSG" ] || { echo "REFUSED: message file '$MSG' is empty."; exit 1; }

# A message file that is itself about to be committed is a mistake worth catching: the .gitignore
# covers .commitmsg*.txt, so anything else named here is probably a real file being clobbered.
case "$(basename "$MSG")" in
  .commitmsg*) : ;;
  *) echo "NOTE: '$MSG' is not a .commitmsg*.txt file, so it is NOT gitignored and may be committed."
     echo "      Continuing, but check that is what you meant." ;;
esac

# NOTHING STAGED is a distinct failure from a red gate, and it must say so. `git commit`'s own
# "no changes added to commit" is clear on its own but confusing arriving from a wrapper, because
# the reader has just watched the gates pass and reasonably concludes the wrapper broke.
if git diff --cached --quiet 2>/dev/null; then
  echo "REFUSED: nothing is staged. Stage first (git add -A), then re-run."
  echo "  The gates are not run for an empty commit: there is nothing to gate."
  echo "  Staging is left deliberate rather than automatic, because 'git add -A' inside a commit"
  echo "  wrapper is how unrelated work gets swept into a commit that does not describe it."
  exit 1
fi

# THE GATES ARE NOT RUN HERE ANY MORE, as of 2026-08-07. They run ONCE, in .githooks/pre-commit,
# which `git commit` below triggers. Two reasons, and the second is the structural one:
#
#   1. This wrapper ran test-offline and then the hook ran it AGAIN, doubling every commit's wall
#      clock for no additional assurance.
#   2. THE HOOK IS THE BROADER GUARD. It covers a direct `git commit`, which this wrapper by
#      definition cannot. Enforcing in two places meant the narrower one could drift from the
#      broader one while both looked authoritative.
#
# What remains here is the half the hook CANNOT do: forcing the message file. By the time a hook
# runs the message is already composed, so `-F` has to be imposed at the call site.
echo "=== commit gate: message form enforced here, gates enforced once in the pre-commit hook ==="
if [ "$(git config core.hooksPath)" != ".githooks" ]; then
  echo "REFUSED: core.hooksPath is not .githooks, so the pre-commit hook would NOT run and this"
  echo "  wrapper no longer runs the gates itself. Run 'make hooks' first."
  echo "  This check exists because moving enforcement into the hook makes an uninstalled hook a"
  echo "  SILENT loss of all gating rather than a redundant one."
  exit 1
fi
echo "  hooks installed (core.hooksPath=.githooks). Committing with -F; the hook gates it."
git commit -F "$MSG"
