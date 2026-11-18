# Result: client disconnect DOES free the vLLM slot. PASS, 4 of 4 cells.

Run 2026-08-02 on the parked stack (ruling 1), against the pre-registration in this directory,
which was committed before any cell ran and is unedited. Raw data: `RESULT.json`. Driver:
`loadtest/cancellation_probe.py`.

## Verdict

**PASS.** 4 of 4 cells freed the sequence slot within the pre-registered 5 second bound, on both
the streaming and the non-streaming path, twice each, through the full external path.

**Reject criterion 1 DOES NOT FIRE.** Promotion is viable. The mechanism is connection close, not
`POST /abort_requests`, which Phase 0 established is unreachable on this serving path.

## Measured free-time distribution, as required

| Cell | Path | Free time (s) | Within 5 s | Preemptions moved |
|---|---|---|---|---|
| 1 | non-streaming rep 1 | 0.806 | yes | no |
| 2 | non-streaming rep 2 | 0.859 | yes | no |
| 3 | streaming rep 1 | 0.800 | yes | no |
| 4 | streaming rep 2 | 0.810 | yes | no |

Min 0.800 s, max 0.859 s, spread 0.059 s across all four. `vllm:num_requests_running` went 0 to 1
on every cell and returned to 0; `vllm:num_preemptions_total` stayed at 0.0 throughout, so no
observation is a preemption misread as a cancellation (P4).

**Measurement resolution, stated rather than glossed.** Samples are taken every 250 ms, so a
reported free time of t means the true value lies in (t minus 0.25, t]. The true free time is
therefore roughly 0.55 s to 0.86 s. The bound being tested is 5 s, so the margin is about six
fold and the quantization does not affect the verdict. It does mean the sub-second figures should
not be quoted to three decimals as if they were precise.

## Predictions versus outcome

| # | Prediction | Outcome |
|---|---|---|
| P1 | non-streaming frees within 5 s, both reps | CONFIRMED, 0.806 and 0.859 |
| P2 | streaming frees within 5 s, both reps | CONFIRMED, 0.800 and 0.810 |
| P3 | median free time under 1 s | CONFIRMED, median about 0.808 |
| P4 | preemptions stay flat | CONFIRMED, 0.0 throughout |
| P5 | Envoy propagates the downstream reset upstream with no configuration | CONFIRMED by consequence: the slot could not have freed otherwise, and no Envoy change was made |

P2 was the prediction held at only medium confidence, because `with_cancellation` hands off to
`StreamingResponse` for streams, a different code path from the one Phase 0 read. It holds. In
both streaming cells the first response byte arrived 0.27 s and 0.37 s BEFORE the disconnect
(`first_byte_at_offset_s` is negative by construction), which confirms the stream had genuinely
started on the wire and the cells are not accidentally testing the pre-first-token case. That
matters, because promotion at deadline P always fires while streaming.

## The finding worth carrying

Streaming and non-streaming free times are indistinguishable (0.800 to 0.859 s across all four
cells, spread 0.059 s). Had the two paths differed materially, that would have suggested
path-specific handling and a fragile mechanism. They do not, which is consistent with both paths
converging on the same `GeneratorExit` branch in `AsyncLLM.generate()` that Phase 0 read at
`vllm/v1/engine/async_llm.py:588-595`. Source prediction and delivered behaviour agree.

## Result transfer, per the authorization

The segment under test is Envoy Gateway to vLLM disconnect propagation. It is invariant across
the planned teardown and rebuild, because every version pin is frozen through Phase 3. The
post-rebuild topology adds the relay in front, but the relay-to-Envoy hop is our own client under
our own control, and the continuous guarantee for the post-relay world is INV13. Experiment now,
canary forever: the two compose, and this result does not go stale at the rebuild.

## Recommended INV13 constant

Measured worst case 0.859 s with 250 ms quantization. Proposed: INV13 asserts the slot frees
within **5 s** (the pre-registered bound, about six times the measured worst case, so it will not
flap), and additionally warns above **2 s**, which is more than double the measured worst case and
so is an early signal of propagation degrading before the hard bound is breached. A15 fixes the
final values; these are the measured basis for them.

## What this does NOT establish

1. Behaviour at any other vLLM version. Both the abort route's absence and the cancellation path
   are version-specific. Any future vLLM pin move re-runs this experiment; that cost is named.
2. Behaviour under concurrency. Every cell ran against a single replica with one in-flight
   request. Whether disconnect propagation stays sub-second when the engine is saturated and the
   scheduler is under pressure is untested. INV13 as a standing probe is what covers this in
   production, and it is the reason the canary is required rather than optional.
3. Behaviour through the relay, which does not exist yet. The relay-to-Envoy hop is new surface.

## Cost and teardown

One `g6e.xlarge`, provisioned and released inside a single session, well under the authorized one
GPU-hour. Teardown verified through the EC2 API rather than kubectl alone: zero nodeclaims, zero
`g6e` instances, only the two `m6i.xlarge` system nodes running. The `spike-safety-net` deadline
was extended to +3 h before provisioning and restored to its prior expired value (1785077633)
afterwards.
