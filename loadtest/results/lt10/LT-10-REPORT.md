# LT-10: the shipped queue path reproduces the LT-8 effect

STATUS: RUN COMPLETE (2026-07-26), PASS. Cluster inference-aware-eks, batch tier on one g6.12xlarge
(4x L4, num_gpu_blocks 1921 = same cache as LT-8), worker image v10 (with the Session + connect-only
retry + SQS_BATCH_SIZE=1 fixes). Both the operating point and the interpretation were pre-registered
before the run.

## Purpose

LT-8 proved and quantified the effect by driving the arrival pattern DIRECTLY at the gateway
(`lt8_direct.py`) to isolate the router from queue-induced variance. LT-10 confirms OUR system produces
that pattern end to end through the full shipped path (submit API, Amazon SQS, worker, gateway,
Endpoint Picker). It is a SIGN confirmation, not a magnitude re-measurement.

## Success criterion and result

PASS: the grouped cell beats the interleaved cell on prefix-cache hit rate in the same direction LT-8
measured. Grouped A' = 0.9133, interleaved B' = 0.2434 (mean), so grouped >> interleaved. The shipped
system reproduces the effect. COMPLEMENTS confirmed through the queue path.

## Operating-point match (asserted programmatically, equals LT-8)

| Parameter | LT-8 | LT-10 | match |
|---|---|---|---|
| P distinct prefixes | 52 | 52 | yes |
| prefix length L (prefix_reps) | 5999 tok (214) | 5999 tok (214) | yes |
| total requests (JOBS) | 624 | 624 | yes |
| R = JOBS/P | 12 | 12 (effective_R 12.0 measured) | yes |
| max_tokens | 64 | 64 | yes |
| batch replicas N | 4 | 4 (asserted 4/4 start and end of every cell) | yes |
| num_gpu_blocks (cache) | 1921 | 1921 | yes |
| model | Llama-3.1-8B | Llama-3.1-8B | yes |
| temperature | 0.0 | 0.0 | yes |
| cache reset | unique salted prefixes | unique salted prefixes | yes |

## Results (two cells, two reps each, through the full path, Endpoint Picker active)

| Metric | A' grouped (2 reps) | B' interleaved (2 reps) |
|---|---|---|
| prefix-cache hit rate | 0.9133, 0.9133 (mean 0.9133) | 0.2469, 0.2399 (mean 0.2434) |
| effective R | 12.0 | 12.0 |
| co-location (max replica share) | 0.365, 0.269 | 0.256, 0.264 |
| end-to-end latency p50/p90 (submitted -> terminal) | 6/7 s | ~252/440 s |
| queue wait p50/p90 (submitted -> claimed) | 0/3 s | ~247/435 s |
| inference interval p50/p90 (claimed -> terminal) | ~4-5/6 s | ~6-7/10 s |
| claim_count / infra_rejections / real runs | 624 / 0 / 624 | 624 / 0 / 624 |

## LT-8 comparison, signs side by side

| | LT-8 (direct) | LT-10 (queue path) |
|---|---|---|
| grouped hit rate | 0.9135 | 0.9133 |
| interleaved hit rate | 0.000 | 0.2434 |
| sign (grouped vs interleaved) | grouped > interleaved | grouped > interleaved (MATCHES) |
| magnitude | reference | grouped identical; interleaved differs (see below) |

## The magnitude difference (pre-registered, and it appeared)

The grouped magnitude reproduced almost exactly (0.9133 vs 0.9135): drain-detected grouping keeps each
prefix in flight alone, so the Endpoint Picker co-locates it on one replica and it fits, identically to
direct grouping. The INTERLEAVED magnitude differs as pre-registered: LT-8 direct interleaved thrashed
to 0.000 because the direct driver issued requests in a strict prefix-major order that maximally
separated each prefix's requests, while the queue path's worker poll timing and Amazon SQS delivery
spread arrivals so that some same-prefix requests land close together, giving partial temporal locality
(0.2434) before eviction. This is a finding about the delivery mechanism, not a contradiction: the sign
is identical, and the interleaved regime still collapses far below grouped.

Bonus finding the direct measurement could not show: end-to-end latency. Grouped e2e p90 is 7s;
interleaved e2e p90 is about 440s, and the gap is almost entirely QUEUE WAIT (247s p50, vs 0s for
grouped), not inference time (both about 6-10s). Under interleaved overflow the cache thrashes, per-job
throughput drops, and 624 jobs back up in the queue. So on the shipped path grouping wins twice: higher
hit rate AND dramatically lower end-to-end latency, the second effect being invisible to a direct
driver that has no queue.

## Validity gates

1/ infra_rejections == 0 in ALL four cells, including the two heavy interleaved cells: PASS. The Session
   fix held under sustained load (item 2.2 confirmed); no claim_count > 1 under healthy load.
2/ Zero DLQ, reconciliation clean (624/624 complete), zero orphan sweeps needed: PASS in every cell.
3/ N = 4 asserted at the start and end of every cell (KEDA suspended on BOTH the batch and async-worker
   ScaledObjects; worker replicas fixed at 8; SQS_BATCH_SIZE=1 confirmed from the deployment): PASS.
4/ Deployed-image preflight (submit API must stamp submitted_at): PASS. submitted_at populated on every
   job, so end-to-end latency was measurable for the first time.

## Session-fix confirmation (item 2.2)

infra_rejections = 0 across all 2496 requests (4 cells x 624), including the interleaved cells that held
hundreds of jobs in flight with a deep queue. No stall recurred after the v10 Session + connect-only
retry fix. CONFIRMED on-cluster under load (previously only correct by construction).

## H4 evidence (item 2.3)

Envoy access logs captured for the whole window (`capture/envoy-access.log`, 2496 batch requests).
(Note added 2026-11-02: the log also holds 42 unsolicited internet-scanner requests, for example
`/.env` and `/.git/config`. On 2026-07-26 the gateway Service was still an internet-facing load
balancer; the relay cutover on 2026-08-04 made it ClusterIP. All 42 got a 500, reached no upstream,
and carry no correlation ID or objective, so no figure in this report includes them.) The
gateway request duration distribution: p50 5.8s, p90 8.8s, p99 12.5s, MAX 14.7s. ZERO requests over 60s,
ZERO near the 300s ext_proc message_timeout, ZERO near the 660s client timeout. The durations equal
normal inference time, so no request stalled at the gateway or in the ext_proc callout. Combined with
infra_rejections = 0, this is a clean ABSENCE of stalls: the fix resolved the intermittency and H4 (an
Endpoint-Picker or gateway-path stall) was NOT triggered in this run. Because no stall occurred, this
run cannot DISCRIMINATE H2-exhaustion from H4 (the discriminators only apply during a stall); it shows
the fix worked and H4 did not manifest. LIMITATION: the Endpoint Picker /metrics scrape captured 0 lines
(a port-forward or endpoint issue), so EPP-side metrics are unavailable for this window; the access log
is the primary discriminator and it is clean. [Data needed]: EPP-side metrics on a future run.

## Verdict (one sentence)

The shipped async front door reproduces the LT-8 effect end to end: driving the queue with a grouped
admission policy yields the grouped hit rate (0.9133, identical to LT-8) while interleaved collapses far
below it (0.2434), the sign matches LT-8 exactly, the pre-registered magnitude difference in the
interleaved cell appeared for the pre-registered reason (worker poll timing spreads arrivals), grouping
additionally cuts end-to-end latency from about 440s to 7s through the queue, and the worker Session fix
held with zero infra rejections and zero gateway stalls across all 2496 requests.
