# LT-8: admission control and cache-aware routing, substitutes or complements?

AMENDMENT (2026-07-25, post-run): the async-worker latency defect (DEFECT-worker-latency.md, hypothesis
H2, connection churn) was present during this run. It does NOT change any number here, because LT-8 was
driven directly through the gateway (not the worker) precisely for that reason; the direct path was
verified reliable (54/54 probes). The defect was diagnosed and fixed after this report was written; the
fix (a reused requests.Session and SQS_BATCH_SIZE default 1) does not touch the direct driver.

STALL AUDIT (2026-07-25, loadtest/results/VERIFICATION-A-stall-audit.md): confirmed clean. All 8 cells
requests_ok = requests_sent = 624, N stable 4/4, p90 tight and reproducible across reps. No worker
records exist for LT-8 (direct-driven), so the worker-stall mechanism could not act; the
infra_rejections gate was not applicable and was not evaluated (the requests_ok gate was met). Hit-rate
results are unaffected. Latency is per-request end-to-end from the driver, not derived from worker
records.


AMENDMENT (2026-07-28, LT-11 cross-reference): LT-11 (`loadtest/results/lt11/LT-11-REPORT.md`) swept the
overflow ratio rho and places this run in context. Three consequences for how LT-8 should be read, none
of which change any number measured here:
1/ This run's rho = 2.54 is now identified as the COLLAPSE ANCHOR of the LT-11 ramp: the one point where
   both interleaved cells are floored at zero. That is why the interaction test at this point is
   degenerate (with B = D = 0, `(A-B) > (C-D)` reduces to `A > C`), and the "COMPLEMENTS" sign reported
   here should not be quoted as a general result. LT-11 measures the sign at four non-degenerate rho
   points and finds it FLIPS (substitutes at rho 0.80, complements at 1.00 and above), with the crossover
   at the cache boundary.
2/ This run's grouped cells are independently reproduced. LT-11 measured C (round-robin grouped) at
   0.6625 to 0.6668 across five rho points against this report's 0.6643, and A (Endpoint Picker grouped)
   at 0.9037 to 0.9135 against this report's 0.9135. The routing gain A - C is rho-invariant at 0.2379 to
   0.2492, so the headline of this run generalizes.
3/ LT-11's first pass (2026-07-26) is a cautionary note that applies to any future re-run of THIS
   experiment: the round-robin route and Service (`loadtest/lt8-batch-route.yaml`,
   `loadtest/lt8-batch-rr-service.yaml`) are labelled delete-at-teardown and are NOT applied by the LT-8
   scripts, so if they are missing the `x-pub-arm: rr` header silently falls through to the Endpoint
   Picker and the round-robin arm measures the Endpoint Picker instead. LT-11's first pass hit exactly
   that. A preflight gate (round-robin grouped must spread, max concentration below 0.5, hit near 0.667)
   now guards it in `run_lt11.sh` and `run_lt11_rr_rerun.sh`. LT-8's own data is unaffected: its C cell
   measured 0.6643 at uniform 0.25, which is only reachable with the route present.

STATUS: RUN COMPLETE (2026-07-25). Cluster inference-aware-eks, batch tier pinned to one g6.12xlarge
(4x NVIDIA L4 24GB, all 4 replicas on one node, uniform cache). This report states the interaction
sign AS MEASURED; the direction was not pre-committed (standard 4). A null or unexpected sign here does
not touch async's durability contribution, which LT-3, LT-6, and LT-9 measure independently.

## Methods note: why the pattern was driven directly, and how the shipped path is confirmed (2026-07-26)

LT-8 drove the arrival pattern directly at the gateway (`lt8_direct.py`), NOT through the async front
door. This was a deliberate methodological choice, not an omission: driving directly removes
queue-induced variance (worker poll timing, instantaneous concurrency, drain pacing) and isolates the
router's response to the admission pattern, which is exactly what the substitutes-versus-complements
question asks. The result and the choice both stand.

The consequence is that LT-8 shows the effect exists and quantifies it cleanly, but does not by itself
show that OUR system (submit API, Amazon SQS, worker, gateway, Endpoint Picker) produces that pattern
end to end. That link is closed by LT-10 (`loadtest/results/lt10/LT-10-REPORT.md`), which reruns the
same operating point through the full shipped path and confirms the shipped system reproduces the
effect in the same direction. LT-10's magnitude is expected to differ (queue pacing changes
instantaneous concurrency); its job is to match the SIGN, not the magnitude. See LT-10 for that result.

## How it was driven (deviation, stated plainly)

LT-8 was driven DIRECTLY through the gateway (loadtest/lt8_direct.py, loadtest/run_lt8_direct.sh),
with the same x-pub-arm mechanism as the publication arms (epp -> InferencePool + Endpoint Picker; rr
-> plain Service). It was NOT driven through the async worker. Reason: the async worker showed
intermittent added latency under sustained concurrent load (some requests stalled and were recovered
only after the SQS visibility window, slowing throughput). That is a real finding about the worker
under load, logged for follow-up, but it is orthogonal to LT-8's question. LT-8 asks about admission
COMPOSITION (what set of prefixes is in flight at once), which the direct driver controls precisely by
send order. The queue as the operational control surface (durable, exactly-once) is exercised and
validated in LT-9. The gateway path itself was verified reliable (54/54 direct probes across EPP and RR
arms, ~1s each), so the direct measurement is clean.

## Regime (from the live read, cache_derivation_batch.md)

- num_gpu_blocks = 1921 (uniform on all 4 L4 replicas), block_size 16 => C = 30,736 tokens per replica.
- P = 52 prefixes, prefix length L = 5,999 tokens (measured: 28 tokens/rep x 214 + 7), R = 12, JOBS = 624/cell.
- Per-replica EPP-interleaved working set (P/N)L = 77,987 = 2.54 x C (overflows). Aggregate P.L = 10.2 x C.
- Cold cache per cell: unique salted prefixes (no restart); cell A also on freshly loaded replicas.

## The 2x2 (measured, 2 clean reps each, both reps identical to 4 dp)

| Cell | Router | Admission | Hit rate | Predicted | p50 s | p90 s | co-location (max replica share) |
|---|---|---|---|---|---|---|---|
| A | Endpoint Picker | GROUPED | **0.9135** | 0.917 (1 - 1/R) | 7.4 | 7.6 | 0.35 (uneven: each prefix on one replica) |
| B | Endpoint Picker | INTERLEAVED | **0.0000** | overflow, < A | 12.4 | 14.5 | 0.26 (uniform: no co-location) |
| C | round-robin | GROUPED | **0.6643** | 0.667 (1 - N/R) | 6.8 | 7.3 | 0.25 (uniform) |
| D | round-robin | INTERLEAVED | **0.0000** | collapse | 13.0 | 15.3 | 0.26 (uniform) |

Requests: 624 sent and 624 counted per cell, all 8 cells (per-replica shares sum exactly). N = 4 stable
at start and end of every cell (single g6.12xlarge, no scale events; KEDA suspended).

## Cold-miss-floor anchor (direction-free)

Cell A reached hit rate 0.9135, essentially the cold-miss floor 1 - 1/R = 0.917 (each prefix cold once,
co-located on one replica). Cell B did NOT tie A: it fell to 0 (the per-replica set overflows at 2.54 x
C and, because a prefix's requests are maximally separated under interleaving, every request is a cold
miss). So A is the unique floor here, as the anchor requires.

## Interaction (sign as measured)

- (A - B) grouping benefit under the Endpoint Picker = 0.9135 - 0.0000 = +0.9135
- (C - D) grouping benefit under round-robin        = 0.6643 - 0.0000 = +0.6643
- (A - B) > (C - D): the measured sign reads as COMPLEMENTS.

Interpretation, stated honestly. Both INTERLEAVED cells saturated at exactly 0 (complete thrash: the
working set overflows and requests do not reuse before eviction; the Endpoint Picker cannot co-locate
under interleaving, so B behaves like D). Because B = D = 0, the interaction reduces to A versus C: under
GROUPED admission the Endpoint Picker beats round-robin 0.914 vs 0.664, because grouping shrinks the
in-flight set to one prefix (which fits, 6k < 30.7k) and the router then co-locates it on one replica (1
cold miss) while round-robin scatters it across all four (N cold misses). The reading is therefore:
grouping is the ENABLING condition, and cache-aware routing extracts an additional ~0.25 hit rate on
top of it; without grouping, neither router helps. That is a complements relationship at this operating
point, and it is the OPPOSITE of the substitutes intuition the model leaned toward, which is exactly why
the direction was not pre-committed.

SCOPE / regime-dependence (a real limitation): the sign depends on how hard the interleaved cells
overflow. Here the overflow was severe (2.54 x C per replica) so both interleaved cells hit 0, which
maximizes (A - B). At a MILDER overflow (per-replica working set closer to 1 to 1.5 x C), B and D would
retain partial hits, D would gain more from grouping than B (round-robin has more to recover), and the
sign could flip to substitutes. Mapping that curve needs additional operating points (not run here).
The single measured point is COMPLEMENTS.

## Predicted vs measured

| Cell | Predicted | Measured | Note |
|---|---|---|---|
| A | 0.917 | 0.9135 | matches the cold-start model (out-of-sample, a regime it was not fitted on) |
| B | < A, ~0.6-0.85 guessed | 0.000 | more extreme than guessed: total thrash, not partial eviction |
| C | 0.667 | 0.6643 | matches 1 - N/R almost exactly |
| D | ~0.1-0.4 guessed | 0.000 | total thrash |

The grouped cells validate the closed-form model out of sample to within 0.004. The interleaved cells
went fully to 0 rather than the partial eviction guessed; the honest lesson is that the closed-form
cold-start model does not describe the interleaved-overflow cells (as the fact pack already notes,
eviction needs the general form, not 1 - N/R).

## Latency note (secondary)

Grouped p90 ~7.5s, interleaved p90 ~15s: interleaved re-prefills the 6k-token prefix on nearly every
request (no reuse), roughly doubling the tail. Within the grouped arms, round-robin p90 (7.3s) is
slightly LOWER than the Endpoint Picker (7.6s) despite a lower hit rate, because co-location concentrates
a group's load onto one replica and adds mild queueing. This echoes the Neuron p90 inversion in the
publication pass: a hit-rate win does not always mean a tail win.

## Validity gates

1/ N >= 2 and stable: PASS (4 replicas on one g6.12xlarge, no scale events, KEDA suspended).
2/ Requests completed: 624/624 per cell, all 8 cells; per-replica shares sum to the request count.
3/ Uniform conditions across cells: same P, L, R, max_tokens, concurrency, model, instance type; only
   the router (x-pub-arm) and admission (send order) vary. Cold cache per cell via unique salted prefixes.
4/ The queue-driven gates (claim_count, infra_rejections, DLQ) do not apply to the direct driver; they
   are exercised in LT-9. This is the cost of the direct-driver deviation, and it is why LT-9 carries the
   exactly-once and durability evidence.

## Verdict (one sentence)

At this operating point (batch L4 cache 30,736 tokens, per-replica interleaved working set 2.54 x cache),
admission grouping and cache-aware routing are COMPLEMENTS: grouping is the condition that lets the
Endpoint Picker deliver (0.914 vs 0.664 hit rate under grouping), while under interleaved overflow
neither router helps (both 0); the sign is regime-dependent and a milder-overflow sweep could shift it.
