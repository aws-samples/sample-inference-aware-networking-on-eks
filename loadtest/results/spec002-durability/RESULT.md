# Durability on Qwen3, 2026-09-25: both headline claims now have Qwen3 evidence

Until today the fact pack's two durability guarantees rested entirely on the Llama series. Both are
now measured on `Qwen/Qwen3-8B`, 4 x NVIDIA L4, through the shipped asynchronous front door.

## 1. Backend-path outage (LT-9 equivalent): PASS

40 jobs in flight, the batch inference path taken down for **900 seconds** (the published fifteen
minutes), then restored.

| Quantity | Value |
|---|---|
| Jobs complete | **40 / 40** |
| Claim attempts | 80 |
| Infrastructure rejections | 40 |
| **Real GPU runs** | **40**, exactly the number submitted |
| Time to drain after restore | 917 s |
| Dead-letter queue, attributable to this run | **0** |

**A correction that matters, because the harness reported the opposite.** `LT9_VERDICT` recorded
`dlq_at_outage_end: 1` and declared the horizon crossed. It was not. Every DLQ message was drained
and inspected: there was exactly one, `diag-2026-09-24#c3691ee4...`, with
`ApproximateReceiveCount: 6` and a DynamoDB row already expired by TTL. It was a **diagnostic job
submitted by hand on 2026-09-24** while testing the submit endpoint, and it predated this run.
**Zero LT-9 messages reached the DLQ.**

The defect is in the harness, and it is worth fixing before the number is quoted again:
`run_lt9.sh` reads DLQ depth **absolutely** rather than as a delta from a pre-run baseline, so any
pre-existing message is attributed to the outage under test. The DLQ was purged after capture and
confirmed empty.

## 2. Worker kill and orphan recovery (LT-6 equivalent): PASS, and the sweeper earns its place

300 jobs, **3 of 6 worker pods force-deleted mid-generation**.

| Quantity | Value | Bound |
|---|---|---|
| Distinct complete | **300** | exactly 300 |
| Total GPU runs (sum of `claim_count`) | 309 | |
| Extra runs beyond N | 9 | re-claims of interrupted jobs |
| **Recovered by the SWEEPER** | **6** | message-less orphans, `requeued_at` set |
| Recovered by SQS redelivery | 3 | message still live |
| Dead-letter queue | 0 | |

**zero lost: PASS, extra runs bounded: PASS, reruns bounded: PASS.**

### Why this is the strongest durability evidence the project has

**The orphan rate is six per 300, not one per 300.** The Llama run surfaced a single message-less
orphan. Qwen3 produced **six**, which makes the sweeper materially more load-bearing than the
published text implies.

**Those six jobs would have been lost.** Their SQS messages were already gone, so nothing else
would ever have re-driven them: not redelivery, not the worker's stale-claim steal path. The
verdict separates the two recovery mechanisms explicitly, 6 by sweeper against 3 by redelivery, so
the sweeper's contribution is measured rather than assumed.

**This is the first time the sweeper has ever been observed doing its job.** It had been failing
`AccessDenied` on every run for an unknown period until its IAM role and Pod Identity were created
on 2026-09-24. `evidence/lt6-sweeper-firing.txt` captures both states minutes apart: the old
`AccessDenied` traceback, and then

    sweeper sweep done grace=120s orphans=6 enqueued=6 cooldown=0 lost_race=0 no_payload=0

The intermediate runs correctly report `orphans=0` while the leases are still live, which is the
guard against stealing from a working worker behaving exactly as designed.

## 3. Scope

**CORRECTED 2026-09-27: only the WORKER-KILL run shortens timers. The outage run does not.**

The worker-kill run uses the LT-6 convention (claim lease 25 s, SQS visibility 60 s, sweeper grace
120 s, restored by an EXIT trap at `run_lt6.sh:40-43`) so a killed job's claim lapses inside the
test window. The ORDERING that the design depends on is preserved; the absolute values are not the
shipped ones. Generation stays well under the shortened lease, so no double run is timer-induced.

The outage run overrides nothing. `run_lt9.sh` has no `set-queue-attributes` call and no lease
override; its only timing knob is `OUTAGE_SECONDS` (`:30`), set to 900 for this run (`lt9.log:4`).
The run order settles the rest: the tags carry epochs, and LT-9 submitted at
`lt9-1790343853` = 2026-09-25T13:44:13Z while LT-6 submitted at `lt6-1790345865` =
2026-09-25T14:17:45Z, 33 minutes later. So the shortening had not happened when the outage ran.

What no record captures is the LIVE queue attribute during the outage run, because `run_lt9.sh`
reads message counts and DLQ depth but never `VisibilityTimeout`. The claim that 900 seconds
matched the visibility timeout therefore rests on the configured chain plus the run order, not on
a value read at the time, and the post is worded that way.
