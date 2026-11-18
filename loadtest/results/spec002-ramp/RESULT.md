# Experiment 4, the overflow ramp on Qwen3-8B / L4, 2026-09-24: COMPLETE

**Verdict: 32 of 32 cells, zero failed cells. Predictions 2 and 3 hold; prediction 1 is REFUTED as
written and prediction 4 survives only on means.** Two of the four predictions were wrong in ways
that are the pre-registration's fault rather than the measurement's, and both are recorded as such.

Pre-registration: `PRE-REGISTRATION.md` in this directory, committed before the first cell.
Cell artifacts: `run-2026-09-24/`. Driver vantage: `DRIVER-VANTAGE.md`.

## 1. Substrate and operating point

| Item | Value |
|---|---|
| Model | `Qwen/Qwen3-8B` @ `b968826d`, asserted live against the served model by escalation gate 0 |
| Fleet | 2 x `g6.xlarge` + 2 x `g6.2xlarge`, NVIDIA L4, one replica per GPU |
| **C, measured on all four replicas** | **24,064 tokens** (`num_gpu_blocks` 1,504 x 16), asserted by escalation gate 1 |
| Operating point | P = 52, R = 12, JOBS = 624, N = 4; rho moves alone |
| PREFIX_REPS by formula | 53 / 66 / 79 / 106 for rho 0.8 / 1.0 / 1.2 / 1.6 |
| Driver | in-cluster (`loadtest-driver`), no RBAC |

**Fleet deviation, recorded.** The pre-registration says "four identical g6.xlarge". The fleet was
two `g6.xlarge` and two `g6.2xlarge`. The homogeneity that matters is the accelerator and the cache,
and both were identical: NVIDIA L4 on every replica, `num_gpu_blocks` 1,504 on every replica. The
instance size differs in host CPU and RAM, which do not enter the KV arithmetic.

## 2. The curve

Two-repetition means.

| rho | B, EPP interleaved | D, RR interleaved | A, EPP grouped | C, RR grouped |
|---|---|---|---|---|
| 0.8 | 0.3394 | 0.2056 | 0.8871 | 0.6630 |
| 1.0 | 0.1116 | 0.1079 | 0.8938 | 0.6643 |
| 1.2 | 0.0566 | 0.0233 | 0.8892 | 0.6618 |
| 1.6 | 0.0081 | 0.0060 | 0.9017 | 0.6677 |

Reference lines: grouped cold-miss floor `1 - P/JOBS` = 0.9167; `1 - N/R` at R = 12 = 0.6667.

## 3. Verdict per pre-registered prediction

**Prediction 1, "both grouped cells sit at the cold-miss floor 0.9167 and do not decline":
REFUTED as written, and the flatness half CONFIRMED.**

Grouped does hold flat: across a doubling of rho, EPP moves 0.8871 to 0.9017 and RR moves 0.6630 to
0.6677, neither declining. That is the substantive claim and it holds. But the level is wrong for
the round-robin arm: grouped RR sits at **0.663 to 0.668, which is `1 - N/R` = 0.6667**, not the
cold-miss floor. Grouped EPP sits near 0.89, also below 0.9167.

**This was foreseeable from committed data and should not have been written.** The Llama reference
line inside `run_lt11.sh` already recorded grouped RR at 0.6643, and this run's own preflight
measured 0.6598 against `1 - N/R` BEFORE the first cell. The error is that the prediction applied a
single-copy cold-miss floor to an arm that replicates every prefix across all N replicas. Corrected
statement: **grouped holds flat in both arms; the EPP arm approaches the cold-miss floor and the RR
arm sits at `1 - N/R`, because grouping removes rho-dependence but not cross-replica replication.**

**Prediction 2, interleaved declines monotonically in rho: CONFIRMED, both arms.** EPP 0.3394 to
0.1116 to 0.0566 to 0.0081; RR 0.2056 to 0.1079 to 0.0233 to 0.0060. Strictly monotonic in both.

**Prediction 3, the decline is a curve rather than a step at rho = 1.0: CONSISTENT, not confirmed.**
The measured shape has no discontinuity at rho 1.0 and falls smoothly across the whole range, which
is what a curve looks like. But four rho points cannot distinguish a smooth curve from a step with
rounded shoulders, and no alternative shape was pre-registered with a discriminating statistic. The
data is consistent with the Llama series' "the point becomes a curve" finding and does not establish
it independently. Marked CONSISTENT deliberately; upgrading it would need a denser sweep near 1.0.

**Prediction 4, "EPP beats round-robin within the interleaved arm at every rho": judged both ways,
and the two judgments disagree.**

- **On two-repetition means: HOLDS at all four rho.** 0.3394 > 0.2056, 0.1116 > 0.1079,
  0.0566 > 0.0233, 0.0081 > 0.0060.
- **Per repetition: 6 of 8 comparisons favour EPP, with one reversal and one exact tie.**

| rho | rep | B (EPP) | D (RR) | EPP ahead? |
|---|---|---|---|---|
| 0.8 | 1 | 0.3583 | 0.1982 | yes |
| 0.8 | 2 | 0.3205 | 0.2129 | yes |
| 1.0 | 1 | 0.1202 | 0.1125 | yes |
| 1.0 | 2 | 0.1029 | 0.1033 | **NO, RR ahead by 0.0004** |
| 1.2 | 1 | 0.0556 | 0.0329 | yes |
| 1.2 | 2 | 0.0577 | 0.0137 | yes |
| 1.6 | 1 | 0.0053 | 0.0053 | **exact tie** |
| 1.6 | 2 | 0.0110 | 0.0066 | yes |

The prediction says "at every rho", and on the pre-registered unit of comparison (the cell mean) it
holds. It is reported as **PARTLY** because the per-repetition view shows the margin collapsing into
noise exactly where the absolute hit rates do: at rho 1.0 the arms are 0.0004 apart, and at rho 1.6
both arms are near zero where a tie is unsurprising. **The defensible claim is that the Endpoint
Picker's advantage is real at low rho and vanishes as the cache is overwhelmed**, which is a more
useful statement than the prediction made.

## 4. Anomalies, reported rather than smoothed

**Four of 32 cells completed fewer than 624 requests**, all grouped, all with `integrity: ok`:

| Cell | requests_ok |
|---|---|
| `cell_A_rho1.0_r2` | 621 |
| `cell_A_rho1.2_r2` | 623 |
| `cell_C_rho1.0_r1` | 623 |
| `cell_C_rho1.2_r2` | 623 |

A shortfall of 1 to 3 requests against 624, or 0.16 to 0.48 percent. The hit-rate metrics are
computed from the replicas' own `vllm:prefix_cache_*` counters over the cell window, so a shortfall
of this size cannot move a four-decimal hit rate, and these four cells sit indistinguishably among
their neighbours. Recorded because a denominator that silently shrinks is the defect class this
project has already been bitten by once (an earlier measurement silently dropped rows whose token
budget ran out), and the rule
earned there was that any category which shrinks a denominator is reported beside the result.

## 5. Comparability

New measurement series. No figure here continues the Llama ramp. The Llama series ran at C = 30,736,
which is the L4 per-replica cache for Llama-3.1-8B, and its driver ran from a laptop against an
externally reachable gateway; this series ran at C = 24,064 from inside the cluster. Hit rates are
structurally comparable because both are computed from replica counters; absolute latencies are not.
