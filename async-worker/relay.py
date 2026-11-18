# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
The interactive front-door relay: its CLASSIFICATION DUTY, and its HTTP client policy.

SCOPE, stated first because the scope is the design decision. The relay classifies and it relays.
It does NOT pick a replica: the Endpoint Picker does that, from live GPU state, inside whichever
pool the relay names. Two levels at different granularities, which is why they compose (spec 001
section 5.2). Conflating them would throw away the cache-aware replica selection this project
exists to demonstrate, so the relay emits headers and gets out of the way.

TWO DIMENSIONS, TWO HEADERS, AND THEY DO NOT FUSE (decision D-3, spec 001 section 4).

    x-gateway-inference-objective : SLA class        {batch | interactive}
    x-inference-model-class       : capability class {economy | standard | premium}

The pool is selected FROM THE PAIR, never from either header alone. Folding them into one token
collapses a two-dimensional space and loses the interactive-and-hard cell: a request can be
interactive and easy, or batch and hard, and both must be expressible.

CORRECTED 2026-08-04. An earlier version of this module emitted a single fused token
(`batch-standard`) on the objective header. That was wrong twice over. It breaks routing on
contact, because the HTTPRoute matches the objective header EXACTLY against `batch`, so a fused
value falls through to the catch-all interactive rule and lands batch traffic on the interactive
pool: the silent misroute this project has already paid for once. And it conflated the axes in the
code itself, treating an SLA change as though it were a capability change. The classifier's job is
D2, the CAPABILITY label (spec 001 section 5). SLA class comes from the caller and the relay does
not invent it.

WHAT IT READS: FREE SIGNALS ONLY, IN PROCESS.

Free means already present in the request the relay has in hand: prompt length, `max_tokens`,
whether a tool schema is attached, conversation turn depth. No model is loaded, no network call is
made, no accelerator is touched. The free-signal baseline is the defensible v1 (spec 001 section 5
item 3), and its LIMIT is recorded rather than glossed: these signals proxy COST and LENGTH, not
semantic difficulty, so a short-but-hard prompt misroutes. Under decision D-2 there is no cascade,
so that misroute is unrecoverable. The conservative default is what makes the baseline defensible,
not the signals themselves.

DETERMINISTIC CONSTRAINTS SIT ABOVE THE PRECEDENCE TABLE (spec 001 section 6.8; clarification
0b A22, 2026-08-04).

The context window is not a difficulty guess, it is a CAPACITY FACT. The economy class runs at
`max-model-len 2048`; a request needing more does not degrade there, it FAILS AT THE BACKEND after
consuming the trip. So the constraint is evaluated BEFORE any preference, and:

    A PIN IS HONOURED OR REFUSED, NEVER OVERRIDDEN.

Table 7's pin is absolute against the CLASSIFIER, not against physics. An infeasible pin (a pinned
class structurally unable to serve the request) returns an explicit client error naming the
constraint, at the relay, before any routing. The two alternatives were both rejected on the
record: silently upgrading would violate the pin's BILLING meaning, since the caller asked to be
served by a cheaper class and would be charged for a dearer one without being told; forwarding
anyway would fail at the backend regardless, later, more expensively, and with an error that
attributes the failure to the engine rather than to the request.

FAIL-OPEN TO THE FLOOR, NEVER REJECT, and refusal is not an exception to this.

A CLASSIFICATION FAILURE (a malformed body, an unexpected shape, a bug in this module) always
returns a tier, because a classifier that raises converts a placement optimisation into an
availability incident. A REFUSED PIN is a different thing entirely: it is a well-formed request
whose explicit instruction cannot be satisfied by physics, and reporting that is the only honest
answer. The distinction is structural: `classify` returns a `Classification`, and refusal is a
FIELD on it, not an exception. Every path still returns one.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping

# ---------------------------------------------------------------------------------------------
# THE TWO DIMENSIONS. Wire values, spelled exactly as the data plane matches them.

SLA_HEADER = "x-gateway-inference-objective"
CAPABILITY_HEADER = "x-inference-model-class"

SLA_BATCH, SLA_INTERACTIVE = "batch", "interactive"
SLA_CLASSES = (SLA_BATCH, SLA_INTERACTIVE)

CAP_ECONOMY, CAP_STANDARD, CAP_PREMIUM = "economy", "standard", "premium"
# Ordered cheapest to dearest. Upgrade-only means movement is always toward the end of this tuple.
CAP_CLASSES = (CAP_ECONOMY, CAP_STANDARD, CAP_PREMIUM)

# CONTEXT LIMITS PER CAPABILITY CLASS, read from the serving manifests rather than chosen here.
# economy  : manifests/02-serving/vllm-neuron.yaml      --max-model-len 2048
# standard : manifests/02-serving/vllm-{interactive,batch}.yaml  --max-model-len=8192
# premium  : NOT DEPLOYED. Absent rather than guessed: inventing a limit for a class that does not
#            exist would let the constraint check certify a placement into nothing.
CONTEXT_LIMIT = {
    CAP_ECONOMY: 2048,
    CAP_STANDARD: 8192,
    # PREMIUM, added 2026-09-21 (spec-002, the maintainer's ruling 2). manifests/02-serving/vllm-premium.yaml
    # --max-model-len=32768. The ladder is now 2,048 / 8,192 / 32,768, and the rung matters: the
    # 8,193-to-32,768 band flips from `unservable:context-bound` to servable with NO new mechanism,
    # because step 3's cheapest-class-that-fits loop walks CAP_CLASSES in order and now reaches
    # premium. That placement is a FACT (the request needs the tokens), not a difficulty guess, so
    # it is step-3 constraint-driven and inherits A23's bypass of the upgrade budget. The
    # free-signal proxies in step 4 remain capped at CAP_STANDARD and do NOT reach premium until
    # an evaluation on the deployer's own prompts justifies it, which is a separate decision.
    #
    # DERIVED KV, NOT MEASURED: 256 KiB/token against ~22.3 GB gives about 85,000 tokens on an
    # L40S. the maintainer's ruling binds on reading num_gpu_blocks from a live replica and recording it
    # beside this. 16,384 is the recorded fallback, on that measurement and a further ruling only.
    CAP_PREMIUM: 32768,
}

# PREMIUM IS ADMITTED ON THE INTERACTIVE LANE ONLY, AND admission is a DIFFERENT PROPERTY FROM
# ROUTABILITY. The premium pool is one replica with no ScaledObject, so it cannot make the
# throughput commitment the batch lane implies, exactly as the economy pool cannot make the latency
# commitment the interactive lane implies (6.9.1). So:
#
#   interactive + premium : ADMITTED here.
#   batch       + premium : REFUSED here, but REACHABLE BY PROMOTION. The promotion path may
#                           re-place an overrunning interactive+premium request as batch+premium,
#                           and the worker drives it to the premium pool. Exposure is bounded by
#                           construction, because only a request already admitted as
#                           interactive+premium can produce a promoted one.
#
# The relay is the enforcement point, which is consistent with INV12 (sole external exposure). The
# data plane does not police who may send a header; this function does.
PROMOTION_ONLY_PAIRS = {(SLA_BATCH, CAP_PREMIUM)}

# Reserve for the chat template's own wrapping (role markers, generation cue) that the estimate
# below cannot see. Firing early costs money; firing late produces a request the class cannot serve
# at all, so the asymmetry justifies a conservative value.
#
# MEASURED 2026-11-09 with the shipped template and the Qwen3 tokenizer at the pinned revision, on
# self-written prompts of 1 to 661 content tokens, rather than left as the guess it started as: the
# non-thinking template adds a CONSTANT 12 tokens per single-turn request (8 for the role frame and
# generation cue, 4 for the empty think block).
#
# So 64 holds with about 5x headroom. It is KEPT rather than tightened to the measured 12: the
# headroom costs a negligible number of admitted requests, and a margin tuned to one template
# would need re-deriving the moment another model family arrives. See docs/specs/model-facts.yaml.
CONTEXT_SAFETY_MARGIN_TOKENS = 64

# Kept as the economy class's limit under its historical name, because tests, the design document
# and BUILD-NOTES all refer to "the 2,048-token bound" and renaming it would break that thread.
ECONOMY_CONTEXT_TOKENS = CONTEXT_LIMIT[CAP_ECONOMY]

# Default `max_tokens` when the caller omits it. vLLM would generate up to the remaining context, so
# treating an omitted value as zero would let an unbounded generation slip under the bound. Assuming
# the worst is the safe direction here.
DEFAULT_MAX_TOKENS = 512

_WORD = re.compile(r"\S+")


def estimate_prompt_tokens(text: str) -> int:
    """Estimate token count WITHOUT loading a tokenizer.

    Loading the real tokenizer would make the relay carry the model's vocabulary and turn a
    microsecond decision into a millisecond one on the synchronous path. The estimate used is
    whitespace tokens times 4/3, the standard English-text approximation, ROUNDED UP.

    IT IS AN ESTIMATE AND THE DIRECTION OF ITS ERROR IS WHAT MATTERS. Under-counting sends a request
    that does not fit to a class that cannot serve it; over-counting sends a request that would have
    fit to a dearer class and costs money. So the ratio rounds up and the caller adds a margin: both
    choices push the error toward the expensive failure and away from the broken one.
    """
    # COUNTED, NOT COLLECTED. findall built a list holding every word: 17.1 MiB for a 1 MiB prompt
    # of two-letter words, and most of the 400 MB peak a 22 MB body reached (deep security scan
    # finding M1, measured 2026-11-05 and 2026-11-07). finditer yields the same matches one at a
    # time, so the count is identical and the extra memory is about 2 KiB.
    words = sum(1 for _ in _WORD.finditer(text))
    return -(-words * 4 // 3)      # ceiling division, no float rounding to argue about


def _extract_prompt_text(body: Mapping[str, Any]) -> str:
    """Pull the prompt out of either OpenAI-compatible shape the gateway accepts."""
    if isinstance(body.get("prompt"), str):
        return body["prompt"]
    parts: list[str] = []
    for msg in body.get("messages") or []:
        if not isinstance(msg, Mapping):
            continue
        content = msg.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            # Multi-part content: text segments count, image and audio parts do not, because their
            # token cost is model-specific and this module refuses to guess at it.
            for p in content:
                if isinstance(p, Mapping) and isinstance(p.get("text"), str):
                    parts.append(p["text"])
    return "\n".join(parts)


@dataclass(frozen=True)
class Classification:
    """The decision plus WHY, because a placement nobody can explain cannot be debugged.

    `sla` and `capability` are the two wire values, kept SEPARATE all the way through. There is no
    combined field, deliberately: a convenience accessor joining them is how a fused token gets back
    onto the wire. `describe()` exists for LOGS AND EVIDENCE ONLY and says so in its name.

    `refusal` is None for every servable request. When it is set, the request is not routed at all:
    an explicit pin cannot be satisfied by physics, and the caller is told which constraint and by
    how much rather than being silently upgraded or being handed a backend failure later.
    """

    sla: str
    capability: str
    reason: str
    estimated_prompt_tokens: int
    required_context_tokens: int
    refusal: str | None = None

    # THE CHEAPEST CLASS THAT CAN PHYSICALLY SERVE THIS REQUEST, and the reason it is a field
    # rather than something the caller recomputes: THE GUARDS NEED IT AS THEIR FLOOR.
    #
    # Clarification 0b A22 put deterministic constraints ABOVE preferences, and a guard is a
    # preference. Applying the guards with `economy` as a blanket floor let an exhausted rate
    # limiter, or an engaged kill switch, degrade a CONTEXT-BOUND request from standard down to
    # economy, placing a 4,072-token request into a class with a 2,048-token hard limit. The
    # request would then fail at the backend, which is exactly the outcome A22 refuses to produce
    # for a caller's pin, arrived at by a different route.
    #
    # So a guard may withdraw a DISCRETIONARY upgrade and may never go below this.
    min_feasible: str = CAP_ECONOMY

    @property
    def refused(self) -> bool:
        return self.refusal is not None

    def describe(self) -> str:
        """A SHORTHAND FOR LOGS AND EVIDENCE. NEVER a wire value.

        The hyphenated form reads well in a report and is exactly the shape that was previously,
        wrongly, sent on the objective header. It is named `describe` and documented here so the two
        can never be confused again: anything putting this on a request is a defect.
        """
        return f"{self.sla}/{self.capability}"


# THE PAIR-TO-POOL MATRIX (design section 6.9). Four cells, three pools, and one cell that does not
# exist. Encoded here because the relay is what decides the pair, and a matrix the decider does not
# consult is a diagram rather than a rule.
#
#   interactive + standard -> interactive pool          VALID, built
#   interactive + economy  -> COALESCES UP to standard  economy is not served on the interactive SLA
#   batch       + standard -> batch pool                VALID, built
#   batch       + economy  -> neuron pool               VALID BY DESIGN, NOT ROUTABLE YET (9.3)
#   either      + premium  -> none                      UNROUTABLE, rejected at the edge
#
# COALESCING IS NOT A SPECIAL CASE (6.9.1). The economy pool as committed is one replica with no
# ScaledObject and a 2,048-token ceiling, so serving synchronous interactive callers from it is a
# latency and availability commitment the substrate cannot make. Under upgrade-only semantics the
# classifier may RAISE a tier and never lower one, so interactive+economy is raised to
# interactive+standard by the ordinary rule with no new mechanism. The caller pays standard price
# for an economy request, which is the bounded money cost section 6.4 describes, and a caller who
# would rather be refused can pin.
def coalesce(sla: str, capability: str) -> tuple[str, str, str | None]:
    """Apply the matrix. Returns (sla, capability, note) with note set when a cell moved."""
    if sla == SLA_INTERACTIVE and capability == CAP_ECONOMY:
        return sla, CAP_STANDARD, "coalesced:interactive-economy-not-served"
    return sla, capability, None


def _floor(headers: Mapping[str, str]) -> tuple[str, str]:
    """The safe placement: the caller's declared SLA (default batch) at the cheapest capability.

    The relay does NOT invent an SLA class. Promotion between SLA classes is a separate mechanism
    with its own deadline and its own budget; silently moving a request onto the interactive pool
    because it looked hard would spend someone else's scarce capacity on a guess.
    """
    declared = (headers.get(SLA_HEADER) or "").strip().lower()
    sla = declared if declared in SLA_CLASSES else SLA_BATCH
    return sla, CAP_ECONOMY


def classify(
    body: Mapping[str, Any],
    headers: Mapping[str, str],
) -> Classification:
    """Return the SLA class, the capability class, and whether the request is servable at all.

    NEVER raises. Order is deliberate, and the first step is the one clarification 0b A22 added:

      1. DETERMINISTIC CONSTRAINTS. Compute what the request physically needs. A fact.
      2. THE PIN, checked against that fact. Honoured if feasible, REFUSED if not. Never overridden.
      3. CONSTRAINT-DRIVEN SELECTION for an unpinned request: the cheapest class that can serve it.
      4. FREE-SIGNAL DIFFICULTY PROXIES. Guesses, applied last, and they only ever upgrade.
    """
    try:
        sla, _ = _floor(headers)

        # ---- 1. THE DETERMINISTIC CONSTRAINT, computed before any preference is consulted -----
        prompt_tokens = estimate_prompt_tokens(_extract_prompt_text(body))
        max_tokens = body.get("max_tokens")
        max_tokens = DEFAULT_MAX_TOKENS if not isinstance(max_tokens, int) or max_tokens <= 0 else max_tokens
        required = prompt_tokens + max_tokens + CONTEXT_SAFETY_MARGIN_TOKENS

        pinned = (headers.get(CAPABILITY_HEADER) or "").strip().lower()

        # ---- 2. THE PIN: honoured or refused, never overridden ---------------------------------
        if pinned in CAP_CLASSES:
            # NOT ADMITTABLE ON THIS LANE, which is not the same refusal as not deployed. The class
            # exists and is serving; this SLA cannot be submitted against it directly. A caller who
            # cannot tell the two apart cannot act, so the reason string and the body distinguish
            # them: "not deployed" means wait for capacity, "promotion-only" means send it
            # interactive and let it promote.
            if (sla, pinned) in PROMOTION_ONLY_PAIRS:
                return Classification(
                    sla, pinned, f"pin-refused:not-admittable:promotion-only:{sla}+{pinned}",
                    prompt_tokens, required,
                    refusal=(
                        f"capability class '{pinned}' is deployed but is not admittable on the "
                        f"'{sla}' service level; it is reachable only by promotion. Re-send as "
                        f"'{SLA_INTERACTIVE}' with the same capability pin: a request that exceeds "
                        f"the interactive promotion deadline is re-placed automatically as an "
                        f"asynchronous job at the SAME capability class."
                    ),
                )
            limit = CONTEXT_LIMIT.get(pinned)
            if limit is None:
                return Classification(
                    sla, pinned, f"pin-refused:class-not-deployed:{pinned}", prompt_tokens, required,
                    refusal=(
                        f"capability class '{pinned}' is pinned but not deployed in this cluster; "
                        f"no backend can serve it"
                    ),
                )
            if required > limit:
                # REFUSED, NOT UPGRADED. Upgrading would violate the pin's billing meaning;
                # forwarding would fail at the backend later and blame the engine.
                return Classification(
                    sla, pinned, f"pin-refused:context-bound:{required}>{limit}", prompt_tokens, required,
                    refusal=(
                        f"request needs about {required} context tokens (prompt {prompt_tokens} + "
                        f"max_tokens {max_tokens} + {CONTEXT_SAFETY_MARGIN_TOKENS} template margin) "
                        f"but the pinned capability class '{pinned}' has a hard limit of {limit}. "
                        f"A pin is honoured or refused, never overridden: re-send without "
                        f"'{CAPABILITY_HEADER}' to be placed automatically, or pin a larger class."
                    ),
                )
            # A PIN IS STILL SUBJECT TO THE MATRIX, and this is not an override of the pin. The
            # interactive-economy cell is not served at all, so honouring it literally would place
            # the request into a pool that does not accept it. Coalescing UP is the same
            # upgrade-only rule the classifier follows, and it is the behaviour 6.9.1 specifies for
            # a caller who declares that pair. A caller who would rather be refused than upgraded
            # pins a class that exists on its own.
            sla, cap, note = coalesce(sla, pinned)
            return Classification(
                sla, cap, "explicit-caller-pin" + (f"+{note}" if note else ""), prompt_tokens, required,
                min_feasible=cap,   # an honoured pin is not discretionary; guards must not undo it
            )

        # ---- 3. CONSTRAINT-DRIVEN SELECTION: cheapest class that can physically serve ----------
        capability = None
        for c in CAP_CLASSES:
            limit = CONTEXT_LIMIT.get(c)
            if limit is not None and required <= limit:
                capability = c
                break
        if capability is None:
            # Nothing deployed can hold it. This is NOT a pin, so there is no instruction to
            # refuse; it is reported as unservable for the same reason a refused pin is, because
            # forwarding it would fail at the backend and misattribute the cause.
            biggest = max(v for v in CONTEXT_LIMIT.values())
            return Classification(
                sla, CAP_CLASSES[-1], f"unservable:context-bound:{required}>{biggest}", prompt_tokens, required,
                refusal=(
                    f"request needs about {required} context tokens but the largest deployed "
                    f"capability class holds {biggest}. Reduce the prompt or max_tokens."
                ),
            )
        constraint_floor = capability   # the cheapest class that can physically hold the request
        if capability != CAP_ECONOMY:
            sla, capability, _ = coalesce(sla, capability)
            return Classification(
                sla, capability, f"context-bound:{required}>{CONTEXT_LIMIT[CAP_ECONOMY]}", prompt_tokens, required,
                min_feasible=capability,   # forced by physics, so no guard may go below it
            )

        # ---- 4. FREE-SIGNAL DIFFICULTY PROXIES. Guesses, and they only ever UPGRADE ------------
        # Each proxies cost or length rather than semantic difficulty, which is the limit spec 001
        # section 8 item 4 records, so the floor is what they fall back to.
        if body.get("tools") or body.get("functions"):
            # A tool schema means structured output the caller intends to execute. A malformed call
            # is not a slightly worse answer, it is an unusable one, and there is no cascade.
            sla_c, cap_c, note = coalesce(sla, CAP_STANDARD)
            # DISCRETIONARY. A guard may withdraw this one, down to the constraint floor. When the
            # coalescing rule fired, the floor is what the matrix forces rather than economy.
            return Classification(sla_c, cap_c, "tool-schema-attached", prompt_tokens, required,
                                  min_feasible=cap_c if note else constraint_floor)

        turns = sum(1 for m in (body.get("messages") or []) if isinstance(m, Mapping) and m.get("role") == "user")
        if turns >= 4:
            # Deep conversations accumulate context that later turns depend on, so an error early
            # propagates through everything after it.
            sla_c, cap_c, note = coalesce(sla, CAP_STANDARD)
            return Classification(sla_c, cap_c, f"turn-depth:{turns}", prompt_tokens, required,
                                  min_feasible=cap_c if note else constraint_floor)

        sla_f, cap_f, note = coalesce(sla, CAP_ECONOMY)
        # If the matrix coalesced the cell, that placement is FORCED, not discretionary: the cell
        # the caller would otherwise land in is not served at all, so a guard must not push back
        # into it.
        return Classification(
            sla_f, cap_f, note or "floor:no-signal-fired", prompt_tokens, required,
            min_feasible=cap_f,
        )

    except Exception as exc:  # noqa: BLE001
        # FAIL-OPEN TO THE FLOOR. Broad on purpose: the catch exists precisely for the malformed
        # bodies nobody enumerated, and narrowing it to the exceptions already thought of would
        # defeat the point. NOTE that this returns no refusal: a bug in this module must never
        # become a rejected request. Only an explicit infeasible PIN refuses.
        try:
            sla, cap = _floor(headers)
            sla, cap, _ = coalesce(sla, cap)
        except Exception:  # noqa: BLE001
            sla, cap = SLA_BATCH, CAP_ECONOMY
        return Classification(sla, cap, f"fail-open:{type(exc).__name__}", 0, 0, min_feasible=cap)


# ---------------------------------------------------------------------------------------------
# HTTP CLIENT POLICY
#
# THE RELAY RETRIES NOTHING. Not on a read timeout, not on a connection reset, not on a 5xx, and
# NOT ON A CONNECT ERROR EITHER.
#
# That last one is where the relay differs from the async worker, and the difference is worth
# spelling out because copying the worker's policy here would look reasonable and be wrong. The
# worker sets `connect=2`, and that is SAFE THERE because a DynamoDB conditional claim sits BELOW
# it: a duplicate attempt is suppressed by the claim before it can become a second GPU run.
#
# THE RELAY HAS NO CLAIM. It is the synchronous path. There is nothing underneath it to suppress a
# duplicate, so any retry it issues is a second generation on a GPU, billed and served in full.
#
# And the cost is not merely doubled, it is AMPLIFIED, which is the R.5b finding (ARCHITECTURE-
# REVIEW.md): the retry fires precisely when the first attempt is slow, meaning the first attempt is
# a LONG generation, so the duplicate is drawn from the most expensive requests rather than from a
# uniform sample. A retry policy that looks like a small reliability win in aggregate is a large
# cost transfer concentrated on the slowest tail.
#
# A connect error on this path is also not clean evidence that nothing happened. The relay speaks to
# the gateway, which may already have selected a backend and forwarded, so "connection refused" at
# the relay does not prove no generation began.
#
# Therefore: total=0, and the failure surfaces to the caller as a failure. That is the correct
# behaviour on an interactive path, where the caller is present and can decide for themselves,
# rather than having a duplicate generation decided for them by a library default.
RELAY_RETRY_TOTAL = 0


def make_relay_session(timeout_seconds: int):
    """Build the relay's HTTP session with retries structurally off.

    Returns `(session, timeout)`. The timeout is Chain B's R, sourced from the `chain-b-timeouts`
    ConfigMap rather than restated here, so the relay reads the reviewed number instead of
    introducing its own.
    """
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry

    # Every field zero, and listed explicitly rather than relying on the default, so that a future
    # reader can see that each was decided rather than inherited.
    policy = Retry(total=RELAY_RETRY_TOTAL, connect=0, read=0, status=0, redirect=0)
    session = requests.Session()
    adapter = HTTPAdapter(pool_connections=8, pool_maxsize=64, max_retries=policy)
    # Plain HTTP inside the cluster to the gateway ClusterIP, documented under README Security considerations, No TLS.
    session.mount("http://", adapter)  # nosemgrep
    session.mount("https://", adapter)
    return session, timeout_seconds
