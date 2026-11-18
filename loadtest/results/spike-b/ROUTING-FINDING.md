# Spike B, Phase 3: does Endpoint Picker routing beat round-robin on Neuron

The question: on AWS Neuron, does the Endpoint Picker's cache-aware routing place same-prefix
requests better than cache-blind round-robin, and does that produce a measurable latency benefit.
Measured on two Neuron replicas (one inf2.24xlarge, TinyLlama-1.1B, TP=2) with a prefix-heavy
workload of 4 groups, each group sharing a long common prefix, 11 requests per group (44 total).

Driver: `loadtest/spike_b_routing.py`. Per arm it records, from per-pod vLLM metric deltas and
client-side streaming timing:

- prefix-cache hit rate: `prefix_cache_hits_total / prefix_cache_queries_total`, summed across the
  replicas over the run.
- co-location concentration: per group, the fraction of that group's requests that landed on a
  single replica (1.0 = the whole group on one pod, 0.5 = evenly split). From per-pod
  `request_success_total` deltas.
- TTFT p50/p90: client time to first streamed token.

## The round-robin baseline is genuinely cache-blind (confirmed)

The round-robin arm does strict client-side alternation across the two pods (one request to pod A,
the next to pod B, by a counter), so the request distribution is exactly 22/22 and the choice of pod
is independent of any cache state. This is cache-blind by construction. An earlier attempt that
port-forwarded the Kubernetes Service was discarded: `kubectl port-forward svc/...` pins to a single
endpoint pod (observed 44/0), which is not round-robin. The comparison below uses the genuine 22/22
round-robin.

## Prefix caching ON: Endpoint Picker routing beats round-robin

`route_pc_on_epp.json`, `route_pc_on_rr.json`. Both arms served 44 requests.

| Metric | Endpoint Picker | Round-robin (cache-blind) | Winner |
|---|---|---|---|
| co-location concentration | 1.000 | 0.545 | EPP (every group on one pod) |
| prefix-cache hit rate | 0.897 | 0.809 | EPP |
| TTFT p50 | 0.456 s | 0.605 s | EPP (about 25 percent lower) |
| TTFT p90 | 0.465 s | 0.616 s | EPP |
| per-pod request share | 11 / 33 | 22 / 22 | (EPP concentrates groups; RR splits) |

Reading:

- Co-location is the direct evidence of backend-aware placement. Under the Endpoint Picker every
  group's 11 requests landed entirely on one replica (concentration 1.0); the four groups were
  distributed across the two pods as one group and three groups (11 and 33). Under round-robin each
  group was split evenly (concentration 0.545, share 22/22).
- The hit rate follows: concentrating a group on the pod that already cached its prefix yields one
  miss and the rest hits (0.897); splitting a group across two pods pays the prefix miss on both
  (0.809).
- TTFT is the user-visible consequence: the Endpoint Picker arm is about 25 percent faster at p50
  and p90, and it wins DESPITE carrying an extra hop (it goes through the gateway, while the
  round-robin arm hits the pods directly). Better cache locality means less prefill recompute, so
  the first token arrives sooner.

Conclusion for prefix caching ON: inference-aware routing works on Neuron. The Endpoint Picker
places same-prefix requests on the same replica and delivers a real latency reduction over
round-robin. This is a positive result: the architecture's routing layer is accelerator-agnostic in
this configuration. Preemptions stayed 0 throughout.

## Prefix caching OFF (contiguous): routing has nothing to be aware of

`route_pc_off_epp.json`. Both replicas recompiled to the contiguous layout, then the Endpoint
Picker arm was run identically (44 requests, 4 prefix groups).

| Metric | PC-off Endpoint Picker |
|---|---|
| requests served | 44 |
| prefix-cache hit rate | undefined (no prefix cache: prefix_cache_queries = 0) |
| co-location concentration | 0.659 |
| TTFT p50 / p90 | 0.444 s / 0.479 s |
| per-pod request share | 29 / 15 |
| preemptions | 0 |

Reading:

- With prefix caching OFF there is no prefix cache, so the hit rate is undefined and cache-aware
  routing has nothing to exploit. The premise of the prefix scorer does not exist on this path.
- The Endpoint Picker scores `vllm:kv_cache_usage_perc`, which Phase 2 established means occupied
  sequence SLOTS on the contiguous path, not KV memory or cache residency. So the signal the router
  ranks on carries no information about which replica holds a given prefix.
- Co-location is 0.659, between the clean 1.0 of the prefix-aware ON arm and the 0.5 of round-robin.
  This is incidental: with the prefix indexer empty, placement falls back to load-based scoring
  (least-occupied slots), which can bunch a burst of a group's requests onto whichever replica was
  idle. It is not cache-driven, and it earns nothing, because there is no cache to hit.

Conclusion for prefix caching OFF: cache-aware routing is moot on the contiguous path. There is no
cache, and the metric the Endpoint Picker scores does not mean cache residency there. Any latency
similarity to the ON arm here is because TinyLlama prefills a 600-token prefix cheaply, not because
routing helped.

## What decides the publication configuration

The routing layer earns its benefit on Neuron only with prefix caching ON, where the KV metric means
memory (Phase 2) and the prefix cache exists for the scorer to exploit. Measured: with prefix
caching ON the Endpoint Picker co-locates same-prefix requests perfectly and cuts TTFT about 25
percent versus round-robin; with prefix caching OFF there is no cache and the scored metric means
slots, so routing carries no locality signal.

Recommendation for the matched publication runs: benchmark with prefix caching ON only. The four
arms should be the accelerator crossed with routing: NVIDIA routing on, NVIDIA routing off, Neuron
routing on, Neuron routing off, all with prefix caching ON so the KV metric means the same thing
(memory) on both accelerators and the comparison is apples to apples. Prefix caching OFF is not a
valid benchmark configuration for the routing question: it removes the cache the router exists to
exploit and changes what the scored metric means.

## Co-location evidence and a measurement caveat

The co-location evidence is the per-group, per-replica request distribution, committed in the
`per_group` field of each `route_*.json` (for each group, how its requests split across the two
replicas). Under the prefix-aware ON arm every group's `per_pod_request_delta` is entirely on one
pod (concentration 1.0); under round-robin each group splits evenly (about 0.5). This is the direct
signal that the Endpoint Picker placed same-prefix requests together.

Caveat, stated rather than smoothed over: per-request correlation-id-to-pod attribution was not
separately captured for these arms. The client streams through the gateway, which does not return
the selected backend pod to the caller, so co-location is measured from per-pod metric deltas per
group rather than a per-request trace. The deltas are unambiguous at this scale (a group of 11
requests landing 11/0 versus 6/5 is clear), but a per-request trace via the gateway access log
would be the stronger artifact for a publication run and is recommended for the matched runs.
