# Qwen3 queue path (LT-10 equivalent), 2026-09-24: PASS, sign matches the direct path

**Verdict: the shipped asynchronous front door reproduces the direct-path effect on Qwen3-8B, and
the exactly-once arithmetic is clean in all four cells. 4 of 4 cells, zero failures.**

Until this run **the Qwen3 series had no queue-path measurement of any kind**: `async-worker` sat at
zero replicas throughout, and every queue claim in the writeup rested on the Llama series alone.

Runner: `loadtest/run_lt10_qwen3.sh`. Cells: `run-2026-09-24/`.

## 1. Substrate and operating point

Anchored to experiment 4's committed direct-path cells at **rho = 1.0**, the middle of the ramp.
`P = 52`, `JOBS = 624`, `N = 4` are identical to both the Llama LT-8/LT-10 pair and the Qwen3 ramp;
only `PREFIX_REPS = 66` differs, because it is the one term that depends on C.

| Item | Value |
|---|---|
| Model | `Qwen/Qwen3-8B`, asserted live against the served model by gate 0 |
| Tier | batch, 4 x NVIDIA L4, **C = 24,064 asserted by gate 1** |
| Workers | 8 `async-worker` replicas, `SQS_BATCH_SIZE=1`, on system nodes (no accelerator) |
| Path | submit API -> Amazon SQS -> conditional claim in DynamoDB -> worker -> gateway -> Endpoint Picker |

## 2. Results

| Cell | Mode | Hit rate | e2e p50 | e2e p90 |
|---|---|---|---|---|
| Aq rep 1 | grouped | 0.9006 | 5 s | 9 s |
| Aq rep 2 | grouped | 0.9038 | 5 s | 9 s |
| Bq rep 1 | interleaved | 0.5517 | 183 s | 321 s |
| Bq rep 2 | interleaved | 0.5592 | 183 s | 322 s |
| **means** | | **grouped 0.9022, interleaved 0.5554** | | |

## 3. The two findings

**Finding 1, the sign transfers: the queue path agrees with the direct path.** Grouped beats
interleaved through the full asynchronous front door, as it does when the gateway is driven
directly. LT-10's framing is honoured exactly: the claim is the SIGN, not the magnitude, and the
magnitude does differ as that framing predicted (interleaved reads 0.5554 through the queue against
0.1116 direct, because drain-detected grouping is stricter and queue pacing changes what is resident
when). The grouped arm is the tighter comparison and it is very close: **0.9022 through the queue
against 0.8938 driven directly, a difference of 0.008**, which mirrors the Llama series' 0.9133
against 0.9135.

**Finding 2, the latency effect reproduces on a second model family, and this is the number the
writeup did not have.** Under interleaved cache thrash the queue path's end-to-end latency is
**183 s at p50 and 321 s at p90**, against **5 s and 9 s** grouped: a factor of **36.6x at p50 and
35.7x at p90**. The Llama series measured "about 440 seconds to about 7 seconds, roughly sixty
times". Same mechanism, same direction, a different constant on a different model, which is exactly
what the comparability rule says to expect and what makes the mechanism claim stronger than the
number. Most of that latency is queue wait rather than generation, which is only visible from the
queue path: the direct driver cannot see it, and experiment 4 did not.

## 4. Exactly-once, measured in every cell

| Cell | claim_count sum | infra rejections | real GPU runs | DLQ depth | statuses | N start / end |
|---|---|---|---|---|---|---|
| all four | **624** | **0** | **624** | **0** | `{complete: 624}` | 4 / 4 |

624 jobs submitted, 624 claims, 624 GPU runs, nothing in the dead-letter queue, every job terminal
and complete, in each of four independent cells. This is the safety property carried by the DynamoDB
conditional claim, now measured on Qwen3 rather than inherited from the Llama series.

**Scope, stated plainly.** This is the uninterrupted path. No worker was killed and no backend
outage was induced, so it says nothing about orphan recovery, which is the sweeper's case and which
remains untested on Qwen3.

## 5. Comparability

New measurement series. The Llama constants (0.9133/0.9135, 440 s to 7 s, sixty times) are not
continued by these numbers; what transfers is the mechanism and the sign.
