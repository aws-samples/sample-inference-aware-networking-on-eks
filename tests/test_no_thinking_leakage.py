#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
THE THINKING-MODE GATE. Generated output must carry zero tokens between think tags.

WHY THIS EXISTS, measured rather than assumed. Qwen3 defaults to thinking ON. With it on, the model
emits `<think>` INLINE IN `content` and there is no `reasoning_content` field to separate it,
confirmed identically on BOTH stacks this project serves: Neuron with vLLM 0.13.0 (pre-check A) and
the GPU DLC with vLLM 0.24.0 (pre-check B). Neither has a reasoning parser configured, so both
behave the same way and the gate needs only one shape.

The cost is not subtle. A 64-token budget produced ZERO answer tokens and `finish_reason: length`,
the entire budget consumed by reasoning. The same question with thinking disabled returned "Four."
in three tokens and stopped cleanly. Time-to-first-token, cancellation timing and any capability
judgement would all be measuring the reasoning trace rather than the answer.

WHY IT ASSERTS ON `content` AND NOT ON A REASONING FIELD. Because that is where the tags land here.
A gate written against `reasoning_content` would pass silently on both stacks while the leakage it
exists to catch sat untouched in `content`.

THE FAILURE MODE THIS CATCHES IS THE QUIET KIND. A missing generation cue announces itself: the
answer comes back empty. Thinking leakage announces nothing. The response is well formed,
the status is 200, the content is prose. It is simply reasoning text where the answer should be,
which is the doubled-BOS shape rather than the blank-answer shape.

PROVEN THE HOUSE WAY, AND THE FIRST VERSION OF THIS FILE WAS NOT. It carried two positive controls
and both were caught by the SAME branch, the unclosed-opener path, because both were truncated
responses. The closed-block branch, which counts words inside a complete `<think>...</think>`, never
fired in any fixture. That branch handles the MOST COMMON real case: a thinking response whose
budget was sufficient to close the block and then answer. So the gate reported PASS while half its
matcher was unproven.

The fix is not just an extra fixture. The suite now asserts that EVERY detection branch is exercised
by at least one control, so an inert branch fails the run rather than hiding behind a passing one.
A checker whose branches are untested is a checker that has only been partly written.
"""
from __future__ import annotations

import re
import sys

# The tag pair Qwen3 emits. Non-greedy so multiple blocks in one response are each matched.
THINK_BLOCK = re.compile(r"<think>(.*?)</think>", re.DOTALL)
# An unclosed opener is leakage too, and is what a truncated response looks like when the budget
# runs out mid-reasoning: `finish_reason: length` with an open <think> and no closing tag.
UNCLOSED_OPENER = re.compile(r"<think>(?!.*?</think>)", re.DOTALL)

# The branch names, so the suite can assert each one was actually exercised.
BRANCH_UNCLOSED = "unclosed-opener"
BRANCH_CLOSED = "closed-block-with-reasoning"
BRANCH_CLEAN = "clean"
DETECTION_BRANCHES = {BRANCH_UNCLOSED, BRANCH_CLOSED}


def thinking_leakage(content: str) -> tuple[bool, str, str]:
    """(leaked, branch, why). `branch` names which rule decided, so branches can be proven live."""
    if content is None:
        return False, BRANCH_CLEAN, "content is None, nothing to leak"
    if UNCLOSED_OPENER.search(content):
        return (True, BRANCH_UNCLOSED,
                "an UNCLOSED <think> opener, which is what a budget exhausted mid-reasoning looks like")
    for body in THINK_BLOCK.findall(content):
        # The non-thinking template injects an EMPTY block into the PROMPT prefix. If one ever
        # reaches the completion it must still be empty; anything else is generated reasoning.
        if body.strip():
            n = len(body.strip().split())
            return True, BRANCH_CLOSED, f"a closed <think> block carrying {n} words of reasoning"
    return False, BRANCH_CLEAN, "no think tags, or only empty ones"


# ---------------------------------------------------------------------------------------------
# FIXTURES. Entries marked CONTROL must leak; the suite fails if any of them does not, and fails
# separately if the controls between them do not exercise every detection branch.
FIXTURES = [
    # (name, content, must_leak, expect_branch)
    (
        "CONTROL: truncated thinking, verbatim from pre-check A on Neuron vLLM 0.13.0",
        '<think>\nOkay, the user is asking "What is 2+2? Answer in one word." Let me think. The '
        'straightforward answer is 4. But they want it in one word. Hmm. Maybe "four"?',
        True,
        BRANCH_UNCLOSED,
    ),
    (
        "CONTROL: budget exhausted mid-reasoning, unclosed opener",
        "<think>\nLet me work through this carefully. First I need to consider",
        True,
        BRANCH_UNCLOSED,
    ),
    (
        "CONTROL: CLOSED block then an answer, the most common real case and the one the first "
        "version of this file never tested",
        "<think>\nThe user wants 2 plus 2, which is 4, and they asked for one word.\n</think>\n\nFour.",
        True,
        BRANCH_CLOSED,
    ),
    (
        "clean answer, thinking disabled, verbatim from pre-check B on GPU vLLM 0.24.0",
        "Four.",
        False,
        BRANCH_CLEAN,
    ),
    (
        "empty think block reaching the completion is NOT leakage: the non-thinking template "
        "injects exactly this into the prompt prefix",
        "<think>\n\n</think>\n\nFour.",
        False,
        BRANCH_CLEAN,
    ),
    (
        "prose that merely discusses thinking must not trip the gate",
        "The model's thinking mode can be disabled with a chat template.",
        False,
        BRANCH_CLEAN,
    ),
]


def main() -> int:
    failures = 0
    controls = 0
    controls_caught = 0
    branches_exercised: set[str] = set()

    for name, content, must_leak, expect_branch in FIXTURES:
        leaked, branch, why = thinking_leakage(content)
        is_control = name.startswith("CONTROL")
        if is_control:
            controls += 1
            if leaked:
                controls_caught += 1
                branches_exercised.add(branch)

        wrong_verdict = leaked != must_leak
        wrong_branch = branch != expect_branch
        status = "FAIL" if (wrong_verdict or wrong_branch) else "OK  "
        if wrong_verdict or wrong_branch:
            failures += 1
        print(f"{status} {name}")
        print(f"       leak={leaked} (expected {must_leak}), branch={branch} "
              f"(expected {expect_branch})")
        print(f"       {why}")

    print()
    # CHECK ONE ON THE CHECKER: every control must actually be caught.
    if controls == 0:
        print("FAIL no control present: a gate with no failing fixture proves nothing")
        failures += 1
    elif controls_caught != controls:
        print(f"FAIL only {controls_caught} of {controls} controls were caught. The matcher is "
              f"inert, so every pass above proves nothing.")
        failures += 1
    else:
        print(f"OK   all {controls} controls were caught, so the matcher is live rather than inert")

    # CHECK TWO ON THE CHECKER, and this is the one the first version of this file lacked: every
    # detection branch must be exercised by some control. Two controls hitting the same branch
    # leave the other branch unproven while the suite still reports PASS.
    unexercised = DETECTION_BRANCHES - branches_exercised
    if unexercised:
        print(f"FAIL detection branches never exercised by any control: {sorted(unexercised)}. "
              f"An untested branch is not a checked case, however green the run looks.")
        failures += 1
    else:
        print(f"OK   every detection branch exercised by a control: {sorted(DETECTION_BRANCHES)}")

    print()
    print("FAIL: thinking-leakage gate" if failures else
          "PASS: thinking-leakage gate, every branch proven against a control")
    return 1 if failures else 0


# ---------------------------------------------------------------------------------------------
# LIVE MODE (2026-09-22, the maintainer's ruling: "the template is the mechanism and the gate is the proof").
#
# The fixture suite above proves the MATCHER. It proves nothing about what the cluster generates. The
# first live premium request of 2026-09-22 returned an unclosed <think> with every one of its tokens
# spent on reasoning, from a server that was meant to have thinking off; the fixtures were green the
# whole time. So this mode sends real requests through the gateway to every routable Qwen3 cell with
# ready capacity and applies the same thinking_leakage() to what comes back, plus the property the
# leak destroyed: at the 64-token budget that once produced ZERO answer tokens, the answer must
# finish on its own (finish_reason "stop") with non-empty content.
#
# Cells with zero ready replicas are reported PEND, never counted as passes, and a run that checks
# no cell at all exits 2. The economy tier is not gateway-routable (design 9.3), so it is PEND here
# by construction and is recorded as such rather than silently omitted.
LIVE_CELLS = (  # (objective, capability, serving Deployment)
    ("interactive", "standard", "vllm-llama3-interactive"),
    ("batch", "standard", "vllm-llama3-batch"),
    ("interactive", "premium", "vllm-llama3-premium"),
    ("batch", "premium", "vllm-llama3-premium"),
    # ECONOMY BECAME A REAL CELL on 2026-09-23, in the same change that routed it (L4 branch, 2f).
    # It was hardcoded as "not gateway-routable (design 9.3)" PEND; that reason expired the moment
    # the two-header batch+economy rule landed, and a PEND that outlives its reason is the
    # stale-claim class this repository corrects elsewhere. interactive+economy is deliberately
    # absent: the relay coalesces it up to standard before admission (6.9.1), so the gateway never
    # sees that pair.
    ("batch", "economy", "vllm-llama3-economy"),
)
LIVE_PROMPTS = ("What is 2+2? Answer in one word.", "Name the capital of France in one word.",
                "Is 7 a prime number? Answer yes or no.")
LIVE_BUDGET = 64


def live() -> int:
    import os
    import requests
    root = os.path.join(os.path.dirname(__file__), "..")
    sys.path.insert(0, os.path.join(root, "loadtest"))
    sys.path.insert(0, os.path.join(root, "tests", "lib"))
    import cluster_probe as cp
    from discover import reachable_gateway
    gw, cleanup = reachable_gateway()
    checked = failures = 0
    try:
        for sla, cap, dep in LIVE_CELLS:
            cell = f"{sla}+{cap}"
            if cp.ready_replicas(dep) < 1:
                print(f"PEND {cell:22s} {dep} has 0 ready replicas: NOT checked, NOT a pass")
                continue
            model = cp.served_model(dep)
            for prompt in LIVE_PROMPTS:
                r = requests.post(f"{gw}/v1/chat/completions", timeout=120, headers={
                    "content-type": "application/json", "x-gateway-inference-objective": sla,
                    "x-inference-model-class": cap}, json={
                    "model": model, "max_tokens": LIVE_BUDGET, "temperature": 0,
                    "messages": [{"role": "user", "content": prompt}]})
                checked += 1
                if r.status_code != 200:
                    failures += 1
                    print(f"FAIL {cell:22s} HTTP {r.status_code}: {r.text[:160]}")
                    continue
                ch = r.json()["choices"][0]
                content, finish = ch["message"].get("content"), ch.get("finish_reason")
                leaked, branch, why = thinking_leakage(content)
                bad = leaked or finish != "stop" or not (content or "").strip()
                failures += bad
                print(f"{'FAIL' if bad else 'OK  '} {cell:22s} finish={finish} branch={branch} content={content!r:.70}")
                if leaked:
                    print(f"       {why}")
        print("  NOTE interactive+economy   coalesced up to standard before admission (6.9.1), so the "
              "gateway never sees it: not a cell, not a PEND")
    finally:
        cleanup()
    if checked == 0:
        print("PEND: live thinking gate checked NO cell. This is NOT a pass.")
        return 2
    print(f"{'FAIL' if failures else 'PASS'}: live thinking gate, {checked} generations, {failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(live() if "--live" in sys.argv[1:] else main())
