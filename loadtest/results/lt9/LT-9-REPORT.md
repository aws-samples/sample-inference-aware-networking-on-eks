# LT-9: durability across a batch-inference-path outage

AMENDMENT (2026-07-25, post-run): the async-worker latency defect (DEFECT-worker-latency.md, hypothesis
H2, connection churn) was present during this run, which WAS worker-driven. It may have added latency to
the post-recovery drain, but it does NOT compromise the verdict: zero loss, zero DLQ, and exactly-once
depend on the durable claim/release/idempotency logic, not on per-request latency. The 44 infra
rejections here are the deliberate outage 5xx path, not the connection stall. The defect was fixed after
this report was written (reused requests.Session; SQS_BATCH_SIZE default 1); the fix would only make the
drain faster, never change the correctness result.

STALL AUDIT (2026-07-25, loadtest/results/VERIFICATION-A-stall-audit.md): confirmed clean. All 44 infra
rejections are attributable to the induced outage: the claim_count distribution {1:8, 2:20, 3:12} and the
queue-depth timeline are fully explained by the 900s outage plus 900s SQS visibility (12 jobs waited out a
second visibility window post-restore, which is the 922s drain). No post-restore rejections, no cc above
3, no amplification, so no stall signature. Per-increment timestamps are unavailable (worker pods deleted
at teardown), so a single hypothetical post-restore stall cannot be positively excluded from the counters,
but nothing indicates one and correctness is latency-independent.


STATUS: RUN COMPLETE (2026-07-25), PASS. Cluster inference-aware-eks, batch tier on g6.12xlarge (4x L4),
async worker v9 (with the B.1/B.2 fixes), production timeout chain restored (route 600 < HTTP 660 <
lease 750 < SQS visibility 900). This measures async's headline contribution (durable, exactly-once
execution) under a sustained outage of the downstream inference path, and it is the regression test for
defect B.1.

## Design (as run)

1/ Submitted K = 40 jobs through the submit API (via the async worker path).
2/ Took the batch Endpoint Picker down for T = 900s (15 min) by scaling vllm-llama3-batch-epp to 0. With
   FailClose, every attempt during the outage returns a gateway 5xx (an infra rejection), the worker
   releases the claim and leaves the message untouched (B.1), and the message keeps the queue's 900s
   visibility. 900s is well beyond the PRE-fix dead-letter horizon (~8-10 min, when the old 120s
   visibility-shortening drove messages to the DLQ), so surviving it proves the fix rather than avoiding
   the window.
3/ Restored the Endpoint Picker; measured drain to completion.

A sampler recorded queue depth (visible, in-flight, DLQ) every 20s to queue_depth.csv.

## Verdict (LT-9-verdict.json)

| Criterion | Result |
|---|---|
| Zero job loss (all K terminal, all complete) | PASS (40/40 complete, statuses = {complete: 40}) |
| Zero dead-letter messages during and after | PASS (DLQ 0 at outage end, 0 after drain) |
| Exactly-once preserved | PASS: real GPU runs = claim_count 84 - infra_rejections 44 = 40 = K |
| No job marked failed by the outage | PASS (no failed status) |
| Clean drain on recovery | PASS (40/40), time-to-drain 922s |
| Sweeper behaviour | no orphan re-enqueues (messages stayed in SQS during the outage, so no orphan condition); no queue-depth amplification |

## What this proves

1/ B.1 regression: a 15-minute batch-inference outage produced ZERO dead-letter messages. Pre-fix, the
   120s visibility-shortening would have burned these to the DLQ in ~8-10 minutes. The horizon is now >
   900s (in fact ~maxReceiveCount x visibility ~= 75 min).
2/ B.2 accounting, live: claim_count (84) over-counts real GPU runs by exactly the number of infra
   rejections (44). real_gpu_runs = 84 - 44 = 40 = K. Quoting claim_count alone would have over-reported
   GPU runs by 110 percent here; the infra_rejections counter corrects it exactly.
3/ Exactly-once holds through the outage: 40 distinct jobs, 40 real GPU runs, no duplicates, no losses.

## The durability envelope

"Zero loss for outages up to at least 15 minutes; the dead-letter horizon is ~75 minutes
(maxReceiveCount 5 x visibility 900s), beyond which work lands in the DLQ where redrive recovers it, not
lost." Observed horizon in this run: > 900s (no DLQ within the outage).

Recovery-latency dimension (honest characteristic, not a failure): drain took 922s after the Endpoint
Picker was restored. This is the SQS visibility window, not slowness: a message last received near the
end of the outage stays invisible for up to 900s before it redelivers and completes. So recovery lags
the fix by up to one visibility window. An operator who wants faster recovery can lower the queue
visibility (at the cost of a shorter dead-letter horizon): the two trade off directly.

Queue-depth series (queue_depth.csv): in-flight held steady at ~32 of 40 throughout the outage with NO
amplification (the sweeper did not pile on re-enqueues), visible stayed 0, DLQ stayed 0, and all drained
to 0 after recovery. The sweeper-amplification concern (gate 2.1) did not materialize: the sweeper found
no orphans because the messages were never lost from SQS.

## Verdict (one sentence)

The async front door survived a 15-minute inference-path outage with zero loss, zero dead-letter
messages, and exactly-once execution (40 real GPU runs from 84 claims once the 44 infra rejections are
subtracted), confirming the B.1 fix and the B.2 accounting live; recovery lags by up to one SQS
visibility window, which is the tunable durability-versus-recovery trade-off.
