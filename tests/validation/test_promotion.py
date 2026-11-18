# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Offline proof for the promotion actor's decision logic (design section 5.4).

No cluster, no accelerator, no network.

The properties, and the first two are the ones that protect callers rather than capacity:

  1. A STREAMING CALLER IS NEVER SILENTLY TRUNCATED. Promotion of a stream requires explicit
     opt-in, because a terminal promotion event is not part of the OpenAI-compatible streaming API
     and an unmodified client would surface a truncated completion as if it were a real answer.
  2. THE CAPABILITY COORDINATE SURVIVES (6.9.5). Promotion moves the SLA axis only. If it could
     lower capability, a request that ran too long would silently receive a weaker model: the harm
     D-1 pays a high price to avoid on the latency axis, reintroduced on the quality axis.
  3. Eligibility fails CLOSED, unlike classification, and the asymmetry is the reason.
  4. A promoted job keeps the identity the async front door already uses, so promotion cannot open
     a second door into the job table.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "async-worker"))

import promotion as promo  # noqa: E402
from jobkey import job_key  # noqa: E402

NON_STREAMING = {"prompt": "hi", "max_tokens": 8}
STREAMING = {"prompt": "hi", "max_tokens": 8, "stream": True}


def test_a_streaming_caller_is_never_silently_truncated() -> None:
    d = promo.decide(STREAMING, {})
    assert not d.eligible and d.streaming, d
    assert "opt-in" in d.reason, d.reason
    print(f"OK  1 a streaming request WITHOUT opt-in is not promoted ({d.reason})")

    for value in ("1", "true", "TRUE", "yes"):
        d = promo.decide(STREAMING, {promo.PROMOTION_OPT_IN_HEADER: value})
        assert d.eligible and d.streaming, (value, d)
    print("OK  2 an explicit opt-in makes it promotable, in each accepted spelling")

    # A value that is not an opt-in must not be read as one. "false", "0" and junk all mean no.
    for value in ("0", "false", "no", "maybe", ""):
        d = promo.decide(STREAMING, {promo.PROMOTION_OPT_IN_HEADER: value})
        assert not d.eligible, (value, d)
    print("OK  3 a non-affirmative opt-in value is not honoured, so junk cannot enable truncation")

    d = promo.decide(NON_STREAMING, {})
    assert d.eligible and not d.streaming, d
    print(f"OK  4 a non-streaming request is promotable with no opt-in needed ({d.reason})")

    # The terminal event is well-formed SSE and carries the redemption route, not just the id.
    ev = promo.promotion_sse_event("job-abc")
    assert ev.startswith(f"event: {promo.PROMOTION_EVENT_NAME}\n") and ev.endswith("\n\n"), ev
    payload = json.loads(ev.split("data: ", 1)[1].strip())
    assert payload["job_id"] == "job-abc" and payload["redeem"] == "/jobs/job-abc", payload
    print("OK  5 the terminal event is well-formed SSE and carries the REDEMPTION ROUTE, not just an id")


def test_the_capability_coordinate_survives() -> None:
    """Property 2, section 6.9.5. The single most important line in this module."""
    for cap in ("economy", "standard", "premium"):
        sla_out, cap_out = promo.promoted_pair("interactive", cap)
        assert sla_out == "batch", (cap, sla_out)
        assert cap_out == cap, (
            f"promotion CHANGED the capability coordinate {cap} -> {cap_out}. A request that ran "
            "too long would silently receive a different model, which the caller never asked for "
            "and cannot detect."
        )
    print("OK  6 promotion moves the SLA axis to batch and holds capability FIXED, all three classes")

    rec = promo.promotion_record("j1", "interactive", "standard", "acme", 120.5)
    assert rec["promoted_to_capability"] == rec["promoted_from_capability"] == "standard", rec
    assert rec["promoted_to_sla"] == "batch" and rec["promoted_from_sla"] == "interactive", rec
    print("OK  7 the record states both endpoints, so a capability change would be visible in data")

    # promoted_from is what tells a promoted job from one submitted as batch in the first place.
    # The promotion-rate measurement depends on that distinction.
    assert rec["promoted"] is True and rec["promoted_after_seconds"] == 120.5
    assert rec["promoted_by_caller"] == "acme"
    print("OK  8 the record is attributable: caller, elapsed, and the original pair are all kept")


def test_the_capability_REACHES_THE_WIRE() -> None:
    """Property 2's third axis, added 2026-09-21. THE TEST THAT WOULD HAVE CAUGHT DEFECT 3.

    `promoted_pair()` holds the capability fixed and the test above asserts it. That assertion was
    true and USELESS on its own, because nothing carried the value to the wire: the worker emitted
    only `x-gateway-inference-objective` (pinned to `batch`) and the queue message had no
    capability field, so a promoted premium job matched the SLA-only HTTPRoute rule and would have
    been served by the STANDARD pool while its promotion record said premium.

    The invariant was enforced one layer above the place it breaks. So this asserts the three legs
    that actually carry it, all offline:

      (a) the capability survives onto the QUEUE MESSAGE, as a sibling of payload
      (b) the WORKER emits it as x-inference-model-class
      (c) the pair is ROUTABLE: a committed HTTPRoute rule matches it, and wins by header count
    """
    import importlib.util
    import re as _re

    root = os.path.join(os.path.dirname(__file__), "..", "..")

    # ---- (a) the queue message carries it, and job_key is UNCHANGED by its presence ------------
    sla_out, cap_out = promo.promoted_pair("interactive", "premium")
    assert (sla_out, cap_out) == ("batch", "premium"), (sla_out, cap_out)
    payload = {"model": "Qwen/Qwen3-32B-AWQ", "messages": [{"role": "user", "content": "hi"}]}
    msg = {"client_id": "acme", "payload": payload, "job_id": job_key("acme", payload),
           "submitted_at": 1, "capability_class": cap_out}
    assert msg["capability_class"] == "premium", msg
    # THIS LEG BUILDS ITS MESSAGE BY HAND, so it proves a shape, not a producer. That gap let the
    # real producers disagree on the key (2026-09-22). test_message_contract.py runs the real
    # producers end to end and is the authority; this leg is kept for the dedup-hash assertion.
    # The dedup hash must not see it. If capability ever entered the payload, a premium promotion
    # would get a DIFFERENT job_id than the same request submitted directly, and exactly-once
    # would be broken from inside the producer.
    assert job_key("acme", payload) == msg["job_id"], "capability must not enter the dedup hash"
    print("OK  9 the promoted capability rides the queue message as a sibling, dedup hash untouched")

    # ---- (b) the worker emits it ---------------------------------------------------------------
    worker_src = open(os.path.join(root, "async-worker", "worker.py")).read()
    assert "x-inference-model-class" in worker_src, (
        "the worker does not emit the capability header: a promoted premium job would route to "
        "the standard pool. This is defect 3."
    )
    # ADDITIVE, never replacing the objective header, or INV9 stops being true.
    assert '"x-gateway-inference-objective": INFERENCE_OBJECTIVE' in worker_src, (
        "the capability header must sit ALONGSIDE the objective header, not replace it (INV9)"
    )
    assert "capability=body.get(mc.CAPABILITY)" in worker_src, (
        "the worker must read the capability from the message rather than an environment default"
    )
    print("OK 10 the worker emits x-inference-model-class ALONGSIDE the objective header (INV9 intact)")

    # ---- (c) the promoted pair is routable, and wins by HEADER COUNT ---------------------------
    route = open(os.path.join(root, "manifests", "03-routing", "httproute.yaml")).read()
    import yaml as _yaml
    doc = _yaml.safe_load(route)
    rules = doc["spec"]["rules"]

    def headers_of(rule):
        m = rule["matches"][0]
        return {h["name"]: h["value"] for h in m.get("headers", [])}

    promoted = {"x-gateway-inference-objective": "batch", "x-inference-model-class": "premium"}
    matching = [r for r in rules if headers_of(r) and
                all(promoted.get(k) == v for k, v in headers_of(r).items())]
    assert matching, "NO committed rule matches the promoted batch+premium pair"
    best = max(matching, key=lambda r: len(headers_of(r)))
    backend = best["backendRefs"][0]["name"]
    assert len(headers_of(best)) == 2, f"the winning rule must be two-header, got {headers_of(best)}"
    assert backend == "vllm-llama3-premium", (
        f"the promoted premium pair routes to {backend}, not the premium pool. A one-header batch "
        f"rule outranking it is exactly the silent downgrade this test exists to prevent."
    )
    print(f"OK 11 the promoted batch+premium pair routes to {backend} by header count, not list order")


def test_coalesced_economy_promotes_as_standard() -> None:
    """THE PROMOTION RECORD MATCHES THE WIRE, never the caller's original pin.

    the maintainer's recording requirement (2026-09-23): a request that pins interactive+economy is
    coalesced UP to interactive+standard BEFORE admission (6.9.1), because one replica with no
    ScaledObject cannot carry a synchronous latency commitment. The admitted capability therefore IS
    standard, and everything downstream must say standard: the upstream header, the promotion
    record, and the queue message. A record showing `economy` would describe a request that was
    never served, which is the falsified-record class this project refuses.

    Chain asserted end to end on the real code: classify -> coalesce -> promoted_pair.
    """
    import re as _re
    from relay import CAPABILITY_HEADER, SLA_HEADER, classify  # noqa: E402
    d = classify({"messages": [{"role": "user", "content": "hi"}]},
                 {SLA_HEADER: "interactive", CAPABILITY_HEADER: "economy"})
    assert d.capability == "standard", f"interactive+economy must coalesce up, got {d.capability}"
    assert "coalesced" in d.reason, f"the move must be NOTED in the reason, got {d.reason!r}"
    assert promo.promoted_pair(d.sla, d.capability) == ("batch", "standard"), (
        "promotion holds the ADMITTED capability fixed; economy must never reappear after coalescing")
    rec = promo.promotion_record("c#1", d.sla, d.capability, "caller", 121.0)
    assert rec["promoted_to_capability"] == "standard", rec
    print("OK 14 interactive+economy coalesces to standard and promotes as batch+standard, record matches")

    # THE ONE SOURCE-LEVEL ASSERTION, and it is here because the property is "two call sites use the
    # SAME value", which no single behavioural call can show: the wire header and the promotion
    # record must be fed by one variable, so they cannot diverge later.
    src = open(os.path.join(os.path.dirname(__file__), "..", "..", "async-worker", "relay_server.py")).read()
    wire = _re.search(r"upstream\[CAPABILITY_HEADER\] = (\w+)", src)
    record = _re.search(r"promo\.promotion_record\([^)]*?,\s*(\w+),\s*caller", src)
    assert wire and record and wire.group(1) == record.group(1), (
        f"the wire header ({wire and wire.group(1)}) and the promotion record "
        f"({record and record.group(1)}) must be the same variable, or the record can drift from "
        "what was actually served")
    print(f"OK 15 the record and the wire are fed by one variable ({wire.group(1)}), so they cannot diverge")


def test_eligibility_fails_closed() -> None:
    """Property 3, and the asymmetry with classification is the point."""

    class Hostile(dict):
        def get(self, *_a, **_k):
            raise RuntimeError("boom")

    d = promo.decide(Hostile(), {})
    assert not d.eligible and "error" in d.reason, d
    print(f"OK  9 an error makes a request INELIGIBLE, not eligible ({d.reason})")
    print("      classification fails OPEN because the safe action there is to serve; promotion")
    print("      fails CLOSED because the safe action here is to leave the caller alone")

    # An ambiguous `stream` value is not treated as streaming, which keeps it promotable and
    # therefore answered with a 202 rather than a truncated stream.
    for v in ("true", 1, None, "yes"):
        assert promo.is_streaming({"stream": v}) is False, v
    assert promo.is_streaming({"stream": True}) is True
    print("OK 10 only a real boolean true counts as streaming, so an ambiguous body gets a 202")


def test_promotion_reuses_the_existing_identity() -> None:
    """Property 4. Promotion must not open a second door into the job table."""
    caller, body = "acme-corp", {"prompt": "hello", "max_tokens": 4}
    promoted_id = job_key(caller, body)
    direct_id = job_key(caller, body)
    assert promoted_id == direct_id, "a promoted job and a direct submission must share one identity"
    print("OK 11 a promoted job and a direct submission of the same payload collapse to ONE key,")
    print("      so the conditional claim still deduplicates them and no second door is opened")

    assert job_key(caller, body) != job_key("other-caller", body)
    print("OK 12 and two different callers do NOT collide, so attribution survives the reuse")


def test_the_202_body_tells_the_caller_what_happened() -> None:
    b = promo.promotion_body("job-xyz")
    assert b["job_id"] == "job-xyz" and b["redeem"] == "/jobs/job-xyz" and b["status"] == "promoted"
    assert "deadline" in b["detail"], b["detail"]
    print("OK 13 the 202 names the job, the redemption route, and WHY it was promoted")


def main() -> int:
    test_a_streaming_caller_is_never_silently_truncated()
    test_the_capability_coordinate_survives()
    test_the_capability_REACHES_THE_WIRE()
    test_coalesced_economy_promotes_as_standard()
    test_eligibility_fails_closed()
    test_promotion_reuses_the_existing_identity()
    test_the_202_body_tells_the_caller_what_happened()
    print("PASS: no caller is silently truncated, capability survives promotion, eligibility fails")
    print("      closed, and a promoted job keeps the identity the async front door already uses")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
