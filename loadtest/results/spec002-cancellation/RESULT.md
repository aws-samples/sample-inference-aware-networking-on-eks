# Disconnect cancellation on Qwen3, 2026-09-25: MEASURED, and above the sampling floor

**Verdict: closing the client connection frees the sequence slot in 0.10 to 0.16 s, 4 of 4 cells,
baseline restored every time.** This replaces a Qwen3 number that was resolution-limited and could
not support the claim the post makes.

## 1. Why a new probe was needed

`tests/smoke/probe_cancellation.sh` polls the metrics endpoint through a `kubectl exec` round trip,
about **2.6 s per sample**. Its own header records that the warn tier is inoperative for exactly
this reason. Run against Qwen3 on 2026-09-24 it returned 2,850 ms, which means only "freed within
one sample" and cannot be compared with a sub-second figure.

`incluster_cancel_probe.py` runs INSIDE the serving pod and polls locally, so the floor is the poll
interval (**20 ms**) rather than an API round trip.

## 2. Method

Per cell: confirm `vllm:num_requests_running` is at baseline, open a raw socket, start a 4,000-token
streaming generation with `ignore_eos`, wait until the engine reports it running, generate for 3 s,
then **abruptly sever the socket** (SO_LINGER zero, so a RST rather than a graceful close), and poll
until the gauge returns to baseline. No abort endpoint is used: it is gated behind a flag at vLLM
0.24.0, which is the point the post makes.

## 3. Result

| Cell | busy before cut | disconnect to slot free | baseline restored |
|---|---|---|---|
| 1 | 1.0 | **0.102 s** | yes |
| 2 | 1.0 | **0.101 s** | yes |
| 3 | 1.0 | **0.104 s** | yes |
| 4 | 1.0 | **0.159 s** | yes |

**n = 4, min 0.101, max 0.159, mean 0.116 s.** A second run is recorded in
`evidence/cancel-probe-run2.json` as a replication.

**The effect is about six times the sampling floor**, so this is a measurement rather than an upper
bound imposed by the instrument. That is the difference from the 2,850 ms reading.

## 4. How this may and may not be compared with the published number

The post cites **0.55 to 0.86 s across 4 of 4 cells** for Llama-3.1-8B, measured 2026-08-03.

**The mechanism claim transfers and is now measured on Qwen3**: connection close propagates to the
engine core and frees the slot, which is what decision D-1 rests on, since the relay abandons an
in-flight generation at the promotion deadline by closing the connection.

**The two numbers must NOT be presented as a model-to-model speedup.** They were taken with
different instruments and this one is finer. The honest statement is that Qwen3 frees the slot in
about 0.12 s measured at 20 ms resolution, and that the earlier Llama figure is a different
measurement whose instrument is not recorded here. Attributing the difference to the model would be
a mechanism story fitted to two samples taken different ways.

## 5. Scope, unchanged from the original

Concurrency 1, single replica. This is a FLOOR, not a bound under load: concurrency lowers the
per-request rate and the queue-side observable that would settle the behaviour under load is
specified and still not measured.
