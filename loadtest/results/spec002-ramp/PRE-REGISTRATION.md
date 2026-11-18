# Experiment 4, the overflow ramp on Qwen3 / L4: PRE-REGISTRATION

Written and committed **before the first cell runs**. The ramp's own rule: do not tune toward step
or ramp. This file fixes the operating point and the predictions so the result can only confirm or
refute them.

## 1. The two inputs that changed from the Llama series

| Input | Llama-3.1-8B series | **Qwen3-8B, this series** | How obtained |
|---|---|---|---|
| C, per-replica KV (tokens) | 30,736 | **24,064** | LIVE read on all four replicas today: `num_gpu_blocks=1504` x block_size 16, all four agreeing, all on identical g6.xlarge |
| tokens per prefix repetition | ~27.6 | **28.0** | measured with the pinned Qwen3-8B tokenizer (`b968826d`) |

**The operating point moved because C fell, not because the tokenizer changed**: 28.0 against 27.6
is a 1.4 percent difference. The plan anticipated a tokenizer effect; measured, it is negligible.

## 2. Operating point (P, R, JOBS, N held exactly as the Llama series)

`P = 52`, `R = 12`, `JOBS = 624`, `N = 4`, so rho moves **alone** and every model prediction stays
constant, which is the whole design of the sweep.

rho -> PREFIX_REPS by the documented formula `round((rho*C*N/P - 7)/TOK_PER_REP)`, now computed in
the script rather than tabulated. The formula reproduces the committed Llama table (67, 84, 101,
135) **exactly**, which is why parameterising the script is safe for both series.

| rho target | PREFIX_REPS (Qwen3, C=24,064) | (Llama, C=30,736) |
|---|---|---|
| 0.8 | **53** | 67 |
| 1.0 | **66** | 84 |
| 1.2 | **79** | 101 |
| 1.6 | **106** | 135 |

L is verified LIVE per rho by tokenizing the actual salted prefix on the serving pod, and the
measured rho is recorded beside the target in `rho_map.txt`.

## 3. Predictions (the acceptance signal, stated before the run)

1. **GROUPED holds flat.** Both grouped cells (EPP and round-robin) sit at the cold-miss floor,
   `1 - P/JOBS = 1 - 52/624 = 0.9167`, and do **not** decline as rho rises: grouping runs a prefix's
   R requests consecutively, so the working set resident at any moment is one prefix, independent
   of rho.
2. **INTERLEAVED declines monotonically in rho.** Interleaving holds P prefixes resident at once,
   so once `(P/N) x L` exceeds C the cache evicts between reuses and the hit rate falls as rho grows.
3. **The decline is a curve, not a step**, at these rho values (the Llama series' finding that "the
   point becomes a curve"); this series tests whether that shape reproduces across model families.
4. **EPP beats round-robin within the interleaved arm** at every rho, since cache-aware placement is
   what the router exists to do.

**What would refute them**: grouped declining with rho; interleaved flat; a step at rho=1.0; or EPP
not beating round-robin interleaved. Any of those is reported as measured, not tuned away.

## 4. Escalation gates (unchanged, and one already exercised)

1. C must equal the expected value on the pinned instance. The script asserts it and **stops**
   otherwise. It stopped correctly today for the old 30,736 expectation, which is how the new C
   entered this file rather than a script default.
2. N must be pinned and stable across all cells (KEDA suspended; `paused-replicas=4`).
3. All replicas must be the same GPU. Verified: four identical g6.xlarge, so the heterogeneity
   escalation the Llama derivation warned about does not apply to this run.
