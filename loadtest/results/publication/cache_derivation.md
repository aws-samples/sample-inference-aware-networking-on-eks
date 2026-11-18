# Cache accounting, derived from Prometheus (not estimated)

## NVIDIA (interactive tier, read from vllm:cache_config_info, 2026-07-23 run window)
- num_gpu_blocks = 12204, block_size = 16, enable_prefix_caching = True
- Total KV cache per replica = 12204 x 16 = 195,264 tokens
- Measured blocks queried per request ~2099 (route_nvidia_*.json hits/queries/requests), so prefix ~2099 tokens
- Large working set = 48 prefixes x ~2099 = ~100.7k tokens, which is LESS than the 195k cache: NO overflow
- Conclusion: the NVIDIA hit-rate gap is NOT cache overflow. It is cold-start cache-replication.

## Cold-start cache-replication model (verified)
- Under round-robin, each distinct prefix is routed to all 4 replicas, so it must be cold-computed on
  each: about 48 x 4 = 192 cold requests.
- Under the Endpoint Picker (co-location ~1.0), each prefix is pinned to one replica: about 48 cold.
- Over ~1590 requests: predicted hit RR = 1 - 192/1590 = 0.879 (measured 0.892), EPP = 1 - 48/1590 =
  0.970 (measured 0.972). The model matches. Measured cold requests: RR ~170, EPP ~44.

## Neuron (PC-on): genuine overflow
- Cache = 513 x 32 = 16,416 tokens (exact). Working set = 48 x ~1200 = 57.6k > cache: overflow.
- Both arms overflow and evict; low absolute hit rates (EPP 0.251, RR 0.154). Cold-start replication
  also present but eviction dominates.

## LT-2 (2026-07-21): round-robin distribution uniform
- Per-pod request share over the active window: 0.24 / 0.25 / 0.25 / 0.26 (max 0.26), across 4 pods.
- The round-robin baseline was genuinely uniform (a real LoadBalancer, not a pinning port-forward).
- LT-2 used 400 prefix groups (~800k tokens) which OVERFLOWS the 195k cache: genuine overflow regime,
  RR hit 54.7 percent, a larger delta (65 percent) than the 48-group cold-start run (17 percent).
- Verdict: the 65 percent figure is SUPERSEDED (valid baseline, heavier working set), not invalid.

## Cold-start replication model (Phase A)
- Closed form: with N replicas, P distinct prefixes, R = requests per prefix, EPP hit = 1 - 1/R,
  round-robin hit = 1 - N/R, gap = (N-1)/R.
- R is derived from requests_ok / P (run counters), NOT from the hit rate (non-circular).
- Fits: NVIDIA large (P=48, R=33.25): pred EPP 0.970 / RR 0.880, meas 0.972 / 0.892.
  NVIDIA control (P=6, R=264): pred 0.996 / 0.985, meas 0.998 / 0.998 (tie because R is high, gap
  (N-1)/R = 3/264 = 0.011; NOT because it fits cache, the large set also fits).
- Misfits (eviction regime, model does not apply): Neuron (P=48, R=6): pred 0.833 / 0.333, meas
  0.251 / 0.154 (2-rep means). LT-2: no single R fits EPP 0.979 and RR 0.547 together.
- Fleet prediction (predicted, not measured): gap (N-1)/R at R=33 is 0.091 (N=4), 0.21 (N=8), 0.45 (N=16).
- Amortization: for a FIXED working set reused indefinitely, R grows and the gap converges to 0; the
  benefit is steady-state only under prefix churn or eviction.

## Neuron retention-starved (Phase B)
- max_num_seqs x max_model_len = 8 x 2048 = 16,384; total cache = 513 x 32 = 16,416; 99.8% reserved for
  active sequences, 32-token retention headroom. Cache cannot retain prefixes past request completion,
  so hits occur only on temporal overlap. Explains EPP 0.251 despite per-replica WS 14.4k < 16.4k.
  Caused by the neuronx-cc SIGHUP that forced num-gpu-blocks-override=512.

## Eviction and churn runs (Phase C, gateway-equalized)
- EVICTION P=180 (WS 180 x 2099 = 377,820 = 1.9x the 195,264 cache), R held ~33:
  EPP hit 0.969 (per-replica 94.5k FITS, stays on cold-start line 1-1/R=0.970),
  RR hit 0.411 (2-rep mean; rep1 alone was 0.418) (per-replica 378k OVERFLOWS; cold-start line 0.867, eviction excess 0.456).
  Gap 0.55 hit; p90 25% lower (0.470 vs 0.623 s), exceeds the cold-start gap (0.08 hit / 17% p90). R_rr=30 (RR
  slower under eviction, fewer requests in the same wall time; recorded).
- CHURN reuse=8 (each prefix used 8 times then never again; R=8 by construction, PERMANENT, cache
  never overflows): EPP 0.873 (model 0.875), RR 0.518 (model 0.500), both fit. Gap 0.356 permanent (compute). p90 only 2% lower (0.568 vs 0.580 s): R=8 is below the tail window.
  p90 gap small (2%) because at R=8 base prefill dominates the tail; the durable signal is hit rate.

## Tail-latency window (derived extension)
- Cold fraction = 1/R (EPP), N/R (RR). The p90 reflects a cold request above 0.10, so a tail benefit
  needs RR p90 cold and EPP p90 warm: 1/R < 0.10 < N/R, i.e. 10 < R < 10N (N=4: 10 < R < 40).
  Generalized to threshold f: 1/f < R < N/f (p99, f=0.01: 100 < R < 100N).
- Validation (all fit): cold-start R=33 EPP 3.0%/RR 12.1% (RR cold, EPP warm, in window) -> 17% p90.
  eviction R=30 3.3%/13.3% (in window + eviction) -> 25%. churn R=8 12.5%/50% (BOTH cold, below window)
  -> 2%. control R=264 0.4%/1.5% (both warm, above window) -> tie.
- TWO benefits: (1) compute saved (hit rate) permanent under churn/eviction, amortizes for a fixed set;
  (2) tail latency only inside the window. A large hit-rate gap can give no tail gap (churn: 0.36 hit,
  2% p90). Churn settles amortization for compute, NOT for tail latency.

## Tail window: general form (holds under eviction) + p99
- GENERAL: tail benefit at threshold f when miss_EPP < f < miss_RR (miss ~ 1 - hit). Cold-start special
  case: miss_EPP=1/R, miss_RR=N/R -> 1/f<R<N/f (p90: 10<R<10N; p99: 100<R<100N).
- Eviction REQUIRES the general form: measured miss_RR=0.589 (hit 0.411) >> N/R=0.133. It passed the
  R-formula for the wrong reason. Counterexample the R-formula gets wrong: high R (264) + eviction ->
  N/R=1.5% says no tail benefit, but actual miss_RR is eviction-driven (cold p90) while miss_EPP~1/R
  (warm p90), so there IS a benefit.
- Re-validated (general form, actual miss): cold-start 0.028/0.108 benefit yes (17%); eviction
  0.031/0.589 yes (25%, general form required); churn 0.127/0.482 no (2%, both cold); control
  0.002/0.002 no (tie). All fit.
- SCOPE LIMITATION: window model is BINARY (predicts presence, not magnitude); cannot distinguish 17%
  from 25%. Hit-rate model is quantitative.
- p99 (f=0.01) window 100<R<400: control R=264 is inside it, so the model predicts a p99 benefit for
  the control despite no p90 benefit. NOT TESTED: pub_routing.py records only p50/p90, no raw samples,
  so p99 is not in control_smallws_* JSON. Upper bound untested; cheapest follow-up = add p99 to the
  driver and rerun the control.

## p99 recovery from vLLM histogram (Prometheus), control misfit, near-threshold
- p99 RECOVERED from vllm:time_to_first_token_seconds histogram (driver records only p50/p90). Control
  arms ran sequentially (EPP 12:54-13:10, RR 13:14-13:29 UTC 2026-07-23), separated by time.
- OUTCOME 3 CANNOT RESOLVE: server-side p99 ~0.0795s (EPP) vs ~0.0794s (RR), diff <0.3ms, wrong
  direction. vLLM TTFT buckets here: le=0.04,0.06,0.08,0.10,0.25. Control prefill is fast (900-tok
  prefixes: cold~0.07s, warm~0.05s), so BOTH arms' p99 falls in the single [0.06,0.08] bucket
  (>0.08s ~0% for all 4 runs). Not benefit-absent; the cold events are too fast to cross a bucket
  boundary. Server-side TTFT (0.05-0.08s) << client-side 0.44s (excludes gateway RTT); valid comparison
  but bucket granularity is the limit. Upper bound UNTESTED; follow-up = finer buckets or larger prefix.
- CONTROL is a MISFIT (not a fit): measured miss_RR ~0.002 vs predicted N/R=0.0152 (6.6x below). Cause:
  warmup exclusion. Driver stage-1 probe warms coloc_groups=6 prefixes; hit-rate snapshot taken AFTER
  probe. Control (6 groups) -> all warmed -> ~0 cold in measured window. Large (48 groups, 6 warmed) ->
  42 cold -> miss 0.108 (fits). Model predicts miss INCLUDING warmup; discarding warmup under-measures
  small working sets (cold front-loaded). Production/churn have no discarded warmup, model applies.
  Measured = FLOOR for small working sets. Validation leads with churn + eviction, not control.
- NEAR-THRESHOLD (C.1): cold-start miss_RR=0.108 is only 0.008 above the 0.10 p90 threshold; treat
  predictions within +/-0.02 of threshold as uncertain. It fits (17%) but is a near-miss on the edge.
- Eviction RR hit = 0.411 (2-rep mean; rep1 alone 0.418); use 0.411 consistently. Gap 0.56, miss 0.589.
