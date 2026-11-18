# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Offline proof for the relay's classification duty and its HTTP client policy.

No cluster, no accelerator, no credentials, no tokenizer, no model.

The properties, in the order they matter:

  1. TWO DIMENSIONS, TWO HEADERS, NEVER FUSED (D-3). The SLA class and the capability class are
     separate all the way through, and nothing joins them into a wire value.
  2. DETERMINISTIC CONSTRAINTS SIT ABOVE THE PRECEDENCE TABLE (clarification 0b A22). The context
     bound is a capacity fact, evaluated before any preference.
  3. A PIN IS HONOURED OR REFUSED, NEVER OVERRIDDEN. An infeasible pin returns a named error rather
     than being silently upgraded (which would violate its billing meaning) or forwarded (which
     would fail at the backend later and blame the engine).
  4. CLASSIFICATION FAILS OPEN TO THE FLOOR, and refusal is not an exception to it: a bug in the
     module never rejects a request, only an explicit infeasible pin does.
  5. The classifier only ever UPGRADES on the capability axis, and never touches the SLA axis.
  6. The relay's HTTP client retries NOTHING, including connect.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "async-worker"))

from relay import (  # noqa: E402
    CAP_ECONOMY,
    CAP_PREMIUM,
    CAP_STANDARD,
    CAPABILITY_HEADER,
    CONTEXT_LIMIT,
    CONTEXT_SAFETY_MARGIN_TOKENS,
    DEFAULT_MAX_TOKENS,
    ECONOMY_CONTEXT_TOKENS,
    RELAY_RETRY_TOTAL,
    SLA_BATCH,
    SLA_HEADER,
    SLA_INTERACTIVE,
    classify,
    coalesce,
    estimate_prompt_tokens,
    make_relay_session,
)

NO_HEADERS: dict = {}
ECON_LIMIT = CONTEXT_LIMIT[CAP_ECONOMY]
STD_LIMIT = CONTEXT_LIMIT[CAP_STANDARD]

# A prompt comfortably over the economy limit but well under the standard one.
OVER_ECONOMY = {"prompt": "word " * 3000, "max_tokens": 8}


def test_the_two_axes_never_fuse() -> None:
    """Property 1. The defect that broke routing on contact was a single fused token."""
    c = classify({"prompt": "hi", "max_tokens": 8}, NO_HEADERS)
    assert c.sla == SLA_BATCH and c.capability == CAP_ECONOMY, c
    assert not hasattr(c, "tier"), "a combined `tier` field is how a fused token gets back on the wire"
    print(f"OK  1 the decision carries two separate axes: sla={c.sla} capability={c.capability}")

    # The SLA class comes from the caller and the classifier never invents it. Every capability
    # outcome below must leave it exactly as declared.
    for body in ({"prompt": "hi"}, OVER_ECONOMY, {"prompt": "hi", "tools": [{"type": "function"}]}):
        c = classify(body, {SLA_HEADER: SLA_INTERACTIVE})
        assert c.sla == SLA_INTERACTIVE, f"the classifier changed the SLA class: {c}"
    print("OK  2 the declared SLA class survives every capability decision, unchanged")

    # An unrecognised SLA value is not honoured and does not raise: it falls back to the floor,
    # which is the conservative reading of a value nobody defined.
    c = classify({"prompt": "hi"}, {SLA_HEADER: "platinum"})
    assert c.sla == SLA_BATCH, c
    print("OK  3 an unrecognised SLA value falls back to batch rather than being honoured")

    # describe() is the log shorthand. It must be the ONLY place the hyphenated form appears, and
    # it must not be mistakable for a wire value.
    c = classify({"prompt": "hi"}, NO_HEADERS)
    assert c.describe() == f"{SLA_BATCH}/{CAP_ECONOMY}"
    assert "-" not in c.describe(), "the shorthand must not look like the old fused wire token"
    print(f"OK  4 describe() is a log shorthand only: {c.describe()!r}, and it is not the old form")


def test_constraints_sit_above_preferences() -> None:
    """Property 2. The context bound is a capacity fact, not a difficulty guess."""
    c = classify({"prompt": "hi", "max_tokens": 8}, NO_HEADERS)
    assert c.capability == CAP_ECONOMY and c.reason == "floor:no-signal-fired"
    print(f"OK  5 a short request stays at the cheapest class ({c.reason})")

    for name, body in (
        ("max_tokens alone", {"prompt": "hi", "max_tokens": ECON_LIMIT + 1}),
        ("prompt alone", OVER_ECONOMY),
    ):
        c = classify(body, NO_HEADERS)
        assert c.capability == CAP_STANDARD and c.reason.startswith("context-bound"), (name, c)
        print(f"OK    {name} over the bound selects standard ({c.reason})")

    # THE SUM. Both terms individually comfortable, together they do not fit. A per-field check
    # would pass this.
    gen = ECON_LIMIT // 2
    words = (gen - 10) * 3 // 4
    c = classify({"prompt": "w " * words, "max_tokens": gen}, NO_HEADERS)
    assert c.capability == CAP_STANDARD and c.reason.startswith("context-bound"), c
    print(f"OK  6 the SUM is what is checked, not either term alone ({c.reason})")

    # An omitted max_tokens assumes the default, never zero.
    room = ECON_LIMIT - DEFAULT_MAX_TOKENS - CONTEXT_SAFETY_MARGIN_TOKENS
    c = classify({"prompt": "w " * (room * 3 // 4 + 200)}, NO_HEADERS)
    assert c.capability == CAP_STANDARD, c
    print(f"OK  7 an omitted max_tokens assumes the default, not zero ({c.reason})")

    # Beyond EVERY deployed class: reported unservable rather than forwarded to fail at a backend.
    c = classify({"prompt": "w " * 40000, "max_tokens": 8}, NO_HEADERS)
    assert c.refused and c.reason.startswith("unservable:context-bound"), c
    print(f"OK  8 beyond every deployed class is reported unservable, not forwarded ({c.reason})")


def test_a_pin_is_honoured_or_refused_never_overridden() -> None:
    """Property 3. This is the ruling, and it is the case the live test got wrong."""
    # FEASIBLE pin: honoured exactly, no second-guessing.
    c = classify({"prompt": "hi", "max_tokens": 8}, {CAPABILITY_HEADER: CAP_STANDARD})
    assert c.capability == CAP_STANDARD and c.reason == "explicit-caller-pin" and not c.refused, c
    print(f"OK  9 a feasible pin is honoured exactly ({c.reason})")

    # INFEASIBLE pin: economy pinned on a request over the economy bound. THE DEFECT CASE.
    # Previously this was HONOURED, sending a 4,072-token request to a 2,048-token backend.
    c = classify(OVER_ECONOMY, {CAPABILITY_HEADER: CAP_ECONOMY})
    assert c.refused, f"an infeasible pin was not refused, which is the frozen-design defect: {c}"
    assert c.reason.startswith("pin-refused:context-bound"), c
    assert c.capability == CAP_ECONOMY, "the refusal must report the class the CALLER pinned"
    assert str(ECON_LIMIT) in c.refusal and str(c.required_context_tokens) in c.refusal, (
        "the error must name the constraint AND the numbers, or the caller cannot act on it"
    )
    print(f"OK 10 an infeasible pin is REFUSED, naming the constraint ({c.reason})")

    # NOT UPGRADED. Stated as its own assertion because silent upgrade is the tempting fix and it
    # violates the pin's billing meaning: the caller asked for a cheaper class and would be charged
    # for a dearer one without being told.
    assert c.capability != CAP_STANDARD, "the refused pin was silently upgraded"
    print("OK 11 and it is NOT silently upgraded, which would violate the pin's billing meaning")

    # THE MIRROR, which is what makes this a rule rather than a special case: pinning a class that
    # CAN serve the same oversized request routes normally.
    c = classify(OVER_ECONOMY, {CAPABILITY_HEADER: CAP_STANDARD})
    assert not c.refused and c.capability == CAP_STANDARD, c
    print(f"OK 12 the same oversized request pinned to standard routes normally ({c.reason})")

    # PREMIUM IS DEPLOYED AS OF 2026-09-21 (spec-002, the maintainer's ruling 1), so the assertion that
    # used to encode "premium is not deployed" INVERTS. Three cases replace the one, because
    # admission and routability became different properties.
    assert CAP_PREMIUM in CONTEXT_LIMIT, "premium is deployed; this test encodes that fact"
    assert CONTEXT_LIMIT[CAP_PREMIUM] == 32768, "ruling 2: the ladder is 2,048 / 8,192 / 32,768"
    print(f"OK 13 premium is deployed and bounds context at {CONTEXT_LIMIT[CAP_PREMIUM]}")

    # CASE A. interactive+premium is ADMITTED: the lane premium serves directly.
    c = classify({"prompt": "hi"}, {SLA_HEADER: SLA_INTERACTIVE, CAPABILITY_HEADER: CAP_PREMIUM})
    assert not c.refused and c.capability == CAP_PREMIUM, c
    print(f"OK 13a interactive+premium is admitted ({c.reason})")

    # CASE B. batch+premium is REFUSED AT ADMISSION but is reachable by promotion, and the refusal
    # must say WHICH of the two it is. "not deployed" means wait for capacity; "promotion-only"
    # means send it interactive and let it promote. A caller who cannot tell them apart cannot act.
    c = classify({"prompt": "hi"}, {SLA_HEADER: SLA_BATCH, CAPABILITY_HEADER: CAP_PREMIUM})
    assert c.refused, "external batch+premium must not be admitted"
    assert "not-admittable:promotion-only" in c.reason, c
    assert "not deployed" not in c.refusal, "this is the WRONG refusal: premium IS deployed"
    assert "promotion" in c.refusal, "the refusal must tell the caller the reachable path"
    print(f"OK 13b batch+premium is admission-refused as promotion-only, not as undeployed ({c.reason})")

    # CASE C. THE LADDER RUNG. A request over the standard bound used to be
    # `unservable:context-bound`; with premium deployed it is servable, and it arrives there by
    # STEP 3 constraint-driven selection (a fact about the request) rather than by a free-signal
    # guess. No code path was added for this: the cheapest-class-that-fits loop simply reaches
    # premium now that premium is in CONTEXT_LIMIT.
    over_standard = {"prompt": "word " * 9000, "max_tokens": 512}
    c = classify(over_standard, {})
    assert not c.refused and c.capability == CAP_PREMIUM, c
    assert c.reason.startswith("context-bound"), f"must be constraint-driven, not a guess: {c}"
    print(f"OK 13c a request over the standard bound now reaches premium by constraint ({c.reason})")

    # AND THE FREE SIGNALS MUST NOT. Step 4's proxies target standard and stay there until an
    # evaluation on the deployer's own prompts justifies more; letting a guess reach the dearest class is the
    # "wastes money, linearly and predictably" case with none of the offsetting protection.
    c = classify({"prompt": "hi", "tools": [{"name": "f"}]}, {})
    assert c.capability == CAP_STANDARD, f"a free signal reached premium: {c}"
    print("OK 13d a free-signal upgrade stops at standard and does not reach premium")

    # An unrecognised pin value is ignored (not refused): it is not an instruction the system
    # understands, so falling through to automatic placement is the conservative reading.
    c = classify({"prompt": "hi"}, {CAPABILITY_HEADER: "turbo"})
    assert not c.refused and c.reason == "floor:no-signal-fired", c
    print("OK 14 an unrecognised pin value falls through to automatic placement rather than refusing")


def test_fails_open_and_refusal_is_not_an_exception_to_it() -> None:
    """Property 4. A bug never rejects. Only an explicit infeasible pin does."""
    malformed = [
        {}, {"prompt": None}, {"messages": "not a list"}, {"messages": [None, 3, "x"]},
        {"messages": [{"role": "user", "content": {"unexpected": "shape"}}]},
        {"prompt": "ok", "max_tokens": "not an int"}, {"prompt": "ok", "max_tokens": -5},
        {"tools": "not a list either"},
    ]
    for b in malformed:
        c = classify(b, NO_HEADERS)
        assert c.capability in CONTEXT_LIMIT and c.sla in (SLA_BATCH, SLA_INTERACTIVE), (b, c)
        assert not c.refused, f"a malformed body was REFUSED instead of failing open: {b} -> {c}"
    print(f"OK 15 {len(malformed)} malformed bodies, all placed at a real class, none refused")

    class Hostile(dict):
        def get(self, *_a, **_k):
            raise RuntimeError("boom")

    c = classify(Hostile(), NO_HEADERS)
    assert c.reason.startswith("fail-open:") and "RuntimeError" in c.reason and not c.refused, c
    print(f"OK 16 a body raising on access falls open to the floor and never refuses ({c.reason})")


def test_only_ever_upgrades_on_the_capability_axis() -> None:
    """Property 5."""
    c = classify({"prompt": "hi", "tools": [{"type": "function"}]}, NO_HEADERS)
    assert c.capability == CAP_STANDARD and c.reason == "tool-schema-attached", c
    print(f"OK 17 a tool schema upgrades capability, and says so ({c.reason})")

    deep = {"messages": [{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}] * 4}
    c = classify(deep, NO_HEADERS)
    assert c.capability == CAP_STANDARD and c.reason.startswith("turn-depth:"), c
    print(f"OK 18 turn depth upgrades capability, and says so ({c.reason})")

    # No path may select a class cheaper than the floor. CAP_ECONOMY is already cheapest, so this
    # asserts the ordering itself holds and nothing returns something outside it.
    for b in ({"prompt": "hi"}, {}, deep, OVER_ECONOMY):
        c = classify(b, NO_HEADERS)
        if not c.refused:
            assert c.capability in CONTEXT_LIMIT, c
    print("OK 19 every non-refused decision names a class that is actually deployed")


def test_the_pair_to_pool_matrix_is_obeyed() -> None:
    """Design section 6.9. The matrix is a RULE, and the relay is what applies it.

    A matrix the decider does not consult is a diagram. This was a genuine conformance defect: the
    relay held interactive+economy as a placement, and that cell IS NOT SERVED.
    """
    # 6.9.1: interactive+economy COALESCES UP, it is not refused and not held.
    sla, cap, note = coalesce(SLA_INTERACTIVE, CAP_ECONOMY)
    assert (sla, cap) == (SLA_INTERACTIVE, CAP_STANDARD) and note, (sla, cap, note)
    print(f"OK 25 interactive+economy coalesces up to standard, not held and not refused ({note})")

    # It coalesces because upgrade-only ALREADY does this. No new mechanism, per 6.9.1.
    c = classify({"prompt": "hi", "max_tokens": 8}, {SLA_HEADER: SLA_INTERACTIVE})
    assert (c.sla, c.capability) == (SLA_INTERACTIVE, CAP_STANDARD), c
    assert not c.refused, "coalescing must not refuse: the caller gets a working answer"
    print(f"OK 26 an interactive caller with no capability opinion lands interactive/standard ({c.reason})")

    # Even an explicit PIN of that cell coalesces, and this is not an override of the pin: the cell
    # is not served at all, so honouring it literally would place into a pool that does not accept
    # it. The reason records both facts so the caller can see what happened.
    c = classify({"prompt": "hi"}, {SLA_HEADER: SLA_INTERACTIVE, CAPABILITY_HEADER: CAP_ECONOMY})
    assert (c.sla, c.capability) == (SLA_INTERACTIVE, CAP_STANDARD), c
    assert "explicit-caller-pin" in c.reason and "coalesced" in c.reason, c
    print(f"OK 27 a pin on the unserved cell coalesces too, and the reason records both ({c.reason})")

    # The cells that ARE served must be untouched by coalescing, or it would be a rewrite rather
    # than a rule about one cell.
    for sla_in, cap_in in ((SLA_BATCH, CAP_ECONOMY), (SLA_BATCH, CAP_STANDARD), (SLA_INTERACTIVE, CAP_STANDARD)):
        out_sla, out_cap, note = coalesce(sla_in, cap_in)
        assert (out_sla, out_cap) == (sla_in, cap_in) and note is None, (sla_in, cap_in, note)
    print("OK 28 the three served cells pass through coalescing unchanged")

    # batch+economy is the Neuron cell and it stays batch+economy here. Whether it is ROUTABLE is a
    # separate question the matrix answers with "not yet"; the relay's job is to name the pair.
    c = classify({"prompt": "hi", "max_tokens": 8}, {SLA_HEADER: SLA_BATCH})
    assert (c.sla, c.capability) == (SLA_BATCH, CAP_ECONOMY), c
    print("OK 29 batch+economy is named as itself; its routability is the matrix's question, not the relay's")


def test_guards_may_not_degrade_below_a_physical_constraint() -> None:
    """0b A22 applied to the GUARDS, which are preferences and therefore sit below constraints.

    THE DEFECT THIS ENCODES was live and shipped in an image. The guards were applied with a blanket
    `economy` floor, so an exhausted rate limiter, which is the normal steady state under load, or an
    engaged kill switch would degrade a CONTEXT-BOUND request from standard down to economy and place
    a 4,072-token request into a class with a 2,048-token hard limit. It would then fail at the
    backend, which is precisely the outcome A22 refuses to produce for a caller's pin, reached by a
    different route. Found by running the kill switch against the live relay.
    """
    import os as _os
    import sys as _sys
    _sys.path.insert(0, _os.path.join(_os.path.dirname(__file__), "..", "..", "async-worker"))
    from guards import Guards, KillSwitch, UpgradeRateLimiter  # noqa: E402

    exhausted = Guards(KillSwitch(), UpgradeRateLimiter(rate_per_sec=0.0, burst=0))
    withdrawn = Guards(KillSwitch(engaged=True), UpgradeRateLimiter(rate_per_sec=1e6, burst=1e6))

    # FORCED by physics. Neither guard may move it.
    d = classify(OVER_ECONOMY, {SLA_HEADER: SLA_BATCH})
    assert d.capability == CAP_STANDARD and d.min_feasible == CAP_STANDARD, d
    for label, g in (("exhausted limiter", exhausted), ("engaged kill switch", withdrawn)):
        got = g.decide(d.min_feasible, d.capability)
        assert got == CAP_STANDARD, f"{label} degraded a context-bound request to {got}"
        assert CONTEXT_LIMIT[got] >= d.required_context_tokens, (
            f"{label} placed a {d.required_context_tokens}-token request into {got}, "
            f"which holds {CONTEXT_LIMIT[got]}"
        )
    print("OK 30 a context-bound placement survives an exhausted limiter AND an engaged kill switch")

    # DISCRETIONARY. Both guards may withdraw it, which is what they are for.
    d = classify({"prompt": "hi", "max_tokens": 8, "tools": [{"type": "function"}]}, {SLA_HEADER: SLA_BATCH})
    assert d.capability == CAP_STANDARD and d.min_feasible == CAP_ECONOMY, d
    for label, g in (("exhausted limiter", exhausted), ("engaged kill switch", withdrawn)):
        assert g.decide(d.min_feasible, d.capability) == CAP_ECONOMY, label
    print("OK 31 a discretionary upgrade IS withdrawn by both guards, so they still do their job")

    # The general property, over every non-refused decision: the placement must be able to hold the
    # request even after the harshest guard has acted.
    for body in ({"prompt": "hi"}, OVER_ECONOMY, {"prompt": "hi", "tools": [{}]},
                 {"messages": "not a list"}, {"prompt": "w " * 1200}):
        d = classify(body, {SLA_HEADER: SLA_BATCH})
        if d.refused:
            continue
        after = exhausted.decide(d.min_feasible, d.capability)
        assert CONTEXT_LIMIT[after] >= d.required_context_tokens, (body, d, after)
    print("OK 32 across every servable body, the post-guard class can still hold the request")


def test_estimator_rounds_up() -> None:
    assert estimate_prompt_tokens("") == 0
    assert estimate_prompt_tokens("one two three") == 4
    assert estimate_prompt_tokens("one two") == 3
    print("OK 20 the token estimate rounds UP, pushing its error toward cost, not toward a request")
    print("      the class could not have served")


def test_relay_retries_nothing_including_connect() -> None:
    """Property 6. Where the relay deliberately differs from the async worker."""
    assert RELAY_RETRY_TOTAL == 0
    session, timeout = make_relay_session(630)
    assert timeout == 630
    for scheme in ("http://", "https://"):
        r = session.get_adapter(scheme).max_retries
        assert r.total == 0 and r.read == 0 and r.status == 0, (scheme, r)
        assert r.connect == 0, (
            f"{scheme} relay retries on CONNECT ({r.connect}). The worker may, because a DynamoDB "
            "claim sits below it. The relay has no claim, so a connect retry is a second GPU "
            "generation, billed and served in full."
        )
    print("OK 21 the relay retries nothing: total, connect, read and status all 0 on both schemes")


def main() -> int:
    test_the_two_axes_never_fuse()
    test_constraints_sit_above_preferences()
    test_a_pin_is_honoured_or_refused_never_overridden()
    test_fails_open_and_refusal_is_not_an_exception_to_it()
    test_only_ever_upgrades_on_the_capability_axis()
    test_the_pair_to_pool_matrix_is_obeyed()
    test_guards_may_not_degrade_below_a_physical_constraint()
    test_estimator_rounds_up()
    test_relay_retries_nothing_including_connect()
    print("PASS: two headers never fuse, constraints sit above preferences, a pin is honoured or")
    print("      refused but never overridden, classification fails open, and nothing is retried")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
