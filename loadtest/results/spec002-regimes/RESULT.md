# Experiment 3, routing four regimes on Qwen3-8B / L40S, 2026-09-24: COMPLETE

**Verdict: the four-regime structure reproduces on a second model family. 16 of 16 cells, zero
failed cells.** Predictions 1, 2 and 3 confirmed; prediction 4 confirmed on hit rate and REFUTED on
tail latency by its own stated clause.

Pre-registration and amendment 1: `PRE-REGISTRATION.md` in this directory, both committed before
any cell ran. Cell artifacts: `run-2026-09-24/`.

## 1. Substrate, pinned

| Item | Value |
|---|---|
| Model | `Qwen/Qwen3-8B` @ `b968826d9c46dd6066d109eabc6255188de91218`, passed explicitly with `--model` |
| Engine | vLLM 0.24.0 GPU DLC, `--max-model-len=8192`, `--gpu-memory-utilization=0.9`, prefix caching on |
| Fleet | 4 x `g6e.4xlarge`, all us-west-2c, NVIDIA L40S 46,068 MiB, one replica per GPU |
| **C, measured** | **170,320 tokens** (`num_gpu_blocks` 10,645 x 16) on ALL FOUR replicas |
| L, measured live | 2,087 tokens at `--prefix-reps 130`, tokenized on the serving pod |
| Operating point | P = 42 / 5 / 158, churn reuse 8, conc 8, two arms, two repetitions, seed recorded per arm |
| Driver | in-cluster (`loadtest-driver-r3`), no RBAC; gateway is ClusterIP by committed design |

## 2. The four regimes

Two-repetition means. `R` is derived from `requests_ok / P` out of the run counters, never from the
hit rate, so the model test is not circular.

| Regime | Arm | Hit rate | Model | R | TTFT p90 | Co-location |
|---|---|---|---|---|---|---|
| 1 cold-start, P=42 | EPP | **0.9723** | 0.972 | 36.1 | 0.0801 s | 1.000 |
| | RR | **0.9012** | 0.889 | 36.1 | 0.1051 s | 0.366 |
| 2 control, P=5 | EPP | 0.9976 | 0.997 | 308.3 | 0.0801 s | 1.000 |
| | RR | 0.9975 | 0.987 | 310.2 | 0.0753 s | 0.362 |
| 3 eviction, P=158 | EPP | **0.9658** | 0.969 | 31.9 | 0.0806 s | 1.000 |
| | RR | **0.4207** | 0.862 | 29.0 | 0.2306 s | 0.347 |
| 4 churn, reuse 8 | EPP | 0.8358 | 0.875 | 8.0 | 0.2030 s | 1.000 |
| | RR | 0.5219 | 0.500 | 8.0 | 0.2224 s | 0.372 |

## 3. Verdict per pre-registered prediction

**Prediction 1, cold-start model fits and a tail benefit appears: CONFIRMED.** The Endpoint Picker
arm lands on `1 - 1/R` to three decimals (0.9723 against 0.972) and round-robin is 0.012 above
`1 - N/R`, inside the stated 0.02 tolerance. R = 36.1 sits inside the tail window `10 < R < 40`.

**The tail benefit is reported per repetition, because the mean conceals its spread:**

| | rep 1 | rep 2 | mean of means |
|---|---|---|---|
| EPP p90 | 0.0812 s | 0.0791 s | 0.0801 s |
| RR p90 | 0.0841 s | 0.1262 s | 0.1051 s |
| **EPP lower by** | **3.4 percent** | **37.3 percent** | 23.8 percent |

The direction is consistent across both repetitions but the magnitude is not: a tenfold spread
between 3.4 and 37.3 percent. The 23.8 percent figure is an average of two dissimilar numbers and
must never be cited as "the" tail benefit. Two repetitions cannot separate a genuine effect of
varying size from p90 sampling noise at this request count, so the honest claim is directional
(the Endpoint Picker arm is not slower, and is sometimes much faster), with the magnitude
unresolved at n = 2.

**Prediction 2, control is a tie with no tail benefit: CONFIRMED.** The arms differ by 0.0001 in
hit rate, far inside the stated 0.01, and R = 308 is far above the tail window. The Endpoint Picker
arm is in fact 6.4 percent SLOWER at p90 (0.0805 / 0.0798 against 0.0755 / 0.0751, both
repetitions), which is consistent with what the shares show: at P = 5 with co-location 1.0, one of
the four replicas receives a 0.000 share in both repetitions, so the working set is carried by three
replicas against round-robin's four. Routing earns nothing here and costs a little.

**Prediction 3, eviction splits the arms and needs the general tail form: CONFIRMED.** The Endpoint
Picker holds the cold-start line (0.9658 against 0.969) because its per-replica working set still
fits C, while round-robin falls to 0.4207, far BELOW its own `1 - N/R` of 0.862; the 0.441 shortfall
is eviction. Gap 0.545 in hit rate, and p90 65 percent lower. The `1/f < R < N/f` special case would
predict no benefit at R = 29 (N/R = 0.138 against f = 0.10); the general form `miss_EPP < f <
miss_RR` predicts one correctly, since 0.034 < 0.10 < 0.579. This is the regime that most separates
the arms and it reproduces the Llama series closely (Llama EPP 0.969 / RR 0.411, gap 0.55).

**Prediction 4, large permanent hit-rate gap with NO tail benefit: PARTLY. The hit-rate half is
confirmed; the tail half is REFUTED by the clause the pre-registration itself wrote.** Both arms sit
near the model at R = 8 (0.8358 against 0.875, 0.5219 against 0.500) and the 0.314 gap is permanent
rather than amortizing. But the pre-registration's own refutation condition reads "regime 4 showing
a p90 benefit above the few-percent noise floor", and the measured benefit is **8.3 percent in rep 1
and 9.2 percent in rep 2**, consistently above a few percent and in the same direction twice. The
Llama series measured 2 percent here. Recorded as refuted rather than reasoned away.

## 4. Anomalies, reported rather than smoothed

**One failed request in the whole experiment.** `r3_eviction_rr_rep2`: `requests_ok` 4,579 with
`requests_failed` 1, a rate of 0.02 percent, in the longest cell of the run (1,000 s steady) and in
the arm under the heaviest eviction pressure. It is recorded because a durability-adjacent number
that is quietly rounded to zero is the kind of omission this project does not make. It moves no
verdict: the hit-rate metrics are computed from the replicas' own counters, and one request in 4,579
cannot move 0.4207.

**One replica takes a 0.000 share in both regime 2 repetitions**, noted under prediction 2 above. It
is a consequence of P = 5 groups across N = 4 replicas at co-location 1.0, not a routing defect.

## 5. Comparability

These are Qwen3 numbers and they open no comparison with the Llama series' absolute values. What
transfers is the MECHANISM: the same four regimes, the same closed-form models, the same ordering of
arms within each regime. The driver's vantage also moved in-cluster for this series
(`../spec002-ramp/DRIVER-VANTAGE.md` applies identically here), so absolute latencies are not
comparable across series even where hit rates are structurally alike.
