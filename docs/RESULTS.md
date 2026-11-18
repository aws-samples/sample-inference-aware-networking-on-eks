# Measurements and design detail

This is sample code, for non-production usage. These results come from the implementation in this
repository, measured on Amazon EKS with the Qwen3 model series unless a section says otherwise.

The AWS Prescriptive Guidance pattern for this repository summarizes the key results and links here
for the full methods, every measured cell, and the caveats.

## Benchmark methodology

All primary measurements use `Qwen/Qwen3-8B` at its pinned revision, the vLLM 0.24.0 GPU container, and a non-thinking chat template mounted, and held byte-equal to its ConfigMap by INV20. Prefix caching is on, each fleet has four replicas, the load driver runs inside the cluster, and every cell ran twice. These were single runs: both durability tests, the engine-direct cancellation figure, and the relay-path cancellation figure.

How the measurements were taken:

- Requests per prefix is always derived from observed request counts divided by distinct prefixes, read from the run counters. It is never back-solved from the measured hit rate, so the model validation is not circular.
- Both routing arms enter through the same gateway. One goes to the `InferencePool` with its Endpoint Picker, and one to a plain Service with cache-blind load balancing, so the only variable is the scoring.
- The round-robin baseline is verified uniform rather than assumed, at 0.25 of requests per replica across four replicas.
- Co-location is measured from per-replica metric deltas per group, not from per-request access-log traces, because streaming through the gateway does not expose the backend pod to the client.
- Steady state is 300 s on NVIDIA accelerators and 240 s on Neuron. The 240 s window is 16 scrape intervals, which is below the 20-interval target for a stable Prometheus series.
- Deltas are stated within one accelerator type only. NVIDIA and Neuron latencies are never compared.
- Every p90 gap is stated as a reduction, consistently. Restating the same two numbers the other way gives a different percentage without any new measurement.

## The closed-form model

The main result is a closed-form model of the hit-rate gap. Cache-aware routing eliminates redundant cold-start cache replication. Round-robin pays the prefill cost of each prefix once per replica, while co-located routing pays it once.

With `N` replicas, `P` distinct prefixes, and `R` requests per prefix, the cache-aware hit rate is `1 - 1/R` and the round-robin hit rate is `1 - N/R`. The gap is `(N - 1) / R`.

Cache-aware routing has two benefits, and each needs different conditions:

- Compute saved is the hit-rate gap, which is prefill work avoided, so it is throughput headroom and therefore cost. It is present under cold-start replication and under eviction. It is permanent under prefix churn and under eviction, and it amortizes toward zero only for a fixed working set reused indefinitely.
- Tail-latency reduction exists only when the co-located miss fraction is below the percentile threshold and the round-robin miss fraction is above it. In the cold-start regime the miss fractions are `1/R` and `N/R`, which reduces to a window of `10 < R < 10N` at p90. Under eviction the round-robin miss is eviction driven and far above `N/R`, measured 0.589 against an `N/R` of 0.133 in the earlier series. The general form is therefore required, and the requests-per-prefix window is only the cold-start special case.

The window model is binary: it predicts whether a tail benefit exists, not its size, so it cannot distinguish the cold-start reduction from the eviction reduction. The hit-rate model is quantitative.

As a sizing rule, the expected hit-rate gap is `(N - 1) / R`. At `N` of 4 and the cold-start regime's measured `R` of 36.1 it predicts 0.083 against 0.071 measured. Treat it as a sizing estimate rather than a promise about latency. At that same `R`, the predicted gap grows to about 0.19 at `N` of 8 and about 0.42 at `N` of 16. These are predictions and not measurements, and they are upper bounds, because adding replicas in practice accompanies more load, which raises `R` and partly offsets the gain.

## Measured cache capacity

All values read from block count times block size at first activity after readiness, on all four replicas. Two independent witnesses agreed exactly on the L40S figure: the metrics label and the vLLM startup line.

| Model | Accelerator | Capacity (tokens) | Blocks x block size |
|---|---|---|---|
| Qwen3-8B | NVIDIA L40S | 170,320 | 10,645 x 16 |
| Qwen3-8B | NVIDIA L4 | 24,064 | 1,504 x 16 |
| Qwen3-1.7B | NVIDIA L4 | 149,376 | 9,336 x 16 |
| Qwen3-32B-AWQ | NVIDIA L40S | 82,432 | 5,152 x 16 |

For contrast, the earlier Llama series read 195,264 tokens on an L40S, and the AWS Inferentia2 configuration read exactly 16,416 tokens. The pre-registered L4 prediction was about 25,018 tokens, so the derivation was 3.8 percent high. Every overflow ratio in the ramp experiment uses the measured 24,064 rather than the predicted value.

## Routing, four regimes

Experiment 3, all 16 cells. The model column is the closed-form prediction.

| Regime | Cache-aware hit | model | Round-robin hit | model | R, cache-aware | R, round-robin | p90, cache-aware against round-robin |
|---|---|---|---|---|---|---|---|
| cold start, P=42 | 0.9723 | 0.972 | 0.9012 | 0.889 | 36.1 | 36.1 | 23.8 percent lower on mean p90 (3.4 and 37.3 per repetition) |
| control, P=5 | 0.9976 | 0.997 | 0.9975 | 0.987 | 308.3 | 310.2 | 6.4 percent worse |
| eviction, P=158 | 0.9658 | 0.969 | 0.4207 | 0.862 | 31.9 | 29.0 | 65 percent lower |
| churn, reuse 8 | 0.8358 | 0.875 | 0.5219 | 0.500 | 8.0 | 8.0 | 8.7 percent lower |

The two routing arms did not always complete the same number of requests, so `R` is reported per arm, each derived from its own arm's run counters. Every model value is computed against the `R` beside it. The arms tie at 36.1 in cold start and at 8.0 in churn. They separate in control (308.3 against 310.2) and in eviction (31.9 against 29.0). Only the eviction separation changes a printed model value.

Eviction produced the largest effect. Cache-aware routing holds the cold-start line while round-robin falls 0.441 below its own `1 - N/R` of 0.862, and that shortfall is eviction. The earlier Llama series measured 0.969 against 0.411 in the same regime, so the four-regime structure reproduces across model families.

Two caveats apply to these numbers:

1. The cold-start tail benefit was 3.4 percent in repetition 1 and 37.3 percent in repetition 2. The 23.8 percent figure is the reduction between the two arms' mean p90 values, and it must not be cited as the tail benefit. Two repetitions cannot separate a real effect of varying size from p90 noise.
2. In the control regime, cache-aware routing is slower in both repetitions, because at 5 prefixes with co-location at 1.0 one of four replicas takes a zero share. Three replicas then carry the load against round-robin's four.

The eviction and control cells, in summary form:

| Metric | Round-robin | Cache-aware | Change |
|---|---|---|---|
| Prefix-cache hit rate (eviction, P=158, 4 x L40S) | 0.421 | 0.966 | plus 0.545 |
| p90 time to first token | 0.231 s | 0.081 s | 65 percent lower |
| Generation throughput | 293 tokens/s | 322 tokens/s | 10 percent higher |
| Control p90, repetitions 1 and 2 (P=5) | 0.0755 s and 0.0751 s | 0.0805 s and 0.0798 s | about 6 percent slower |

## Overflow ramp

Experiment 4, all 32 cells, cache capacity 24,064 tokens on four NVIDIA L4 replicas. Values are prefix-cache hit rates.

| Overflow ratio | Cache-aware, interleaved | Round-robin, interleaved | Cache-aware, grouped | Round-robin, grouped |
|---|---|---|---|---|
| 0.8 | 0.3394 | 0.2056 | 0.8871 | 0.6630 |
| 1.0 | 0.1116 | 0.1079 | 0.8938 | 0.6643 |
| 1.2 | 0.0566 | 0.0233 | 0.8892 | 0.6618 |
| 1.6 | 0.0081 | 0.0060 | 0.9017 | 0.6677 |

The grouped arms hold flat across the whole ramp. The grouped round-robin arm sits at `1 - N/R` of 0.667, which contradicted this series' own pre-registered prediction. The prediction said the arm would sit at the single-copy cold-miss floor, applying a single-copy floor to an arm that replicates every prefix across all replicas. Grouping alone recovers most of the benefit without cache-aware routing.

The shape of the interleaved decline is unresolved, so claim neither a smooth curve nor a step. A dense seven-point sweep from 0.90 to 1.40, two repetitions each, measured mean within-ratio spread 0.0221 against mean between-ratio step 0.0336. Signal-to-noise was therefore 1.52, and the decline is non-monotonic. Overlap with the committed ramp is good and sits inside the within-ratio spread. Settling the shape needs more repetitions per point, not more points.

## The queue path

These runs used the same operating point, driven through the shipped asynchronous queue path with 8 workers and a receive batch size of 1.

| Mode | Hit rate | End-to-end p50 | End-to-end p90 |
|---|---|---|---|
| grouped | 0.9022 | 5 s | 9 s |
| interleaved | 0.5554 | 183 s | 321 s |

Under interleaved cache thrash the queue path costs 36.6 times more end-to-end latency at p50. Most of that is queue wait rather than generation, which only the queue path can observe. The earlier Llama series measured roughly 60 times, with the same mechanism and a different constant. The grouped arm agrees closely with direct driving (0.9022 against 0.8938).

Exactly-once was measured again on this series. In each of four cells, 624 claims produced 624 real accelerator runs, with zero infrastructure rejections, dead-letter queue depth zero, and all 624 jobs terminal and complete. Scope: this is the uninterrupted path. No worker was killed, so it says nothing about orphan recovery.

## Durability

Backend-path outage: 40 jobs in flight, the batch inference path down for 900 s, then restored. That 900 s matches the Amazon SQS visibility timeout, so the outage forces a full redelivery cycle.

| Quantity | Value |
|---|---|
| Jobs complete | 40 of 40 |
| Claim attempts | 80 |
| Infrastructure rejections | 40 |
| Real accelerator runs | 40, exactly the number submitted |
| Time to drain after restore | 917 s |
| Dead-letter queue messages attributable to this run | 0 |

The outage harness had a reproducible defect. It read dead-letter queue depth absolutely rather than as a delta from a pre-run baseline, so a pre-existing message was attributed to the outage under test. One such message existed, from a diagnostic job submitted manually the previous day, and the harness declared the durability horizon crossed. Zero messages from the run itself reached the dead-letter queue.

Worker kill: 300 jobs, with 3 of 6 worker pods force-deleted mid-generation. Timers were shortened but kept in the production chain's ordering (25 s lease, 60 s visibility, 120 s sweeper grace). A killed job's claim therefore lapsed inside the test window.

| Quantity | Value |
|---|---|
| Distinct jobs complete | 300 of 300 |
| Total accelerator runs (sum of durable claim counts) | 309 |
| Extra runs beyond the job count | 9, all re-claims of interrupted jobs |
| Recovered by the sweeper | 6, message-less orphans |
| Recovered by Amazon SQS redelivery | 3, message still live |
| Dead-letter queue | 0 |

The orphan rate is six per 300, not the one per 300 an earlier run suggested, so the sweeper recovers more jobs than that run implied. Those six had no Amazon SQS message left, so nothing else would ever have re-driven them.

Earlier series, for contrast: 3,000 duplicate submissions of 1,000 distinct jobs produced exactly 1,000 accelerator runs with zero lost, re-asserted from durable claim counts. Tier isolation held at interactive p95 time to first token of 76 ms baseline against 76 ms under a sustained batch flood, a ratio of 1.00. That run placed four batch pods on one node and four interactive pods on four separate nodes. Those 76 ms figures were measured from inside the Region, and there is no Qwen3 absolute equivalent.

## Generation rate and the promotion deadline

Measured at concurrency 1, greedy decoding, end-of-sequence ignored, four output sizes, linearity checked.

| Model | L40S tokens/s | L4 tokens/s | Ratio |
|---|---|---|---|
| Llama-3.1-8B | 47.0 | 16.8 | 2.80 |
| Qwen3-8B | 45.92 | 16.53 | 2.78 |

A promotion therefore lands on hardware nearly three times slower. An earlier design assumed equal hardware, and the measurement that contradicts it reproduces on a second model family. The arithmetic: a request promoted at `P` seconds frees up to `600 - P` seconds of interactive occupancy. It creates `2.778P` seconds of batch generation plus about 0.95 s of tax and slot release. Break-even is therefore `600 - P > 2.778P + 0.95`, which caps `P` at 158.6 s. The committed 120 s holds at a margin of 1.32. Above 158.6 s a promotion still completes but costs more accelerator time than it frees. Above roughly 216 s the regeneration no longer fits the 600 s route timeout and stops completing at all. The promotion floor is about 5,510 output tokens against a standard-tier context of 8,192.

Premium envelope, measured rather than derived: Qwen3-32B-AWQ generated 8,000 completion tokens in 222 s on one L40S at concurrency 1, which is 36.04 tokens per second. A second run reproduced the rate. A premium request therefore promotes above roughly 4,324 generated tokens, and a promoted job completes only below roughly 21,622, while the tier admits 32,768 tokens of context. Requests in that band promote durably and then cannot finish.

## Cancellation

- Engine-direct: 0.10 to 0.16 s across 4 of 4 cells, at 20 ms poll resolution. The figure was measured from inside a serving pod over a loopback connection, so it bypasses both the entry service and Envoy. The pod and tier were not recorded.
- Full entry-service path: 0.7 s on a single premium request, polled every half second.
- The two are different instruments measuring different spans. Neither can be read as a speedup over the earlier Llama figure, which came from a third, coarser instrument.
- Slot release under Qwen3: disconnect cancellation still frees the sequence slot on the standard interactive tier, with the baseline restored inside the operative 5 s bound. The measured 2,850 ms is at the probe's own sample resolution of about 2.6 s, so it means "freed within one polling interval". It is not comparable with the sub-second figures taken by a finer method. Concurrency 1 on a single replica: a floor, not a bound under load.

## Checking answer quality before you route to a cheaper tier

This sample does not measure the answer quality of capability routing. Before you route traffic to a lower-cost tier, sample roughly 100 prompts from your own traffic, serve them on that tier, and evaluate them against your own quality bar.

Certifying that under 5 percent of a tier's answers fail your bar, at 95 percent confidence, needs at least 59 judged answers even with zero failures. So 59 is a minimum sample size, not a passing score.

Report the calls an evaluation excludes, because exclusions change the result. Transport failure, budget exhaustion, and a genuinely unusable reply are three different things and must be three different categories.

## Cost and time

- The verification run on 2026-09-26 took 27.2 minutes and 0.71 accelerator instance-hours, about 1.57 USD at us-west-2 On-Demand prices (per-instance hours and cost in `loadtest/results/spec002-walkthrough/RESULT.md`). It covered `make preflight`, `make dry-run`, `make stage-1` through `make stage-3` with their tests, `make observability-scalers`, and `make verify-invariants`. The run did not include the add-on install step, which has no committed timing. The run also did not include cluster creation and teardown, so the figure covers the deployment sequence only, not a full cluster lifecycle.
- The benchmark figures used four-replica fleets, which this walkthrough does not provision. Budget for those separately.
- The first three steps cost nothing: creating the virtual environment, running the offline suite, and resolving version pins. Cluster creation onward provisions real capacity.
- Entry-service overhead: about 1.2 ms added to first byte at the median, measured against the gateway with empty pools, so no model was serving. The overhead is reusable across a model change, because the entry service never loads a tokenizer. It grows with prompt length, and the code and deployment have both changed since that measurement and have not been re-measured.
- NVIDIA accelerator utilization, the DCGM window average over the large-working-set publication run: 65.6 percent (`loadtest/results/publication/nvidia_gpu_util.txt`).
- The cost-side benefit of cache-aware routing is throughput rather than latency. In the eviction regime, generation throughput rose from 293 to 322 tokens per second. That is prefill work avoided, which reduces the capacity you need to provision.
- The counter-case carries a capacity cost as well as a latency cost. At 5 distinct prefixes with co-location at 1.0, one of four replicas took a zero share, so three replicas carried the load. The p90 penalty was about 6 percent in both repetitions.
- Arrival shaping is a low-cost lever and needs no routing layer. At an overflow ratio of 1.0 through the queue path, grouping moved end-to-end p50 from 183 s to 5 s.

## Long configuration samples a reader will want

The Epics reference the files below, and each is better read in full from the repository than inlined here:

- the seven-rule HTTPRoute
- the node pool and node class pair for one tier
- the entry-service Service with its four annotations and source ranges
- the timeout-chain ConfigMap
- the conditional claim and the ownership-guarded terminal write in the job store
- the cost safety-net CronJob body

The implementation verifies nine correctness properties independently. Reproduce them as a checklist when you adapt the pattern:

- routing by live state
- exactly-once for non-interrupted jobs
- no job loss
- tier isolation
- elastic scaling on a real demand signal
- deterministic failure handling
- structural invariants
- end-to-end correlation-ID traceability
- observable queue state with a reconcilable asynchronous path

One reconciliation pass covered seven identities across a 300 s window. Peak visible queue depth of 195 drained to zero and all 220 asynchronous jobs completed. The pass recorded zero failures and two instrumentation artifacts, both diagnosed rather than absorbed.

## Design rationale

Four properties shape this architecture. The first two belong to each request, and the HTTP layer sees neither.

Duration drives the timeout chains. They bound how long one request can hold an accelerator slot, and they do not retry it. On the standard tier, a generation above roughly 5,510 output tokens passes the 120 s promotion deadline. The 600 s route timeout exists because requests legitimately run that long.

Placement cost drives the Endpoint Picker. The picker scores candidate replicas on prefix-cache affinity and on live cache and queue metrics, instead of connection counts. On one 158-prefix workload across four replicas, p90 time to first token was 0.081 s co-located versus 0.231 s spread evenly, with the same requests, hardware, and model.

Durability drives the conditional claim. Deferrable work is claimed before any accelerator call, so a duplicate delivery starts no second run. Submission never blocks on accelerator availability.

Caller intent drives the two-label contract. Urgency and capability are independent labels, so a caller does not choose an endpoint per tier. The relay raises or refuses a pairing it cannot serve as asked.

Measured scale-up behavior on a Llama model workload: a queue-depth trigger fired at 11 waiting requests against a threshold of 5, and desired replicas moved from 2 to 3. Trigger-to-Ready took 170 s, dominated by model load. Testing confirmed both scale-down and Karpenter consolidation.

## Further limitations

- Capability routing quality is not measured in this sample. The free signals proxy cost and length, not difficulty. Evaluate the lower-cost tier on your own prompts first.
- The premium tier has a request band that cannot complete. Qwen3-32B-AWQ generated 8,000 completion tokens in 222 s on an L40S at concurrency 1, which is 36.04 tokens per second. A premium request promotes above roughly 4,324 generated tokens, and a promoted job completes only below roughly 21,622, while the tier admits 32,768 tokens of context.
- There are no routing measurements on AWS Inferentia for the Qwen3 family. The Neuron arm closed as characterization after a stage-1 experiment found period-4 multi-slot degeneracy with silent corruption on the legacy Neuron line. The economy class moved from Inferentia2 to NVIDIA L4 on 2026-09-23.
- The Neuron pin freezes vLLM three minor versions back. The SDK 2.28 Deep Learning Container carries vLLM 0.13.0 against 0.16.0 in the SDK 2.31.0 image. NxD Inference dropped Trn1 and Inf2 support at Neuron 2.29.0 and is in maintenance mode.
- Two observability counters under-report. `increase(worker_claims_total)` is pod-local and read 61 against a true 220 across a KEDA scale-down. [Amazon Simple Queue Service (Amazon SQS)](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/welcome.html) `ApproximateNumberOfMessages` is eventually consistent and under-reports for up to about a minute after a large enqueue.
- The relay overhead figure is stale. About 1.2 ms added to first byte at the median, measured on 2026-08-05 as a hop cost against the gateway with empty pools. The relay code and deployment have both changed since, without re-measurement.

## Further workarounds

- To close the premium completion band, lower the admitted context for the premium class to the completable range. Alternatively, raise the batch-side capacity so the promoted generation fits inside the route timeout.
- On Neuron, the KV-cache metric changes meaning. With prefix caching off the contiguous layout makes `vllm:kv_cache_usage_perc` the fraction of occupied sequence slots, which is length invariant. With prefix caching on it is paged KV block utilization, matching NVIDIA semantics. The Endpoint Picker scores on that metric, so the only valid configuration for inference-aware routing on Neuron is prefix caching on.
- On Neuron, the preemption counter never moves. On the contiguous path each block is sized to hold the full model length, so preemption is never needed and the preemption counter can only read zero. Scope any preemption-keyed autoscaler or alarm to the NVIDIA pools.
- On Neuron, NxD Inference loads adapters only at server startup, so it does not support dynamic runtime loading and the dynamic-residency routing problem does not exist on Neuron.

## Further troubleshooting

| Issue | Solution |
|---|---|
| The arming target reports ARMED and the safety net is inert. | The arming step wrote to a different ConfigMap name than the CronJob mounts. Derive the ConfigMap name from the manifest the CronJob mounts, never restate it, and verify by watching the net fire rather than by reading its report. |
| Requests fail with HTTP 404 from vLLM after a model change. | A test fixture hardcoded a stale served-model name. Resolve the served model from the live Deployment rather than hardcoding it. |
| Prefix caching is on, the Endpoint Picker is running, and the hit rate is still low on Inf2. | The cache may be retention starved rather than overflowing. With `max_num_seqs` times `max_model_len` at 16,384 against a total cache of 16,416 tokens, only 32 tokens of retention headroom remain. Prefixes cannot be held past request completion, so hits occur only on temporal overlap. Read `vllm:cache_config_info` and compute the retainable remainder before concluding anything about the router. |
| A model crash-loops in the Neuron compiler on Inf2. | The aggressive configuration (`max-model-len` 4096 with a 1,500-block override) does not compile, and nine restarts were observed. A conservative configuration (`max-model-len` 2048 with a 512-block override) compiles and serves, at the cost of a retention-starved KV cache. |
| The Chain B check fails reporting that the load balancer idle-timeout annotation is present in more than one file. | The idle-timeout annotation must live on exactly one owner. This occurs during an ingress cutover when both the old and the new owner carry it. |
| An Amazon SQS queue reports as non-existent but you can see it in the console. | An omitted Region flag used the CLI default while the queue lived elsewhere. Pass every scoping flag explicitly in any command whose output becomes a claim. |

## Field notes behind the best practices

The pattern states these entries in shorter form. This is their full wording, including the incident each one comes from.

- In the worker HTTP client, retry connect errors only, with read and status retries set to zero. A read retry can double-execute on an accelerator and break exactly-once from inside the client.
- Default the Amazon SQS receive batch size to 1 for long generations. With a batch above 1, messages behind the first wait for up to the full HTTP client timeout. The trade-off is more receive calls.
- Pin external downloads by content as well as by URL. The Karpenter AWS CloudFormation template is pinned by tag and by SHA-256, and verified before every deploy. If the checksum mismatches at a pinned tag, investigate the change before you update the checksum.
- After a rebuild, check the resulting cluster state directly. A rebuild that ran the add-on scripts individually skipped the device plugin, and cost 22 minutes of a paid accelerator idling while a pod sat Pending.
- Make an armed cost guard a hard dependency of every accelerator stage. Four accelerator instances ran for three hours behind a preflight that reported the safety net as present while the net logged itself as disarmed every two minutes.
- Set an explicit retention policy on every CloudWatch log group at creation. A group with no retention bills indefinitely while every list-based sweep reports it as healthy.

| Issue | Solution |
|---|---|
| An accelerator node joins, is healthy, carries the accelerator taint, and the pod still sits Pending on a paid instance. | Nothing advertises the accelerator resource, so the node joins with capacity that no pod can schedule onto. Install the device plugin with `bash infra/addons/install-device-plugins.sh nvidia`, or `neuron` for Inf2, then run `make preflight-bringup`, which checks that a node advertises an allocatable accelerator. |
| `make dry-run` fails reporting that the monitoring CRDs are absent. | Install the monitoring CRDs first with `make observability-crds`. The pod monitor manifests validate against them, so without them a failure has nothing to do with the manifests and a green result would depend on install order. |
| Every answer contains a reasoning block, or a short token budget returns no answer at all. | Qwen3 defaults to thinking on and emits its reasoning inline in the content field with no separate reasoning field, measured identically on Neuron vLLM 0.13.0 and GPU vLLM 0.24.0. A 64-token budget produced zero answer tokens. Mount one non-thinking chat template server side on every serving Deployment, and budget 12 prompt tokens rather than 8, because the template injects an empty reasoning block. `make test-no-thinking-leakage` asserts that generated output carries no reasoning. |
