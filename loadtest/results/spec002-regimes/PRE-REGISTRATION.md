# Experiment 3, routing four regimes on Qwen3-8B / L40S: PRE-REGISTRATION

Written and committed **before the first cell runs**, on the maintainer's ruling of 2026-09-24 ("pre-register
it before running: yes, but start now"). It fixes the design, the operating points and the predictions
so the result can only confirm or refute them. It is the sibling of the ramp's pre-registration
(`../spec002-ramp/PRE-REGISTRATION.md`) and follows its rules.

## 0. What is new here, and what is carried unchanged

**Carried unchanged from the Llama series**, so that only the model differs: four regimes, two arms
(Endpoint Picker against genuine cache-blind round-robin), two repetitions per cell, warm-up
discarded, both arms entering through the same gateway so the network path is equalized, and the
closed-form models below. The driver is the same `loadtest/pub_routing.py`.

**New in this series:**

1. `pub_routing.py` no longer carries a hardcoded model id. It is passed explicitly or read live from
   the Deployment's own `--model` argument, and is recorded in every result JSON.
2. **Every arm records a seed** (`--seed`). The Llama publication arms recorded none, which is the
   one reproducibility gap this series closes by construction. Decoding is greedy (temperature 0.0),
   so the seed changes no output today; it is recorded so the sampling state is stated rather than
   defaulted.
3. **Working sets are expressed as RATIOS against the measured per-replica cache C**, not as absolute
   prefix counts. The Llama series could hardcode P because its C was known. Qwen3-8B on L40S is the
   one cache number in spec-002 still derived (~171,000 tokens), so P is solved at run time from the
   C measured at Gate 1. If the measured C differs materially from the derivation, the P values move
   and the ratios do not. That is the whole point of stating them this way.

## 1. The two inputs that must be measured before any cell runs

| Input | Llama series | Qwen3 series | How obtained |
|---|---|---|---|
| C, per-replica KV on L40S | 195,264 (measured, `num_gpu_blocks` 12,204 x 16) | **DERIVED ~171,000, TO BE MEASURED** | first act after readiness, `num_gpu_blocks` from the first replica, per the standing condition |
| L, tokens per prefix | ~2,099 | **TO BE MEASURED** | tokenize the actual salted prefix on the serving pod, as the ramp does, never assumed from the Llama value |

Qwen3-8B costs 147,456 bytes per KV token against Llama-3.1-8B's 131,072, a ratio of 1.125, so the
expectation is `195,264 / 1.125 = 173,568` before the weight difference, and ~171,000 after it. The
measurement at Gate 1 settles it and closes the last derived number in spec-002.

## 2. The four regimes, as ratios

`N = 4` replicas throughout. `P` is the count of distinct prefixes. Under round-robin every prefix is
replicated to all N replicas, so the per-replica working set is `P x L`; under the Endpoint Picker at
co-location 1.0 it is `(P/N) x L`. The regime is defined by where `P x L` sits against C.

| # | Regime | Ratio target, `P x L / C` | P for C = 171,000 and L = 2,130 (provisional, recomputed at Gate 1) | Llama equivalent |
|---|---|---|---|---|
| 1 | Cold-start replication | **0.52** (fits, no overflow) | P = 42 | P = 48, 0.516 |
| 2 | Control, small working set | **0.065** (trivially fits) | P = 5 | P = 6, 0.065 |
| 3 | Eviction | **1.93** (round-robin overflows, Endpoint Picker does not) | P = 155 | P = 180, 1.93 |
| 4 | Prefix churn | not a ratio: `--churn-reuse 8` fixes R = 8 by construction and the cache never overflows | reuse = 8 | reuse = 8 |

Regime 3 is the one that requires the ratio framing: it is defined by round-robin exceeding C while
the Endpoint Picker's `(P/N) x L` stays under it, which is only true for a band of P that moves with C.

## 3. The models being tested (stated before the run)

Closed form, with R the requests per prefix, **derived from `requests_ok / P` out of the run counters
and never from the hit rate**, so the test is not circular:

- Endpoint Picker hit rate `= 1 - 1/R`
- Round-robin hit rate `= 1 - N/R`
- Gap `= (N-1)/R`

Tail-latency window, general form: a tail benefit at threshold f exists when `miss_EPP < f < miss_RR`.
The cold-start special case reduces to `1/f < R < N/f`, so for p90 and N = 4 the window is `10 < R < 40`.

## 4. Predictions (the acceptance signal, fixed before the run)

1. **Regime 1 fits the cold-start model** in both arms, within the tolerance the Llama series achieved
   (predicted 0.970 / 0.880 against measured 0.972 / 0.892, so within 0.02 absolute), and the
   Endpoint Picker shows a p90 tail benefit because R lands inside `10 < R < 40`.
2. **Regime 2 is a tie.** Both arms warm, hit rates within 0.01 of each other, and NO tail benefit,
   because R is far above the window. Routing earns nothing when every replica warms every prefix.
3. **Regime 3 splits the arms.** The Endpoint Picker stays on the cold-start line (`1 - 1/R`) because
   its per-replica working set still fits C, while round-robin falls far BELOW `1 - N/R`, the excess
   being eviction. A tail benefit exists and the general form is required to predict it.
4. **Regime 4 shows a large hit-rate gap with NO tail benefit.** Both arms fit the model at R = 8
   (about 0.875 and 0.500), the gap is permanent rather than amortizing, and p90 is within a few
   percent because at R = 8 both arms are cold and base prefill dominates the tail.

**What would refute them:** regime 1 missing the model by more than 0.02 absolute in either arm;
regime 2 showing a hit-rate gap above 0.01 or a p90 benefit; regime 3 showing the Endpoint Picker
falling off the cold-start line, or round-robin sitting at `1 - N/R` rather than below it; regime 4
showing a p90 benefit above the few-percent noise floor. Any of those is reported as measured. The
parameters in this file are not adjusted to recover a prediction.

## 5. Escalation gates (the run stops rather than degrades)

1. **C is read as the first act after readiness** and recorded beside the derivation, per the standing
   condition. The P values in section 2 are recomputed from the measured C before any cell runs, and
   the recomputation is recorded here as an amendment rather than applied silently.
2. **Four replicas, on four distinct GPUs, all the same GPU.** The pool now permits 4xlarge and
   12xlarge (ruled 2026-09-24 for availability), so a 12xlarge hosting four replicas on its four L40S
   is acceptable and is in fact more homogeneous than four separate nodes. A MIXED fleet is not: the
   run stops.
3. **The round-robin arm must be proven genuinely cache-blind before any delta is trusted**, by its
   per-replica request share being uniform (the Llama series measured 0.24 / 0.25 / 0.25 / 0.26). A
   round-robin arm whose co-location looks like the Endpoint Picker's means the arm is not routing,
   and the run stops, which is the same gate the ramp's preflight already exercises.
4. **The driver runs inside the cluster.** The gateway is ClusterIP by committed design (INV12, exactly
   one externally exposed Service, the relay's), so a laptop driver cannot reach it. The stated
   consequence is recorded in `../spec002-ramp/DRIVER-VANTAGE.md` and applies identically here:
   absolute latencies shift against the Llama series, the hit-rate metrics do not, and under the
   comparability rule no Qwen3 figure is presented as a continuation of the Llama series.

## 6. Cost, against the ceiling raised to 500 USD on 2026-09-24

Four L40S replicas for an estimated 5 to 6 hours of held wall clock, covering acquisition, bring-up,
the C measurement, 16 cells (4 regimes x 2 arms x 2 repetitions) and the analysis pass.

| Fleet shape | USD/hr | 6 h |
|---|---|---|
| 4 x g6e.xlarge | 7.444 | ~45 |
| 4 x g6e.4xlarge | 12.017 | ~72 |
| 1 x g6e.12xlarge (4 L40S, one acquisition event) | 10.4926 | ~63 |

Reported at each 25 USD increment. The campaign does not scale down or release on cost grounds; only
The maintainer stops the clock, and 500 USD is the hard stop.

---

# AMENDMENT 1, 2026-09-24: the measured cache, and the operating point derived from it

Recorded **before any cell runs**, as escalation gate 1 requires. Nothing in sections 1 to 6 above is
edited; this amendment supplies the numbers those sections deliberately left open.

## A1.1 C is now MEASURED, and the last derived number in spec-002 is closed

Read as the first act after readiness, before any request reached the pod, on the first L40S replica
(`vllm-llama3-interactive-7f9765b5c5-6zc82`, g6e.4xlarge, us-west-2c, NVIDIA L40S 46,068 MiB):

| Quantity | Derived (spec-002 section 4) | **Measured** | Delta |
|---|---|---|---|
| `num_gpu_blocks` | not derived | **10,645** | |
| C, KV cache tokens | ~171,000 | **170,320** (10,645 x 16) | derivation 0.4 percent high |
| Max concurrency at 8,192 | not derived | **20.79x** | |

Two independent witnesses agree exactly: the metrics label `kv_cache_size_tokens="170320"` and vLLM's
own startup line `GPU KV cache size: 170,320 tokens`. Served model confirmed `Qwen/Qwen3-8B`.

**This closes the one cache number spec-002 carried as derived.** Pre-check B flagged it in writing on
2026-09-21 ("Nothing about the L40S. That C is still derived and must be measured in experiment 3"),
and this is that measurement. The KV arithmetic now has three measured points on Qwen3 (L4 economy
149,376, L4 standard 24,064, L40S standard 170,320) plus premium 82,432, and the derivation was
within 0.4 percent here against 3.0 percent on premium and 3.8 percent on the L4 batch lane.

## A1.2 L is calibrated so R lands in the same window as the Llama series

Measured live with the pinned Qwen3-8B tokenizer on the serving pod, not assumed:

| `--prefix-reps` | L (tokens) | P at 0.52 | P at 0.065 | P at 1.93 |
|---|---|---|---|---|
| 60 (driver default) | 967 | 92 | 11 | 340 |
| 120 | 1,927 | 46 | 6 | 171 |
| **130 (CHOSEN)** | **2,087** | **42** | **5** | **158** |
| 150 | 2,407 | 37 | 5 | 137 |

**Why 130 and not the driver default.** The Qwen3 tokenizer spends 16.1 tokens per repetition of the
base sentence where the Llama series' prefix ran to about 2,099 tokens. Keeping `--prefix-reps 60`
would hold the ratios but force P to 92 and 340, and since `R = requests_ok / P` at fixed request
volume, R would fall roughly threefold. Regime 3 in particular would drop to R near 5, which is below
the tail window `10 < R < 40` and would turn the eviction regime into something closer to churn. That
would change what the regime measures while appearing to honour the pre-registration.

`--prefix-reps 130` puts L at 2,087 against the Llama series' ~2,099, so **both the ratio against C
and the prefix size in tokens match**, and P lands at 42 / 5 / 158 against the Llama series' 48 / 6 /
180. The ratios in section 2 are unchanged and remain the binding statement; this is the value of L
they are solved at.

## A1.3 The operating point, fixed

| Regime | `--groups` (P) | `--prefix-reps` | Other |
|---|---|---|---|
| 1, cold-start replication | 42 | 130 | |
| 2, control, small working set | 5 | 130 | |
| 3, eviction | 158 | 130 | |
| 4, prefix churn | 42 | 130 | `--churn-reuse 8` |

Both arms, two repetitions each, seed recorded per arm, `--model Qwen/Qwen3-8B` passed explicitly.

## A1.4 Fleet state at the time of writing, recorded rather than smoothed

The L40S fleet is **NOT yet complete**: 1 of 4 replicas Ready (g6e.4xlarge, us-west-2c). Karpenter
logged **14 distinct `InsufficientInstanceCapacity` combinations** in the acquisition window, covering
g6e.xlarge, g6e.2xlarge, g6e.4xlarge and g6e.12xlarge across **all four** availability zones,
including the us-west-2d subnet created this morning specifically to widen the pool. The subnet is
working (Karpenter reached into 2d and AWS answered with capacity errors rather than routing errors);
2d simply has no g6e capacity in this window either.

No cell of experiment 3 runs until 4 replicas are Ready on the same GPU, per escalation gate 2. The
partial fleet is held, not released, under the standing order.
