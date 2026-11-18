# Spike B, Phase 2: what vllm:kv_cache_usage_perc measures on Neuron

The Endpoint Picker scores backends on `vllm:kv_cache_usage_perc`. This finding establishes,
empirically on the running inf2 pod, what that metric actually measures on the AWS Neuron fork in
each prefix-caching flag state. The probe (`loadtest/spike_b_kv_probe.py`) drives three conditions
and samples the metric while requests are in flight, taking the peak reading per condition:

- A: one short sequence (1 slot, about 5 tokens of context).
- B: one long sequence (1 slot, about 1260 tokens of context). Compared to A, this is the LENGTH test.
- C: four short sequences (= max_num_seqs, several slots). Compared to A, this is the COUNT test.

The discriminator: if the metric moves with sequence COUNT but not with sequence LENGTH, it is
measuring occupied sequence slots, not memory pressure.

## The metric name on the fork

Confirmed present under the upstream name: `vllm:kv_cache_usage_perc` (label `model_name`), alongside
`vllm:num_requests_running`, `vllm:num_requests_waiting`, `vllm:num_preemptions_total`,
`vllm:prefix_cache_hits_total`, and `vllm:prefix_cache_queries_total`. Full inventory (91 `vllm:`
families) in `neuron_metrics_inventory.txt`. No name translation is needed for the scored metric.

## Prefix caching OFF (contiguous layout): the metric measures occupied sequence SLOTS

Config confirmed from `vllm:cache_config_info` on the running pod: `enable_prefix_caching=False`,
`block_size=2048` (equal to max_model_len), `num_gpu_blocks=5`, `num_gpu_blocks_override=5`,
`_neuron_null_block_adjusted=True` (the plugin adjusted the requested 4 to 5 by adding one null
block). This is the contiguous, one-block-per-sequence layout.

Measured (`kv_probe_pc_off.json`):

| Condition | sequences | context tokens | peak kv_cache_usage_perc | peak running | preemptions |
|---|---|---|---|---|---|
| A one short | 1 | ~5 | 0.25 | 1 | 0 |
| B one long | 1 | ~1260 | 0.25 | 1 | 0 |
| C many short | 4 | ~5 each | 1.00 | 4 | 0 |

Reading:

- LENGTH (A to B): context grows about 250 times, the metric does not move at all (0.25 to 0.25).
- COUNT (A to C): sequences go from 1 to 4, the metric jumps exactly 4 times (0.25 to 1.00), which
  is active_sequences / max_num_seqs (1/4 and 4/4).

Conclusion: with prefix caching OFF, `vllm:kv_cache_usage_perc` on Neuron measures the fraction of
occupied sequence slots, not KV memory pressure. A 1260-token sequence and a 5-token sequence are
indistinguishable to the metric; four trivial sequences read as fully utilized. This is the opposite
of the NVIDIA meaning, where the same metric is the fraction of paged KV blocks in use and rises
with the total tokens resident.

## Preemption counter on the contiguous path: dead (reads zero)

In the same run, `vllm:num_preemptions_total` stayed 0 across every condition, including condition C
at full slots. On the contiguous path NxD Inference sizes each block to hold max_model_len, so a
sequence is never preempted for KV space and the plugin disables the scheduler's preemption checks.
Any KEDA scaler or alarm keyed on `vllm:num_preemptions_total` for a Neuron contiguous pool sits
green regardless of load. Scope preemption-based signals to the NVIDIA pools only.

## Prefix caching ON (blockwise layout): the metric tracks KV MEMORY, as on NVIDIA

Config confirmed from `vllm:cache_config_info` on the running pod: `enable_prefix_caching=True`,
`block_size=32`, `num_gpu_blocks=257`, `num_gpu_blocks_override=257`. This is the paged, blockwise
layout (many small blocks shared across sequences), not one block per sequence.

Measured (`kv_probe_pc_on.json`):

| Condition | sequences | context tokens | peak kv_cache_usage_perc | peak running | preemptions |
|---|---|---|---|---|---|
| A one short | 1 | ~5 | 0.031 | 1 | 0 |
| B one long | 1 | ~1260 | 0.231 | 1 | 0 |
| C many short | 4 | ~5 each | 0.141 | 4 | 0 |

Reading:

- LENGTH (A to B): context grows about 250 times and the metric rises about 7.4 times (0.031 to
  0.231). The metric is strongly length sensitive, the opposite of the contiguous path.
- COUNT (A to C): sequences go from 1 to 4 and the metric rises about 4.5 times (0.031 to 0.141),
  because each sequence still allocates blocks.
- The decisive comparison: B (one long) 0.231 is GREATER than C (four short) 0.141. A single long
  sequence occupies more KV cache than four trivial ones. Under a slot-count metric the opposite
  would hold (one sequence is one slot, four sequences are four). This is memory-pressure semantics.

The readings match paged-block accounting: allocated_blocks = ceil(total_resident_tokens /
block_size), divided by num_gpu_blocks (257). A about 8 blocks, C about 36 blocks (4 sequences of
about 9), B about 59 blocks (about 1900 tokens): usage rises with total resident tokens across all
sequences, exactly the NVIDIA meaning.

Conclusion: with prefix caching ON, `vllm:kv_cache_usage_perc` on Neuron measures paged KV block
utilization, tracking total resident tokens (memory pressure), the same quantity the Endpoint Picker
assumes. Preemptions remained 0 in this run because 257 blocks were never exhausted at these loads;
the preemption counter is live on the blockwise path in principle (the plugin does not disable it as
it does on the contiguous path), but this pass did not drive it above zero.

## Side-by-side: the same metric on two accelerators

| Signal | NVIDIA (paged) | Neuron prefix caching OFF (contiguous) | Neuron prefix caching ON (blockwise) |
|---|---|---|---|
| vllm:kv_cache_usage_perc | fraction of paged KV blocks in use, rises with total resident tokens (memory pressure) | fraction of occupied sequence slots, independent of sequence length (count, not memory) | fraction of paged KV blocks in use, rises with total resident tokens (memory pressure), same as NVIDIA |
| vllm:num_preemptions_total | increments under real KV pressure | disabled, always 0 | live in principle (not disabled), read 0 in this pass because blocks were not exhausted |
| length discriminator (A one short to B one long) | rises with length | 0.25 to 0.25, no change | 0.031 to 0.231, rises 7.4 times |
| count vs length (B one long vs C four short) | one long can exceed several short | B 0.25 == C 1.00 is count-only (C higher) | B 0.231 greater than C 0.141 (memory, one long exceeds four short) |

## Why this matters for routing

The Endpoint Picker's cache-aware score treats `vllm:kv_cache_usage_perc` as memory pressure. On the
Neuron contiguous path that assumption is false: the score reflects how many slots are busy, not how
much KV memory (and therefore prefix-cache residency) a backend holds. A router scoring this metric
on the contiguous path is not merely uninformed, it is scoring a quantity that does not carry the
signal it assumes. Phase 3 measures the routing consequence directly.
