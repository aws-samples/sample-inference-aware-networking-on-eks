# LT-8 batch-tier cache derivation (static half)

Everything here is derived from committed manifests and the model architecture. Exactly ONE value is
left live: `num_gpu_blocks`, read from the running batch pod. Read that one number and the whole LT-8
design (P, prefix length L, jobs, and the pre-registered predictions) resolves. Do NOT reuse the
interactive tier's 195,264 figure without re-deriving: the batch Deployment differs, and the batch
NodePool is heterogeneous (see the escalation below).

## 1. Static inputs (from the repo, not estimated)

From `manifests/02-serving/vllm-batch.yaml`:
1/ `--model NousResearch/Meta-Llama-3.1-8B-Instruct` (Llama-3.1-8B architecture)
2/ `--enable-prefix-caching` (blockwise KV, so the hit-rate model applies, unlike the Neuron path)
3/ `--max-model-len 8192`
4/ `--gpu-memory-utilization 0.9`
5/ `replicas: 4` (N = 4; pinned and KEDA-suspended for LT-8, see run_lt8.sh)

Llama-3.1-8B KV geometry (standard config): 32 layers, 8 KV heads (GQA), head_dim 128, bf16 (2 bytes).
Block size is the vLLM default 16.

## 2. The formula (per-replica KV cache in tokens)

    C_tokens = num_gpu_blocks x block_size          (block_size = 16)

`num_gpu_blocks` is what vLLM computes at startup after loading weights, so it already accounts for
weights, activation, and the 0.9 utilization cap. It is the ONE live input:

    kubectl -n inference exec <batch-pod> -- python3 -c \
      "import urllib.request;print([l for l in urllib.request.urlopen('http://localhost:8000/metrics',timeout=10).read().decode().splitlines() if 'cache_config_info' in l])"

(or read `vllm:cache_config_info{num_gpu_blocks=...,block_size=...}`). Record it in predictions.json.

Cross-check on the arithmetic (so a wrong reading is caught):
- Per-token KV = 2(K,V) x 32 layers x 8 KV-heads x 128 head_dim x 2 bytes = 131,072 bytes = 128 KiB.
- Per-block KV = 128 KiB x 16 = 2 MiB.
- Weights = 8.03e9 params x 2 bytes ~ 16.1 GB.
- num_gpu_blocks ~ floor((GPU_mem x 0.9 - 16.1 GB - activation overhead) / 2 MiB).

## 3. ESCALATION: the batch NodePool is heterogeneous, so C is not one number

`manifests/02-serving/vllm-batch.yaml` uses `nodeSelector: tier: batch` and the batch NodePool is
diversified across g6e (L40S, 48 GB), g6 (L4, 24 GB), and g5 (A10G, 24 GB). C depends on which GPU a
replica landed on:

| GPU (instance) | GPU mem | Estimated num_gpu_blocks | Estimated C (tokens) |
|---|---|---|---|
| L40S (g6e) | 48 GB | ~12,000 (interactive read 12,204) | ~192k |
| L4 (g6) / A10G (g5) | 24 GB | ~1,500 to 2,800 (ESTIMATE, overhead-sensitive) | ~24k to 46k |

These estimates are for planning only; the live `num_gpu_blocks` per replica is authoritative. The
consequence is a hard requirement, not a nicety:

1/ **Pin the batch tier to a SINGLE instance type for LT-8** (a nodeSelector on
   `karpenter.k8s.aws/instance-family`, restored at teardown), OR read `num_gpu_blocks` on every
   replica and confirm they are equal. If replicas sit on different GPUs, C differs across replicas
   and the four cells are not run under identical conditions. That is escalation trigger 3 (a
   contaminated 2x2 is worse than none): stop and escalate rather than average across GPUs.
2/ **Prefer a 24 GB card (g5 or g6) for LT-8.** A smaller C makes the eviction regime reachable with a
   far smaller working set and jobs count, so the session is cheaper. The mechanism is the same; only
   the absolute hit rates differ.

## 4. Sizing P and prefix length L (resolves once num_gpu_blocks is read)

Crack 2 (accepted): size so the PER-REPLICA Endpoint-Picker-interleaved working set also overflows,
not just the aggregate. With N = 4:

    per-replica EPP-interleaved working set = (P / N) x L     target ~ 2 to 3 x C
    aggregate working set                   = P x L           then ~ 8 to 12 x C

Procedure once C = num_gpu_blocks x 16 is known:
1/ Choose L via PREFIX_REPS. L is about 30 tokens per repetition of the base sentence (VERIFY live the
   same way the NVIDIA derivation did: blocks-queried-per-request from vllm counters is the prefix
   token length). A convenient target is L ~ a few hundred tokens (PREFIX_REPS ~ 10 to 20).
2/ P = ceil(target_aggregate x C / L), with target_aggregate ~ 10.
3/ Choose R (requests per prefix) in the eviction band; R ~ 8 to 33 keeps both interleaved cells
   evicting and lands inside or near the p90 tail window (10 < R < 40 for N=4). JOBS = P x R.
4/ Record the MEASURED per-replica working set (P/N)x L against C for every cell in the report, so a
   reader can verify which cells actually evicted rather than trusting the sizing.

Worked example (pin to a 24 GB card; suppose the live read gives num_gpu_blocks = 2,000 -> C = 32,000
tokens; choose L = 300 via PREFIX_REPS ~ 10):
- aggregate target 10 x C = 320,000 -> P = ceil(320000 / 300) ~ 1,067; round to a multiple of N, say
  P = 1,068. Per-replica EPP-interleaved = (1068/4) x 300 = 80,100 ~ 2.5 x C (evicts, good).
- R = 12 -> JOBS = P x R ~ 12,816. (Reduce P, or R, if that exceeds the session budget; keep
  per-replica > 2 x C.) These numbers are ILLUSTRATIVE: the real ones come from the live read.

## 5. Pre-registered predictions (formulas; numbers resolve after the live read)

Validated cold-start model (loadtest/results/publication/cache_derivation.md), with R = JOBS / P
derived from run counters (NON-circular, never from the hit rate):

| Cell | Router | Admission | Predicted prefix-cache hit rate |
|---|---|---|---|
| A | EPP | GROUPED     | ~ 1 - P/JOBS  (each prefix cold once; the cold-miss floor) |
| B | EPP | INTERLEAVED  | ~ 1 - P/JOBS if (P/N)L < C, LOWER if it overflows (eviction) |
| C | RR  | GROUPED     | ~ 1 - (P x N)/JOBS  (each prefix warmed on all N replicas) |
| D | RR  | INTERLEAVED  | collapses when P x L > C (thrash) |

Interaction is (A-B) vs (C-D); the sign is NOT pre-committed (standard 4). The direction-free anchor:
cell A attains the cold-miss floor (P cold events); confirm cell B did not tie A by fitting (that
equality would itself be the A-versus-B result). Commit the resolved numbers to predictions.json
BEFORE running, then compare measured against predicted per cell (an out-of-sample validation of the
model in a regime it was not fitted on).

## 6. One-number resolution checklist (first cluster action)

1/ Pin the batch tier to one instance type; wait for 4 Ready replicas.
2/ Read `num_gpu_blocks` on one replica; confirm all four match. C = num_gpu_blocks x 16.
3/ Plug C into section 4 to get P, L (PREFIX_REPS), R, JOBS.
4/ Write predictions.json (section 5 formulas evaluated at those P, JOBS).
5/ `P=<> JOBS=<> PREFIX_REPS=<> bash loadtest/run_lt8.sh`.
