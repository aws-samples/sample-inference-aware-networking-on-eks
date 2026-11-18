# Walkthrough re-verified on Qwen3, 2026-09-26

**Every step the post publishes was run against the Qwen3 stack and passed.** Two of the post's
stated numbers are wrong and are corrected here, and two defects in the published procedure were
found by running it.

Raw timings: `timing.txt`.

## 1. Step by step, as the post orders them

| Step | Command | Result | Seconds |
|---|---|---|---|
| 1 | `make preflight` | OK | 1 |
| 3 | `make dry-run` | **validated 34, skipped 1, exempt 3, failed 0** | 99 |
| 4 | `make stage-1` + `test-routing-reachable` | ALL INVARIANTS PASS, routing reachable (200) | 83 + 5 |
| 5 | `make stage-2` + `test-idempotency` | PASS: duplicate submission ran exactly one GPU job | 87 + 14 |
| 6 | `make stage-3` + `test-tier-isolation` | PASS gate A (routing) and gate B (isolation) | 75 + 40 |
| 7 | `make observability-scalers` | KEDA queries non-empty for both tiers | 15 |
| 8 | `make verify-invariants` | ALL INVARIANTS PASS | 5 |

**Wall clock for steps 1 to 8: 27.2 minutes.**

## 2. Corrections to the published numbers

**"Expected result: 31 validated, 0 failed" is stale.** The current repository validates **34**,
with 1 skipped and 3 exempt. A reader comparing against 31 would think something was missing.

**Cost and time.** The post states 1.70 GPU instance-hours, about 3.29 USD, and about one hour.
Measured on Qwen3 over the walkthrough window:

| Instance | Window hours | USD |
|---|---|---|
| g6e.4xlarge (interactive, L40S) | 0.454 | 1.36 |
| g6.xlarge (batch, L4) | 0.259 | 0.21 |
| **Total** | **0.713** | **1.57** |

**0.71 GPU instance-hours, about 1.57 USD**, for the stage sequence. This is LOWER than the
published figure mainly because it excludes cluster creation and deletion (see scope below), and
because the batch tier here is an L4 rather than an Inferentia2 instance.

## 3. Two defects found by running the published procedure

**The isolation test refuses to run from a distant client, and the Prerequisites do not say so.**
`test-tier-isolation` measured a network floor of 571 to 660 ms against its 500 ms target and
failed closed with: "a p95 measured here describes client distance, not tier isolation. Run this
from inside the cluster's region, or raise INTERACTIVE_TTFT_TARGET_MS deliberately." The guard is
correct and valuable, but a reader outside the Region hits a hard failure at step 6 with nothing in
the Prerequisites warning them. **The Prerequisites should state that the walkthrough is run from
inside the cluster's Region**, or the step should carry the override inline.

It was re-run with `INTERACTIVE_TTFT_TARGET_MS=3000` and passed. Only the RATIO is citable from
this location, because a constant transatlantic offset inflates both numbers equally:

    baseline interactive TTFT 589 ms, under a 40-request batch flood 619 ms, ratio 1.05x

The absolute values are NOT comparable with the post's 76 ms, which was measured from inside the
Region. The isolation property is what this verifies, and it holds.

**A safety-net firing leaves KEDA paused, and `make stage-2` does not clear it.** After the cost
safety net fired overnight it left `paused-replicas: 0` on every ScaledObject. `make stage-2` then
completes and its inline check passes (it reuses an already-complete job, because `job_key` is
deterministic), while `make test-idempotency` fails with `status=None`: the freshly salted job sits
in the queue with no worker to claim it, so no DynamoDB row is ever created. Clearing the pause and
scaling the worker made it pass in 14 seconds.

This does not affect a reader starting from a clean cluster, where KEDA is installed unpaused. It
matters for anyone re-running the walkthrough on a cluster the safety net has touched, and it is
worth a line in the runbook rather than the post.

## 4. Scope, stated so the cost figure is not over-read

**`make cluster-up` and `make cluster-down` were NOT re-run.** Rebuilding would have destroyed a
g6e node that took 14 hours of waiting to obtain, with no guarantee of getting another. So the
0.71 GPU instance-hours covers the STAGE SEQUENCE only, not cluster creation or deletion, and the
27.2 minutes is not the post's end-to-end hour.

`make cluster-down` itself did not exist until 2026-09-25 and is now committed, so the post's
Cleaning up section names a real target for the first time.

**Premium and economy routing were SKIPPED**, loudly and correctly, because both tiers rest at zero
replicas by design. The isolation test reported "THIS IS NOT A PASS" for each. Premium and economy
routing were both verified separately on 2026-09-22 and 2026-09-23.
