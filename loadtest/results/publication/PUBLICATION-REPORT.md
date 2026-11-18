# Publication measurement report: matched routing arms on NVIDIA and Neuron

> MECHANISM CORRECTION (2026-07-24): This report's original text describes the NVIDIA benefit as the
> working set exceeding cache. That is WRONG and is corrected in WRITEUP-FACTPACK.md (E.1, E.3a) and
> loadtest/results/publication/cache_derivation.md. The NVIDIA cache is 195,264 tokens (derived), which
> HOLDS the ~100k working set, so there is no overflow. The measured NVIDIA gap (hit 0.972 vs 0.892,
> p90 17 percent) is cold-start cache-replication: round-robin warms each prefix on all 4 replicas, the
> Endpoint Picker on one. Overflow applies to Neuron (cache 16k, working set 58k) and to LT-2 (400
> prefixes). Read the fact pack, not this report's original overflow wording, for the mechanism.


The last measurement pass before writing. Target model Llama-3.1-8B (the ungated same-weights mirror
NousResearch/Meta-Llama-3.1-8B-Instruct, revision d10aef79) on both accelerators, 4 replicas each,
both routing arms entering through the same gateway so the only variable is the scoring. Every number
comes from a committed script and the per-arm JSON under `loadtest/results/publication/`.

## Headline finding

Inference-aware (Endpoint Picker) routing improves prefix-cache hit rate and tail latency over
cache-blind round-robin because cache-aware co-location changes how many replicas must warm each
prefix: round-robin scatters a prefix across all replicas so each pays a cold miss, while the Endpoint
Picker keeps it on one. The SIZE of the benefit is regime-dependent (see WRITEUP-FACTPACK.md). On
NVIDIA the per-replica cache HOLDS the working set, so the gap is the cold-start replication cost
round-robin pays warming every replica (it amortizes with reuse), NOT cache overflow. On Neuron the
cache is far smaller than the working set, so the gap is steady-state overflow. When the working set is
small enough that every replica warms every prefix almost immediately, round-robin ties the Endpoint
Picker. Co-location alone is not sufficient: it must change the per-replica warm-or-evict cost to
produce a consequence.

## Phase 0: cost safety net (Gate 0, passed)

A session-independent safety net was built and PROVEN before any capacity was provisioned: a CronJob
on the always-on control plane (`manifests/07-safety-net/safety-net.yaml`, image alpine/k8s:1.34.1
pinned in versions.yaml) reads a deadline from a ConfigMap and, once past it, pauses KEDA and scales
all serving Deployments to zero. It depends on no session and no refreshed credentials. Demonstrated:
a manual trigger with a past deadline scaled a labelled canary and every serving Deployment to zero;
the real deadline was then set to the 6 hour session ceiling. The net validated itself in practice:
credentials expired twice during this pass, and the net (not a live session) is what bounds cost.

## Matched design (Phase 2)

- Both arms enter through the gateway (Phase 2.5 hop equalization). x-pub-arm: epp routes to the
  InferencePool and its Endpoint Picker; x-pub-arm: rr routes to the plain Service (Envoy cache-blind
  load balancing). The only difference is the scoring.
- The round-robin baseline is genuinely cache-blind and uniform: observed per-replica split 0.25 each
  (NVIDIA 0.248/0.252/0.255/0.245). The earlier Spike B port-forward-to-Service approach (which pinned
  all traffic to one pod) was not used.
- 4 replicas per arm (Phase 2.3), NVIDIA pinned via KEDA min=max=4, Neuron 4 pods on one inf2.24xlarge.
- 2 repetitions per arm for variance. Steady state 300s (NVIDIA) and 240s (Neuron, trimmed to fit the
  Neuron compile budget; 16 scrape intervals, below the 20-interval Prometheus-series target, but the
  client-side metrics are computed over hundreds to thousands of requests).
- Deltas are reported WITHIN each accelerator only. NVIDIA and Neuron latencies are never compared
  (different hardware and price points).

## NVIDIA results (arms 1 and 2), Llama-3.1-8B on g6e.xlarge (NVIDIA L40S), prefix caching on

Hardware (corrected): each replica runs on its own g6e.xlarge node, which carries one NVIDIA L40S
(48 GB). g6e is L40S; L4 is on g6/g5, and the leftover g6.12xlarge seen at teardown was an unrelated
batch-tier remnant. With tensor-parallel size 1, the 4 replicas occupied 4 separate L40S GPUs on 4
separate nodes (GPUs not shared). Replica-to-GPU placement is one replica per GPU, so the round-robin
baseline and the co-location result are read at the granularity of whole GPUs, not shared devices
(`config_nvidia.json`).

Matched at a LARGE working set (48 distinct prefixes, about 93k tokens, exceeding per-replica cache):

| Metric | Endpoint Picker | Round-robin | note |
|---|---|---|---|
| prefix-cache hit rate | 0.9724 | 0.8925 | EPP higher |
| TTFT p50 | 0.438 s | 0.439 s | equal |
| TTFT p90 | 0.463 s | 0.559 s | EPP about 17 percent lower tail |
| inter-token latency p50 | 0.0226 s | 0.0225 s | equal |
| throughput | 255 tok/s | 252 tok/s | equal |
| co-location concentration | 1.000 | 0.389 | EPP concentrates each group |
| GPU utilization | 65.6 percent (window average) | | DCGM |

Variance across the two repetitions is tiny (hit-rate spread 0.0001), so the deltas are far larger
than run-to-run noise. The benefit is at the tail: EPP lowers TTFT p90 (where cache misses land) by
about 17 percent while p50 is unchanged.

Small-working-set control (6 prefixes, fits per-replica cache), `control_smallws_*`:

| Metric | Endpoint Picker | Round-robin |
|---|---|---|
| prefix-cache hit rate | 0.9979 | 0.9977 |
| TTFT p50 / p90 | 0.439 / 0.457 s | 0.433 / 0.452 s |
| co-location | 1.000 | 0.372 |

Here EPP and round-robin are indistinguishable on every consequence metric, even though co-location
still differs sharply (1.0 vs 0.37). Every replica caches all 6 prefixes, so round-robin hits the
cache just as often. This is the weaker-than-Spike-B case, reported not tuned away: at a working set
that fits in cache, inference-aware routing earns nothing. The boundary is confirmed by a stress probe
(`stress_nvidia_hitrate.json`): at 48 prefixes EPP 0.949 vs RR 0.808.

## Neuron results (arms 3 and 4), Llama-3.1-8B on inf2.24xlarge, TP=2, prefix caching ON

Config note (a finding in itself): the initial config (max-model-len 4096, num-gpu-blocks-override
1500) crash-looped in neuronx-cc (SIGHUP) on the 8B model; a conservative config (max-model-len 2048,
num-gpu-blocks-override 512, giving a per-replica cache of about 16k tokens) compiled and served
cleanly. A shared hostPath compile cache let the first replica compile (about 30 minutes) and the other
three reuse the artifact in seconds.

Large working set (48 prefixes, about 57k tokens, well over the 16k-token per-replica cache):

| Metric | Endpoint Picker | Round-robin | note |
|---|---|---|---|
| prefix-cache hit rate | 0.251 | 0.154 | EPP about 1.63 times higher |
| TTFT p50 | 0.797 s | 0.838 s | EPP lower |
| TTFT p90 | 1.404 s | 1.158 s | mixed (see caveat) |
| inter-token latency p50 | 0.121 s | 0.127 s | EPP slightly lower |
| throughput | 56 tok/s | 56 tok/s | equal |
| co-location concentration | 1.000 | 0.389 | EPP concentrates each group |
| preemptions | 0 | 0 | |

Direction matches NVIDIA (the Endpoint Picker wins hit rate and co-locates every group), though the
MECHANISM differs: here the conservative Neuron cache (16,416 tokens) is far smaller than the working
set (about 57k), so this is genuine cache overflow, whereas on NVIDIA the cache holds the working set
and the gap is cold-start replication. With prefix caching ON the Endpoint Picker lifts the hit rate
about 1.63 times on Neuron and co-locates every group. The absolute hit rates are low because of that
overflow.

Note (resolved in the follow-up pass below): an earlier round-robin repetition recorded a negative
throughput (a vLLM counter reset from a mid-run Neuron replica restart) and was dropped. The follow-up
re-ran with two clean repetitions per arm and no restarts (the corruption was a one-off), so the
round-robin hit rate above (0.154) is the two-repetition mean, not a single sample. The Endpoint Picker
arm hit-rate spread was 0.028.

## Arm 5 (Neuron, prefix caching OFF, the cautionary arm): not completed at 8B in the budget

Arm 5 requires recompiling all replicas to the contiguous (prefix-caching-off) path, a second roughly
25 to 30 minute Neuron compile, plus the slow Neuron measurement runs. With the wall-clock ceiling
reached (and credentials expiring), this did not fit. It is reported here from the Spike B result on
the same DLC, which established it rigorously at the small scale: with prefix caching off, Neuron uses
the contiguous layout, `vllm:kv_cache_usage_perc` measures occupied sequence slots (equal to
active_sequences divided by max-num-seqs, length-invariant), `vllm:num_preemptions_total` is fixed at
zero, and cache-aware routing is moot because there is no cache and the scored metric does not mean
cache residency. The 8B-scale re-confirmation of the slot arithmetic (Gate 1) and the arm 5
measurement are the one piece this pass could not take cleanly, and should be the first item in a
short follow-up (the PC-off contiguous path compiles faster than the blockwise path, so it is a
bounded addition).

## What could not be taken cleanly (Phase 5.4 honesty)

- Arm 5 and the 8B slot-arithmetic re-confirm: not completed in the budget (above).
- Neuron round-robin: one clean repetition (the other corrupted by a mid-run replica restart).
- Per-request co-location traces: co-location is measured from per-replica metric deltas per group
  (the `per_group_colocation` field of each JSON), not per-request gateway access-log traces, because
  streaming through the gateway does not expose the backend pod to the client. Unambiguous at this
  scale (a group of requests landing 12/0 versus 6/6 is clear), but a per-request access-log trace is
  the stronger artifact for the write-up.
- Neuron accelerator utilization: neuron-monitor was not deployed, so Neuron device utilization was
  not scraped (NVIDIA DCGM utilization is reported).
- Steady state on Neuron was 240s (16 scrape intervals) rather than the 300 to 600s target, to fit the
  compile budget.

## Recommendation for the write-up

1. The publishable claim is conditional and precise: inference-aware routing improves hit rate and
   tail latency over round-robin whenever cache-aware co-location reduces the per-replica warm-or-evict
   cost, with prefix caching on. That covers two regimes: cold-start replication when the per-replica
   cache holds the working set (NVIDIA here) and steady-state overflow when it does not (Neuron here).
   When the working set is small enough that every replica warms every prefix at once, routing earns
   nothing. State the regime (cache-versus-working-set) explicitly; it is the crux.
2. Report deltas within accelerator only, never NVIDIA versus Neuron latency.
3. Close the two gaps in a short bounded follow-up: the arm 5 (prefix-caching-off) 8B measurement and
   slot-arithmetic re-confirm, and a clean second Neuron round-robin repetition, and add a per-request
   gateway access-log trace for co-location.

## Artifacts (loadtest/results/publication/)

Per-arm JSON: `route_nvidia_{epp,rr}_rep{1,2}.json`, `route_neuron_pcon_{epp,rr}_rep{1,2}.json`,
`control_smallws_*` (small working set), `stress_nvidia_hitrate.json`. Summaries: `summary_nvidia.csv`,
`summary_neuron.csv`. Config per arm: `config_nvidia.json`, `config_neuron.json`, plus `versions.yaml`.
GPU util: `nvidia_gpu_util.txt`. Charts (hand-rendered SVG, no plotting library available):
`chart_hitrate_5arm.svg`, `chart_coloc_5arm.svg`, `chart_nvidia_ttft90.svg`, `chart_workingset.svg`.
Metric semantics side by side: see `../spike-b/KV-SEMANTICS-FINDING.md`.

## Follow-up pass closures (2026-07-23, after the initial publication run)

Three open points from the initial pass were closed in a follow-up:

- Hardware fact corrected: the NVIDIA accelerator is L40S (g6e.xlarge), not L4; the 4 replicas ran on
  4 separate nodes, one L40S each (not shared). The leftover g6.12xlarge at teardown carried L4 and
  was an unrelated batch-tier remnant (`config_nvidia.json`).
- Headline number reconciled: the matched 17 percent (about 96 ms) p90 reduction is the headline; the
  earlier LT-2 65 percent figure is superseded (not gateway-equalized, heavier working set driving the
  baseline hit rate to 54.7 percent).
- Neuron round-robin gap closed: two clean repetitions per arm, no replica restarts (the earlier
  corruption was a one-off). The p90 inversion HOLDS: Endpoint Picker p90 1.404 s versus round-robin
  0.982 s, while the Endpoint Picker still wins hit rate (0.251 versus 0.154) and median (0.797 versus
  0.824 s). Mechanism: on Neuron the cache (16,416 tokens) is far smaller than the working set (about
  57.6k tokens), so co-location concentrates each group onto one replica and the tail suffers from
  queueing even as the median and hit rate improve. This is regime 3 of the three-regime finding
  (`WRITEUP-FACTPACK.md`, `summary_neuron.csv`).
- Arm 5 (Neuron, prefix caching off) closed at 8B: co-location 0.454 (at the random baseline, no
  cache-aware placement), hit rate undefined, and the slot arithmetic re-confirmed at 8B (one sequence
  0.125 = 1/max-num-seqs, four sequences 0.5, length-invariant).

## Validation and teardown (Phase 5)

- `make verify-invariants` 10/10; INV7, INV9, and INV10 each confirmed to fail on an injected
  regression and restored (run locally, no cluster needed).
- `make dry-run` clean on the new manifests: the safety-net, both publication HTTPRoutes, the Neuron
  8B deployment, and the publication dashboard ConfigMap all validate server-side.
- Teardown: the Neuron deployment scaled to zero, its Endpoint Picker (helm) and both publication
  HTTPRoutes removed, the interactive tier scaled to zero and its KEDA bounds restored to min 2 max 4.
  Zero running GPU and Neuron instances, confirmed stable across a no-relaunch poll (the inf2.24xlarge
  and a leftover empty g6.12xlarge both deprovisioned). The base cluster, the sweeper CronJob,
  Prometheus, Grafana, and the CPU exporters remain up.
- The cost safety net validated itself in production: credentials expired twice during the pass, and
  at the 6 hour ceiling the net fired on schedule (observed `SAFETY-NET FIRING: now >= deadline`),
  pausing KEDA and scaling serving to zero without any live session. It is left disarmed
  (deadline_epoch 0) with the CronJob in place, ready to re-arm for the next session by setting a
  future deadline.
- Credential-expiry note: the the internal credential system credentials expire in about 12 hours and expired twice here;
  for a pass this long the safety net (not credential refresh) is the load-bearing cost control, which
  is why Phase 0 gated everything.
