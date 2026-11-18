# Pre-registration: does a client disconnect free the vLLM slot

Written 2026-08-02, BEFORE any cell ran, per the implementation authorization ruling 1 and the
project's standing pre-registration rule. Nothing below is edited after execution; results land
in `RESULT.md` beside this file.

## Why this experiment exists

Design v6.1 section 5.3 forbids implementing D-1's "abandon the in-flight attempt" by closing the
client connection, on a rationale inherited from vLLM issue 24584. Phase 0 found source evidence
that contradicts that rationale for the pinned build, and the supervisor authorized measuring the
delivered behaviour rather than ruling from code alone.

Phase 0 source findings this experiment tests (all at commit `ee0da84`, upstream vLLM v0.24.0,
which the pinned Deep Learning Container ships):

1. `create_chat_completion` is decorated `@with_cancellation`
   (`vllm/entrypoints/openai/chat_completion/api_router.py:51`).
2. `AsyncLLM.generate()` catches `asyncio.CancelledError` and `GeneratorExit` and calls
   `await self.abort(...)` (`vllm/v1/engine/async_llm.py:588-595`).
3. `abort()` calls `await self.engine_core.abort_requests_async(...)`
   (`vllm/v1/engine/async_llm.py:709-718`), so it reaches EngineCore rather than performing
   client-side stream cleanup only.
4. `POST /abort_requests` is NOT reachable on this serving path: it is registered only under
   `if getattr(app.state.args, "tokens_only", False)`
   (`vllm/entrypoints/serve/disagg/api_router.py:80-82`), and `--tokens-only` "only enable[s] the
   Tokens In<>Out endpoint" (`vllm/entrypoints/openai/cli_args.py:154-158`), so it is mutually
   exclusive with the OpenAI serving endpoints this design routes to.

Source says cancellation should reach the engine. This measures whether it does, end to end,
through the real network path.

## Hypothesis

Closing the client connection mid-generation frees the vLLM sequence slot promptly, because the
disconnect propagates as an ASGI `http.disconnect`, cancels the handler task, closes the async
generator, and reaches `engine_core.abort_requests_async`.

## Success criteria, fixed in advance by the authorization

1. PASS: after client disconnect, `vllm:num_requests_running` returns to its pre-request value
   within **5 seconds**, on BOTH a non-streaming and a streaming `/v1/chat/completions` request,
   through the full path the relay will use (external entry, Envoy Gateway, Endpoint Picker,
   vLLM). Each case run **twice**.
2. FAIL: either case exceeds the bound in either repetition, or the slot frees only when the
   generation would have completed anyway.
3. `vllm:num_preemptions_total` is recorded alongside every observation, so a preemption is not
   mistaken for a cancellation.
4. The measured free-time distribution is reported, not just the pass or fail, because the
   measured bound becomes a constant in the INV13 canary.

## Predictions, committed before execution

| # | Prediction | Confidence | What would refute it |
|---|---|---|---|
| P1 | Non-streaming disconnect frees the slot within 5 s, both reps | high | slot held until generation end |
| P2 | Streaming disconnect frees the slot within 5 s, both reps | medium | `with_cancellation` hands off to `StreamingResponse` for streams, a different code path than the one read in Phase 0; if the handoff loses the disconnect, this is where it shows |
| P3 | Median free time is under 1 s, dominated by the Prometheus scrape interval rather than by vLLM | medium | a multi-second tail would indicate buffering somewhere in the path |
| P4 | `vllm:num_preemptions_total` stays flat at its pre-run value throughout | high | any increment means the slot moved for a reason other than cancellation, invalidating that observation |
| P5 | Envoy propagates the downstream reset upstream without configuration | medium | slot held while the gateway keeps the upstream stream open, which would make this an Envoy question rather than a vLLM one |

P2 is the one genuinely in doubt, and it is the one that matters most: promotion at deadline P
happens tens of seconds in, by which time an interactive request is streaming.

## Observables

- `vllm:num_requests_running` sampled from the pod's `:8000/metrics` at approximately 250 ms,
  read directly from the pod rather than through Prometheus, so resolution is not limited by the
  15 s scrape interval.
- `vllm:num_preemptions_total`, same source, recorded at t0 and at slot-free.
- Wall-clock delta from socket close to the first sample where running returns to baseline.

## Method

1. Extend the `spike-safety-net` ConfigMap deadline before provisioning.
2. Scale the interactive tier to exactly one replica, on the pinned `g6e` NodePool.
3. Baseline `num_requests_running` and `num_preemptions_total`.
4. Issue a long generation (`max_tokens` large enough that it cannot finish inside the bound)
   through the external entry, confirm running increments, then close the socket abruptly.
5. Sample until running returns to baseline or 60 s elapses, whichever first.
6. Repeat for streaming and non-streaming, twice each, in a fixed order.
7. Scale back to zero, verify zero GPU instances through the EC2 API, not kubectl alone.

## Result transfer, recorded now so it is not argued later

The segment under test is Envoy Gateway to vLLM disconnect propagation. It is invariant across
the planned teardown and rebuild because every version pin is frozen through Phase 3. The
post-rebuild topology adds the relay in front, but the relay-to-Envoy hop is our own client under
our own control, and the continuous guarantee for the post-relay world is INV13, the canary probe
specified in A15. Experiment now, canary forever: the two compose, and this result does not go
stale at the rebuild.

## What this does not establish

Behaviour at any other vLLM version. Both the abort route's absence and the cancellation path are
version-specific, which is why the pins are frozen and why any future vLLM move re-runs this.
