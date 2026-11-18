# Verification A: did the worker-stall defect contaminate LT-8 or LT-9? (2026-07-25)

Verdict in one line: NO. LT-8 and LT-9 are both clean; nothing needs republishing.

## Repository fact that reshapes the check

LT-8 was driven DIRECTLY through the gateway (`loadtest/lt8_direct.py`), NOT through the async worker.
Direct-driven requests never touch DynamoDB, so there is no `claim_count` and no `infra_rejections` for
LT-8 to inspect. The worker-stall mechanism (stall to 660s HTTP timeout, InfraRejection, release,
re-claim) therefore cannot apply to LT-8. The relevant contamination signal for a direct-driven cell is
a failed request (urllib timeout at 120s) or an inflated latency. LT-9 was worker-driven and is checked
the intended way.

## LT-8 (direct-driven), per cell and repetition

| Cell (arm/admission) | requests_sent | requests_ok | N start/end | p50 s | p90 s |
|---|---|---|---|---|---|
| A r1 (epp/grouped)      | 624 | 624 | 4/4 | 7.38 | 7.62 |
| A r2                    | 624 | 624 | 4/4 | 7.36 | 7.64 |
| B r1 (epp/interleaved)  | 624 | 624 | 4/4 | 12.39 | 14.46 |
| B r2                    | 624 | 624 | 4/4 | 12.40 | 14.52 |
| C r1 (rr/grouped)       | 624 | 624 | 4/4 | 6.84 | 7.31 |
| C r2                    | 624 | 624 | 4/4 | 6.81 | 7.35 |
| D r1 (rr/interleaved)   | 624 | 624 | 4/4 | 13.30 | 15.31 |
| D r2                    | 624 | 624 | 4/4 | 12.76 | 15.65 |

1/ `infra_rejections` and `claim_count > 1`: not applicable and not present (direct driver writes no
   DynamoDB records). CONFIRMED: the worker-stall mechanism could not have acted on LT-8.
2/ The `infra_rejections == 0` validity gate (designed in `lt8_cell.py`, the worker-driven driver that
   was NOT used) was therefore NOT evaluated. Stated honestly per "a gate never evaluated is not a
   passed gate." The applicable direct-driver gate, `requests_ok == requests_sent`, was met in all 8
   cells (624/624), and N was stable at 4 at the start and end of every cell.
3/ Latency: every request completed (0 timeouts at the 120s driver ceiling), and p90 is tight and
   reproducible across repetitions (A 7.62/7.64, B 14.46/14.52, C 7.31/7.35, D 15.31/15.65). Random
   stalls would spread the tail and diverge between reps; that is absent. LIMITATION [Data needed]: the
   direct driver saved only p50/p90, not the raw samples or max, so a single hidden sub-120s slow
   completion cannot be excluded from the artifact alone. It would not affect the hit-rate result (hit
   rate is from the batch tier cache counters, independent of client latency), and the 100 percent ok
   rate plus cross-rep reproducibility make it very unlikely.

Verdict LT-8: CLEAN. The reported hit rates and the substitutes-versus-complements finding stand
unchanged. The latency figures are labelled per-request end-to-end from the driver and are not derived
from worker records.

## LT-9 (worker-driven)

Counts: 40 jobs, all complete; sum(claim_count) 84; sum(infra_rejections) 44; real GPU runs 84 - 44 =
40 = K. claim_count distribution {1: 8, 2: 20, 3: 12}; infra_rejections per job {0: 8, 1: 20, 2: 12}.

Attribution of the 44 rejections to the induced outage (from `queue_depth.csv`, 24s sampling):
1/ 8 jobs completed BEFORE the Endpoint Picker finished scaling to zero (cc=1, 0 rejections).
2/ 20 jobs were received once during the outage (EPP down, gateway 5xx), rejected once, then redelivered
   after restore and completed (cc=2, 1 rejection).
3/ 12 jobs were received at submit, redelivered once more INSIDE the 900s outage (2 rejections), then
   had to wait out a second 900s SQS visibility window after restore before redelivering and completing
   (cc=3, 2 rejections). In-flight held flat at 12 from epoch 1785002069 to 1785002888, which is exactly
   this second visibility window, then dropped to 0.

All 44 rejections occurred inside the outage window (before restore near epoch 1785002004). There are no
post-restore rejections, no `claim_count` above 3 (2 outage rejections is the maximum a 900s outage with
900s visibility can produce), and no queue-depth amplification. A drain-time stall would have added a
rejection beyond the outage (a cc=4 job) or extended the drain past one visibility window; neither is
present. The 922s drain equals one visibility window, the expected recovery lag.

LIMITATION [Data needed]: `infra_rejections` is a durable counter without per-increment timestamps, and
the worker pods were deleted at teardown, so a single hypothetical post-restore stall cannot be
positively excluded from the counters alone. The distribution and the queue-depth timeline show no stall
signature, and correctness (zero loss, exactly-once) is independent of latency regardless.

Verdict LT-9: CLEAN. The 44 rejections are the induced outage. Zero loss, zero DLQ, and exactly-once
stand unchanged.

## Part B audit: is any published figure derived from the empty `submitted_at`?

Clean. The live submit-api was image v8, so `submitted_at` was empty in the LT-8/LT-9 records, but no
published figure depends on it:
1/ B.1: no figure labelled end-to-end, total, or queue wait is derived from `submitted_at`. LT-8 latency
   is measured client-side in `loadtest/lt8_direct.py` (time.time() around the request), a legitimate
   per-request round-trip through the gateway (the synchronous direct path has no queue), correctly a
   client end-to-end, not an async submit-to-complete. No relabel needed.
2/ B.2: P8 ("traceable from submission through the pipeline") is supported by its evidence file
   `loadtest/results/phased_trace.md`, whose `submitted` column is populated (e.g. 1784703298). That
   proof run stamped submission timestamps, so P8 holds as worded. The later LT-8/LT-9 runs had an empty
   `submitted_at` because the live submit-api was v8; that does not unsupport P8 (its proof is the
   phased_trace run), and P8 makes no claim about the LT-9 run.
3/ B.3: LT-9 drain time is `DRAIN_END - DRAIN_START` from `date +%s` (run_lt9.sh lines 69, 90, 122), wall
   clock, independent of `submitted_at`.

The only consequence of the deployment gap is that the Part A queue-wait interval could not be measured
from records (noted in DEFECT-worker-latency.md). The pre-run image check (B.4) prevents a recurrence.

## Overall

No stalls contaminated either result. No amendment to any figure is required. The dated amendment notes
already added to LT-8-REPORT.md and LT-9-REPORT.md (about the defect being present and worked around) are
confirmed accurate by this audit; the per-cell numbers above are recorded so a reader who now asks can
see them.
