# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
The promotion actor's decision logic: who is eligible, what the caller is told, and what survives.

Design section 5.4 and its decisions. This module holds the parts that can be decided and tested
with NO cluster, NO accelerator and NO network, so the policy is provable before the mechanism runs.
The relay server owns the I/O; this owns the rules.

WHAT PROMOTION IS. An interactive request that has not answered within P seconds is ABANDONED by
closing its connection, and RE-PLACED as an asynchronous job. The caller receives a job id instead
of a completion. It exists so that decision D-1's tier isolation survives a long request: without
it, one slow generation holds a scarce interactive slot for the whole route budget.

ELIGIBILITY IS NARROW ON PURPOSE (section 5.4.3).

  Non-streaming            PROMOTABLE. Nothing has been sent, so a 202 with the job id is available
                           and the three outcomes stay distinguishable.
  Streaming, no opt-in     NOT PROMOTABLE, and this is the shipping default. A terminal promotion
                           event is NOT part of the OpenAI-compatible streaming API, so an
                           unmodified client library would see an unrecognised event followed by a
                           stream that simply ends, and would surface a SILENTLY TRUNCATED
                           completion. That is worse than a timeout: a timeout is an error the
                           caller handles, a truncated answer looks like a real answer and
                           propagates.
  Streaming, opted in      PROMOTABLE. The caller has declared it understands the terminal event,
                           so the stream ends with a named event carrying the job id and the
                           redemption route.

THE CAPABILITY LABEL SURVIVES (section 6.9.5). Promotion moves a request along the SLA axis and
MUST hold the capability coordinate fixed: interactive+standard promotes to batch+standard, never
to batch+economy. If promotion could lower capability, a request that ran too long would silently
receive a weaker model, which is a quality regression the caller never asked for and cannot detect.
That is exactly the harm D-1 pays a high price to avoid on the latency axis, reintroduced on the
quality axis by an accident of implementation.

PRE-REGISTRATION HAPPENS ON ARRIVAL, NOT AT THE DEADLINE. The job id must exist before it can be
handed out, and deriving it at the deadline would mean the caller's identity for the job depends on
when the deadline fired. It uses the SAME `job_key` the async front door uses, so a promoted job and
a directly submitted duplicate collapse to one identity and the conditional claim still deduplicates
them. Promotion must not open a second door into the job table.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

# The caller's declaration that it understands a terminal promotion event on a stream. Absent this,
# a streaming request is never promoted, which is what keeps an unmodified OpenAI client safe.
PROMOTION_OPT_IN_HEADER = "x-accept-promotion-event"

# What the caller gets back. Names are stable because they are a contract.
PROMOTION_EVENT_NAME = "promotion"
REDEMPTION_ROUTE = "/jobs/{job_id}"


@dataclass(frozen=True)
class PromotionDecision:
    """Whether this request may be promoted, and why not when it may not.

    `reason` is populated in BOTH directions. An ineligible request that was slow is a thing an
    operator will want to explain, and "it was streaming and did not opt in" is the explanation.
    """

    eligible: bool
    reason: str
    streaming: bool


def is_streaming(body: Mapping[str, Any]) -> bool:
    """OpenAI-compatible `stream: true`. Anything unparseable is treated as NOT streaming.

    The default direction is deliberate: treating an ambiguous body as non-streaming makes it
    PROMOTABLE, and the promotion path answers with a 202 rather than a truncated stream. Guessing
    the other way would make a genuinely streaming caller ineligible, which is merely a lost
    optimisation, but guessing this way cannot truncate anyone: a caller who is not really streaming
    receives a valid 202, and a caller who IS streaming without saying so has already departed from
    the API contract this module reads.
    """
    return body.get("stream") is True


def decide(body: Mapping[str, Any], headers: Mapping[str, str]) -> PromotionDecision:
    """Eligibility, per section 5.4.3. Never raises."""
    try:
        streaming = is_streaming(body)
        if not streaming:
            return PromotionDecision(True, "eligible:non-streaming", False)
        opted = (headers.get(PROMOTION_OPT_IN_HEADER) or "").strip().lower()
        if opted in ("1", "true", "yes"):
            return PromotionDecision(True, "eligible:streaming-opted-in", True)
        return PromotionDecision(False, "ineligible:streaming-without-opt-in", True)
    except Exception as exc:  # noqa: BLE001
        # NOT ELIGIBLE on error. Promotion is an optimisation; failing to promote costs a slot,
        # while wrongly promoting a streaming caller truncates their answer. The asymmetry decides
        # the direction, and it is the opposite of the classifier's fail-open for the same reason:
        # there, the safe action was to serve; here, the safe action is to leave alone.
        return PromotionDecision(False, f"ineligible:error:{type(exc).__name__}", False)


def promoted_pair(sla: str, capability: str) -> tuple[str, str]:
    """The pair the re-placed job carries. Section 6.9.5: capability is HELD FIXED.

    Promotion moves the SLA axis only. Returning the capability unchanged is the whole rule, and it
    is a function rather than an inline expression so that the one place it could be got wrong is
    named, tested, and greppable.
    """
    return "batch", capability


def promotion_record(job_id: str, sla: str, capability: str, caller: str, elapsed_s: float) -> dict:
    """The fields the promotion writes alongside the job, for attribution and for later analysis.

    `promoted_from` keeps the ORIGINAL pair. Without it a promoted job is indistinguishable from a
    job that was submitted as batch in the first place, and any later analysis of promotion rate or
    tier quality needs to tell those apart.
    """
    to_sla, to_cap = promoted_pair(sla, capability)
    return {
        "job_id": job_id,
        "promoted": True,
        "promoted_from_sla": sla,
        "promoted_from_capability": capability,
        "promoted_to_sla": to_sla,
        "promoted_to_capability": to_cap,
        "promoted_after_seconds": round(elapsed_s, 3),
        "promoted_by_caller": caller,
    }


def promotion_body(job_id: str) -> dict:
    """The 202 payload for a non-streaming caller."""
    return {
        "status": "promoted",
        "job_id": job_id,
        "redeem": REDEMPTION_ROUTE.format(job_id=job_id),
        "detail": (
            "this request exceeded the interactive promotion deadline and was re-placed as an "
            "asynchronous job; redeem the result at the route above"
        ),
    }


def promotion_sse_event(job_id: str) -> str:
    """The terminal event for an opted-in streaming caller.

    Named event plus a JSON payload, then the stream ends. Only a caller that sent the opt-in header
    can ever receive this, which is what preserves the guarantee that the three outcomes stay
    distinguishable: a caller who never opted in can never see the third one.
    """
    import json

    payload = json.dumps({"job_id": job_id, "redeem": REDEMPTION_ROUTE.format(job_id=job_id)})
    return f"event: {PROMOTION_EVENT_NAME}\ndata: {payload}\n\n"
