# Dense rho sweep, 2026-09-25: the curve-versus-step question stays UNRESOLVED, and now we know why

**Verdict: 7 rho points at 2 repetitions CANNOT distinguish a smooth curve from a rounded step.
Signal-to-noise is 1.52 and the measured decline is not monotonic.** This is the answer the
supervisor's prompt allowed for ("or state that 7 points still cannot distinguish them"), and it is
now quantified rather than asserted.

Measurement A (the cold-start tail magnitude) was **not taken**; see section 4.

## 1. Substrate

Identical to the committed ramp: `Qwen/Qwen3-8B`, batch tier, **4 x g6.xlarge, NVIDIA L4, C = 24,064
on every replica**, `P = 52`, `JOBS = 624`, `N = 4`, `TOK_PER_REP = 28`, interleaved arm, Endpoint
Picker, in-cluster driver. `PREFIX_REPS` from the committed formula `round((rho*C*N/P - 7)/28)`.

The round-robin arm was dropped: the curve-versus-step question is about the arm that collapses, and
28 cells did not fit the session's wall-clock release. That is a scope reduction, recorded as one.

## 2. The curve

| rho | PREFIX_REPS | rep 1 | rep 2 | mean | within-rho spread |
|---|---|---|---|---|---|
| 0.90 | 59 | 0.1309 | 0.1887 | 0.1598 | 0.0578 |
| 0.95 | 63 | 0.1887 | 0.1775 | **0.1831** | 0.0112 |
| 1.00 | 66 | 0.0886 | 0.1216 | 0.1051 | 0.0330 |
| 1.05 | 69 | 0.0994 | 0.1181 | **0.1087** | 0.0187 |
| 1.10 | 72 | 0.0647 | 0.0801 | 0.0724 | 0.0154 |
| 1.20 | 79 | 0.0596 | 0.0440 | 0.0518 | 0.0156 |
| 1.40 | 92 | 0.0107 | 0.0139 | 0.0123 | 0.0032 |

**Agreement with the existing 4-point series is good where they overlap**, which is the check that
makes this comparable: rho 1.0 reads 0.1051 here against 0.1116 on 2026-09-24, and rho 1.2 reads
0.0518 against 0.0566. Both differences are inside the within-rho spread.

## 3. Why the question cannot be settled at this resolution

| Quantity | Value |
|---|---|
| Mean WITHIN-rho spread (noise, 2 reps at one rho) | **0.0221** |
| Mean BETWEEN-rho step (signal, adjacent points) | **0.0336** |
| **Signal to noise** | **1.52** |
| Monotonic decline across all 7 points | **NO** |

Two points rise where a monotone decline requires them to fall: **0.90 to 0.95 rises** (0.1598 to
0.1831) and **1.00 to 1.05 rises** (0.1051 to 0.1087). Both rises are smaller than the within-rho
spread at those points, so they are noise rather than structure, but that is exactly the problem: at
this spacing the noise is the same size as the effect being resolved.

**What the data does show.** The largest single drop is 0.95 to 1.00 (0.078, about 3.5x the mean
noise), which is where a step would sit if there were one. But the decline then CONTINUES through
1.1, 1.2 and 1.4 rather than flattening, which is what a true step would predict. So the shape is
consistent with a steepening curve around rho = 1, consistent with a rounded step, and the data
cannot separate those two readings.

**What would settle it**: more repetitions per point, not more points. Noise is per-cell and shrinks
with repetitions; adding rho values at this rep count adds points that each carry the same 0.022
uncertainty. A sensible next design is 5 to 8 reps at four points (0.95, 1.0, 1.05, 1.1) rather than
2 reps at seven.

## 4. Measurement A was not taken, and why

The cold-start tail magnitude (3.4 percent versus 37.3 percent across the two existing reps) required
4 L40S replicas in **one** availability zone. It was abandoned before any cell ran:

1. The fleet came up split across three zones (2 x g6e.2xlarge in us-west-2d, one g6e.4xlarge in
   us-west-2a, one in us-west-2b) while the driver sits in us-west-2b.
2. That biases the measurement ASYMMETRICALLY: the Endpoint Picker arm concentrates on one replica
   (co-location 1.0) while round-robin spreads across all four, so a cross-zone spread moves the p90
   delta between the arms, which is precisely the quantity under test.
3. Pinning to a single zone failed: us-west-2d yields three g6e and refuses a fourth with
   `InsufficientInstanceCapacity`. N = 4 is structural to experiment 3 (the `(N-1)/R` model IS the
   measurement), so three replicas is a different experiment, not a smaller one.

**The decisive argument for stopping rather than running it anyway**: the two existing repetitions
were already same-zone (four g6e.4xlarge in us-west-2c) and still produced a 3.4 versus 37.3 spread.
The variance is therefore intrinsic to p90 at this sample size, not a zone artifact, and a mixed-zone
fleet would have added bias without addressing the cause.

**The tail benefit stands as a stated limitation**: direction is consistent (the Endpoint Picker arm
is never slower in the cold-start regime), magnitude is unresolved and high-variance, and the 23.8
percent mean of two dissimilar numbers must not be cited as a point estimate.
