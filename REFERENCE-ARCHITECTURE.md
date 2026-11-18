# Inference-aware networking on Amazon EKS: reference architecture and correctness proof

This document specifies the inference-aware networking reference architecture on Amazon EKS
and proves that it is correct. It is a specification and a correctness proof. Every
quantitative figure is grounded in a committed source file in this repository and cited to
that file. Where this document states a measured result, the arbiter is the committed
`LOAD-TEST-RESULTS.md` and the test that produced it.

## 1. Purpose and scope

The architecture is an inference-aware networking layer on Amazon EKS. It routes large
language model inference requests by live model-server state (key-value cache utilization,
request-queue depth, adapter residency) rather than by connection-level signals such as
round-robin order or active-connection count. The routing decision is a closed-loop function
of current backend GPU state, evaluated once per request.

The architecture guarantees the following correctness properties, each proven in section 3:

- P1. Inference-aware routing routes by live backend state, not by connection-level signals.
- P2. A non-interrupted asynchronous job executes exactly once.
- P3. No submitted job is lost. Interrupted jobs are recovered and therefore run at least once.
- P4. Service-level tiers are isolated: a saturating load on one tier does not degrade another.
- P5. Serving capacity scales elastically on a real demand signal (request-queue depth).
- P6. Failure handling is deterministic: the routing layer rejects requests when the routing
  decision cannot be made, and resumes when it can.
- P7. The configuration is internally consistent, enforced by ten structural invariants.
- P8. Every request is traceable end to end by a correlation id, from submission through the
  queue, the claim, the gateway routing decision, and the serving backend, to completion.
- P9. The real queue state is observable in telemetry, the two request paths are distinguishable in
  telemetry, and the asynchronous path's counts reconcile end to end.

Inference-aware routing (P1) holds on both the synchronous and the asynchronous path: the durable
queue sits in front of the same inference-aware gateway, so queued requests are routed by live
backend state identically to synchronous requests (section 4.4).

Validated scope. The properties in section 3 are verified against a Llama-3.1 8 billion
parameter class model served on NVIDIA g6e nodes in a single AWS region (us-west-2). The
served model and its pinned revision are recorded in `versions.yaml` (`loadtest_model`,
`loadtest_model_revision`). The architecture also specifies an AWS Neuron (inf2) provisioning
path (section 2.5); the correctness measurements in this document are taken on the NVIDIA g6e
path.

## 2. Architecture specification

The system is specified as committed in this repository. Every element below maps to a
committed manifest, script, or version pin.

### 2.1 Request path and the four layers

A request traverses four layers, all on Amazon EKS.

- Layer 1, inference-aware routing. Envoy Gateway with the Envoy AI Gateway extension receives
  the request behind an AWS load balancer and selects the destination endpoint through the
  Gateway API Inference Extension Endpoint Picker (section 2.2).
- Layer 2, durable asynchronous front door. For asynchronous traffic, the request enters an
  Amazon SQS queue with a DynamoDB job-state and idempotency store, rather than holding a
  connection (section 2.3).
- Layer 3, inference engine. vLLM with prefix caching serves the request, one Deployment per
  tier (section 2.4).
- Layer 4, node provisioning. Karpenter provisions the GPU or Neuron node the serving pod runs
  on, in per-tier node pools (section 2.5).

Synchronous path. The client sends an OpenAI-compatible request to `/v1` on the gateway. The
gateway issues an ext_proc callout to the tier's Endpoint Picker, which selects a vLLM endpoint
by live backend state and returns it. The gateway routes the request to that endpoint, vLLM
generates the completion, and the response streams back to the client over the held connection.

Asynchronous path. The client submits a job to the submit API (`async-worker/submit_api.py`),
which computes a deterministic job identity and enqueues one SQS message. A worker
(`async-worker/worker.py`) receives the message, establishes exclusive ownership with a
conditional DynamoDB write (section 2.3), then submits the request to the same inference-aware
gateway the synchronous path uses, carrying the batch tier header
(`x-gateway-inference-objective: batch`). The gateway issues the ext_proc callout to the batch
Endpoint Picker, which selects a batch vLLM endpoint by live backend state, exactly as on the
synchronous path. The worker records the result and terminal state in DynamoDB, and the client
retrieves the result by job identity. The queue sits in front of the inference-aware gateway,
not around it: every request, synchronous or asynchronous, passes through the identical
Endpoint-Picker routing path, and the only difference is the durable queue in front of the
asynchronous one. The queue decouples submission from execution, so a submission never blocks on
GPU availability.

### 2.2 Layer 1: inference-aware routing

This layer is the core of the architecture and is specified precisely.

Gateway stack. The gateway is Envoy Gateway plus the Envoy AI Gateway extension. Envoy Gateway
alone rejects an InferencePool backendRef; the Envoy AI Gateway controller runs an extension
server that reconfigures Envoy Gateway to route to an InferencePool as a backend
(`infra/addons/install-gateway-api-inference.sh`, `DESIGN.md` build reconciliation 1). Pinned
versions: Envoy Gateway `v1.8.2`, Envoy AI Gateway `v1.0.0` (`versions.yaml`).

Routing decision. On each request the gateway issues an ext_proc callout to the tier's Endpoint
Picker. The Endpoint Picker reads live model-server metrics from the vLLM backends (key-value
cache utilization, request-queue depth, adapter residency) exposed on the vLLM metrics endpoint,
scores the candidate endpoints on that state, and returns the selected endpoint, to which the
gateway routes (`DESIGN.md` Layer 1). The decision is therefore closed-loop and per request: it
is a function of the current internal state of the model servers, not of connection-level
signals. This is what distinguishes an inference-aware router from a connection-level load
balancer.

Upstream provenance. The InferencePool and the Endpoint Picker are the upstream Gateway API
Inference Extension, installed from its Helm chart at `v1.5.0` and owned by that chart rather
than hand-applied (`versions.yaml` `gateway_api_inference_extension_chart`, `epp_image`;
`infra/addons/install-gateway-api-inference.sh`; `DESIGN.md` build reconciliation 2). The pool
API is `inference.networking.k8s.io/v1` (GA) (`versions.yaml` `facts.inferencepool_api`).

Service-level tiers. There are two tiers, interactive and batch. Each tier has its own
InferencePool, Endpoint Picker, vLLM Deployment, node pool, and autoscaler. A request selects a
tier through the `x-gateway-inference-objective` header. The HTTPRoute
(`manifests/03-routing/httproute.yaml`) contains a batch rule that matches both the `/v1` path
prefix and the header value `batch` and forwards to the `vllm-llama3-batch` InferencePool, and a
default rule that matches the `/v1` path and forwards to the `vllm-llama3-interactive`
InferencePool. The batch rule matches both the path and the header because Gateway API resolves
precedence by longest path match and then by header-match count, not by list order, so a
header-only batch rule would lose to the interactive path rule and batch traffic would route to
the interactive pool. Both backendRefs are of kind InferencePool in group
`inference.networking.k8s.io` (`manifests/03-routing/httproute.yaml`).

Failure handling. Endpoint selection passes through the ext_proc callout to the Endpoint
Picker. When the Endpoint Picker is unavailable, the callout fails closed and the gateway
rejects the request with a server error; when the Endpoint Picker returns, routing resumes. This
FailClose behavior holds for both tiers on this gateway stack: the Envoy AI Gateway v1.0.0
integration maps InferencePool endpoint selection through an ext_proc filter that does not set
the Envoy failure_mode_allow flag, so an Endpoint Picker outage is a hard rejection rather than a
fallback to blind routing (`DESIGN.md` invariant 6). The failure mode configured per pool is set
through the chart value `inferenceExtension.failureMode`
(`infra/addons/install-gateway-api-inference.sh`).

Retry posture. The inference routes carry no retry policy. The gateway does not re-issue a request
to another backend on a timeout, a 5xx, a reset, or a connect failure. A retry would re-run a
request whose DynamoDB claim already succeeded, producing a duplicate GPU run below the idempotency
layer, so retries are kept off and asserted (`make test-no-gateway-retry`). Because the async
worker treats a gateway 5xx as an infrastructure rejection (it releases the claim and backs off,
section 5) rather than a job failure, an Endpoint Picker outage does not push healthy async jobs to
the DLQ.

### 2.3 Layer 2: the durable asynchronous front door

This layer is specific to this architecture. It provides durable, idempotent, asynchronous
submission that the upstream routing and serving layers do not.

Components. An Amazon SQS queue is the durable buffer, with a dead-letter queue and a redrive
policy (`infra/addons/setup-async-infra.sh`, maxReceiveCount 5). A DynamoDB table
(`inference-jobs`, primary key `job_id`) is the job-state and idempotency store. The worker,
submit API, and reconciler authenticate through EKS Pod Identity (`DESIGN.md` build
reconciliation 4).

Job identity. The submit API derives a job identity from the client identity and a hash of the
request body (`async-worker/jobstore.py`, `job_key`). Two submissions of the same logical job
hash to the same identity and therefore the same DynamoDB item.

Claim protocol. Before any GPU work, a worker claims the job with a single conditional DynamoDB
write (`async-worker/jobstore.py`, `try_claim`). The write succeeds only if no live record
exists for the job identity: either there is no record at all, or the existing record is a prior
attempt whose claim lease has expired. The write establishes `status=processing`, a unique
`claim_token` that proves ownership, a claim lease, a retention lifetime that is independent of
the lease, a durable `claim_count`, and the request payload. Only the worker that wins the
conditional write proceeds to submit the request through the inference-aware gateway
(section 2.2). The claim precedes the gateway call, so the exactly-once guarantee holds
independently of the routing path. A duplicate delivery of the same job finds a live
record and the conditional write fails, so it does not run a second execution. A terminal state
transition (`complete` or `failed`) is itself guarded by the claim token, so a worker whose lease
has lapsed and whose claim has been taken by another worker cannot overwrite the new owner's
state (`async-worker/jobstore.py`, `mark`; proven in `tests/validation/test_ownership_guard.py`).

Reconciler. A job can be stranded in `status=processing` if its worker dies after claiming and
its SQS message is removed by a delivery race, so no redelivery arrives to re-drive it. A
reconciler (`async-worker/sweeper.py`, scheduled by
`manifests/06-sweeper/sweeper-cronjob.yaml`) scans for records that are in `processing` with an
expired lease and that have been stranded longer than a grace period, and re-enqueues an SQS
message carrying the stored payload so a worker re-claims the job. The reconciler acts only on
abandoned jobs: it never touches a `complete` record, and never touches a `processing` record
whose lease is still in the future (a live worker). Its grace period is derived from the queue's
own visibility timeout plus a margin, so it never re-enqueues a job that SQS redelivery would
still recover on its own. A conditional `requeued_at` write with a cooldown makes it idempotent
against itself and against a second reconciler instance. These properties are enforced in code
and asserted in `tests/validation/test_sweeper.py`.

Routing. The worker submits each claimed job to the inference-aware gateway
(`GATEWAY_ENDPOINT`, the in-cluster Envoy gateway) with the batch tier header, not to a vLLM
Service directly. Queued jobs are therefore placed on a backend by the batch Endpoint Picker
using live GPU state, identical to synchronous traffic (section 2.2, proven in section 4). The
gateway's request timeout accommodates a full long generation, so a long batch request completes
rather than being cut off (`manifests/03-routing/httproute.yaml`, request timeout 600s).

Traceability. Every request carries a correlation id end to end. The correlation id is the
deterministic job identity: the submit API stamps it into the SQS message, it is the DynamoDB
primary key, the worker sends it to the gateway as the `x-correlation-id` header, and the gateway
access log records it alongside the selected upstream vLLM pod
(`manifests/03-routing/envoyproxy-accesslog.yaml`). Joining the correlation id across the
DynamoDB item, the gateway access log, and the live pod metadata yields a durable per-request
trace of the full journey (submitted, claimed, backend-selected, completed, and the serving pod,
tier, node, and accelerator), built by `loadtest/build_trace.py`.

Guarantees. A non-interrupted job runs exactly once (P2). No submitted job is lost; an
interrupted job is recovered and runs at least once (P3). The reconciler is the only component
granted DynamoDB Scan, under the least-privilege role `inference-sweeper`
(`async-worker/iam/sweeper-policy.json`, `infra/addons/setup-sweeper.sh`).

### 2.4 Layer 3: the inference engine

vLLM serves inference with prefix caching enabled, one Deployment per tier
(`manifests/02-serving/vllm-interactive.yaml`, `manifests/02-serving/vllm-batch.yaml`). The
serving image is pinned in `versions.yaml` (`vllm_image`). Prefix caching is what makes
inference-aware placement worth doing: an endpoint that already holds a request's prefix in cache
serves it faster, and the routing layer is what places the request on that endpoint (section 4).

### 2.5 Layer 4: node provisioning

Karpenter provisions nodes on demand (install version `1.14.0`, `versions.yaml`
`karpenter_install`). The architecture defines a per-tier NVIDIA g6e path
(`manifests/01-nodepools/ec2nodeclass-gpu.yaml`, `nodepool-interactive.yaml`,
`nodepool-batch.yaml`) and an AWS Neuron inf2 path
(`manifests/01-nodepools/ec2nodeclass-neuron.yaml`, `nodepool-neuron.yaml`). Tiers are isolated
by physically separate node pools: the interactive and batch Deployments schedule onto distinct
NodePools, so the two tiers never share a node. Each NodePool taint has a matching Deployment
toleration (`DESIGN.md` invariant 5).

### 2.6 Components and pinned versions

All versions are pinned in `versions.yaml`, which is the single source of truth. Key pins: EKS
Kubernetes `1.34`; Gateway API Inference Extension chart and Endpoint Picker image `v1.5.0`;
Envoy Gateway `v1.8.2`; Envoy AI Gateway `v1.0.0`; KEDA `v2.20.1`; kube-prometheus-stack
`87.17.0`; NVIDIA device plugin `0.19.3`; the vLLM serving image and the Neuron vLLM DLC as
recorded in `versions.yaml`.

Adopted from upstream, used directly:

- The Gateway API Inference Extension (InferencePool and Endpoint Picker) for inference-aware
  routing.
- vLLM for serving.

Specific to this architecture:

- The durable, idempotent asynchronous front door (Layer 2): the SQS and DynamoDB claim protocol
  and the reconciler.
- The AWS Neuron (inf2) provisioning path.
- The AWS ingress configuration and the per-tier physical isolation.
- The observability of the durable queue: a read-only SQS depth exporter (`sqs-exporter`) and the
  submit-API submission counter, which make the real queue state and the async entry point
  first-class Prometheus signals (P9). These reuse the async-worker runtime and its Python
  dependencies (`prometheus_client`), so they add no new pinned external version.

## 3. Correctness properties and their proofs

Each property states the guarantee, the mechanism that makes it hold, and the committed
verification that proves it.

### P1. Inference-aware routing routes by live backend state

Mechanism. The gateway issues a per-request ext_proc callout to the Endpoint Picker, which
scores candidate endpoints on live vLLM metrics (key-value cache utilization, queue depth,
adapter residency) and returns the selected endpoint (section 2.2). Both the synchronous and the
asynchronous path route through this identical mechanism: the async worker submits each queued
job to the same gateway with the batch tier header (section 2.3). Verification and the full proof
are in section 4, which covers both paths.

### P2. Exactly-once execution for non-interrupted jobs

Mechanism. The conditional DynamoDB claim write (`async-worker/jobstore.py`, `try_claim`) lets
exactly one worker win ownership of a job identity before any GPU call; duplicate deliveries fail
the conditional write and do not execute (section 2.3).

Verification. `tests/validation/test_idempotency.py` submits the same job twice concurrently and
asserts that exactly one GPU execution occurs. Under load, the idempotency scenario submits 1000
distinct jobs three times each, for 3000 submissions, and the durable GPU-run count is exactly
1000, with 1000 jobs complete and zero double-allocation (`LOAD-TEST-RESULTS.md`, LT-3). The
offline ownership guard (`tests/validation/test_ownership_guard.py`) proves that a superseded
worker cannot overwrite the new owner's terminal state.

### P3. No job loss

Mechanism. SQS is a durable buffer; the claim protocol makes redelivery safe; the reconciler
re-drives any job stranded in `processing` whose message is gone (section 2.3).

Verification. The durability scenario submits 300 distinct jobs and kills half the workers
mid-generation. By durable DynamoDB count, all 300 jobs reach `complete` with zero lost. The
total GPU-run count is 305: 295 jobs run exactly once and 5 interrupted jobs run twice, so
295 + 10 = 305, and 5 jobs run more than once. Of those 5 interrupted jobs, the reconciler
recovers 1 (a job whose message was gone, identified by the `requeued_at` attribute that only the
reconciler writes) and SQS redelivery recovers the other 4, so 1 + 4 = 5. The result is
300 of 300 complete, zero lost, with recovery accounted for exactly
(`LOAD-TEST-RESULTS.md`, LT-6; verdict produced by `loadtest/analyze.py` `lt6d`). This is
at-least-once execution for interrupted jobs, which does not weaken P2, since P2 concerns
non-interrupted jobs, which are never re-driven.

### P4. Service-level tier isolation

Mechanism. Physically separate node pools per tier: the interactive and batch Deployments
schedule onto distinct NodePools and never share a node (section 2.5).

Verification. Under a sustained saturating batch flood, the interactive tier's server-side
time-to-first-token at the ninety-fifth percentile is 76 ms in the quiet baseline window and
76 ms under the flood, a ratio of 1.00x, and node labels confirm the batch pods and interactive
pods occupy separate nodes (`LOAD-TEST-RESULTS.md`, LT-4; `tests/validation/test_tier_isolation.py`).

### P5. Elastic scaling on a real demand signal

Mechanism. KEDA scales each vLLM Deployment on request-queue depth read from Prometheus, and
Karpenter provisions the node the new replica needs. The interactive scaler triggers on
`sum(vllm:num_requests_waiting{tier="interactive"})` with a threshold of 5
(`manifests/04-autoscaling/keda-interactive.yaml`).

Verification. When the interactive request-queue depth reaches 11, above the threshold of 5, the
autoscaler raises the desired replica count from 2 to 3, a new replica reaches Ready, and the
tier scales back down and consolidates the node on ramp-down. The trigger-to-Ready latency is
170 s and is dominated by the model load on the newly provisioned node
(`LOAD-TEST-RESULTS.md`, LT-5). The desired count follows from the scaler arithmetic: the ceiling
of 11 divided by the threshold 5 is 3.

### P6. Deterministic failure handling

Mechanism. Endpoint selection passes through the ext_proc callout, which fails closed on
Endpoint Picker unavailability, so the gateway returns a clean server error rather than routing
blind (section 2.2).

Verification. With an Endpoint Picker scaled to zero, probes to its tier return clean server
errors, and when the Endpoint Picker is restored the tier returns to serving. Both tiers exhibit
this FailClose behavior and both recover (`LOAD-TEST-RESULTS.md`, LT-7).

### P7. Structural invariants

Ten cross-manifest invariants keep the configuration internally consistent. They are checked by
`tests/verify_invariants.sh` (`make verify-invariants`) and specified in `DESIGN.md`.

1. Each InferencePool selector matches its vLLM Deployment labels, so a pool selects its own tier.
2. Each InferenceObjective poolRef points at its own tier's pool, so the tiers stay distinct.
3. Each pool's endpointPickerRef names a real deployed Endpoint Picker.
4. Each KEDA scaleTargetRef names a Deployment, never a pool (a pool has no scale subresource).
5. Every NodePool taint has a matching Deployment toleration, so a pod can land on its tier's node.
6. failureMode asserts the delivered behavior: both tiers FailClose on this gateway stack.
7. The header-matched batch HTTPRoute rule matches both the `/v1` path and the batch header and
   precedes the interactive rule, so both pools are reachable and batch traffic is not misrouted.
8. Every component is in the `inference` namespace.
9. The async worker routes through the inference-aware gateway (its `GATEWAY_ENDPOINT` names the
   Envoy gateway, not a vLLM Service) and carries the tier objective header, so queued jobs are
   placed by the Endpoint Picker rather than round-robined.
10. The timeout chain is strictly ordered: route timeout < worker HTTP client timeout < claim lease
    < SQS visibility, so recovery is orderly and one claimed job never double-runs.

### P8. End-to-end correlation-id traceability

Mechanism. Every request carries a correlation id that ties its whole journey together. On the
synchronous path the id travels in the `x-correlation-id` request header. On the asynchronous path
the correlation id IS the deterministic job id (`client_id#sha256(payload)`, computed by the single
shared `jobkey.job_key`): it is present in the SQS message, is the DynamoDB primary key, and is sent
to the gateway as `x-correlation-id` by the worker (`async-worker/worker.py`, `run_inference`). The
Envoy access log records the correlation id against the selected upstream vLLM pod, so the id
resolves to a specific backend. The trace timestamps are captured at their source: the submit API
stamps `submitted_at`, and the worker persists the per-request trace (`submitted_at`,
`backend_endpoint`, `backend_selected_at`, `completed_at`) on the terminal DynamoDB write, guarded by
claim ownership so a superseded worker cannot clobber it. `loadtest/build_trace.py` joins the
correlation id against the access log to resolve the backend pod, tier, node, and accelerator.

Verification. Over a concurrent mixed load, every queued request is traced end to end from
submission through claim, backend selection, and completion, and attributed to a specific backend:
30 of 30 async batch jobs are traced through the gateway, each with its full timestamp chain
(submitted, claimed, backend_selected, completed) and its serving pod, tier, node, and accelerator,
with 0 cross-tier placements (`loadtest/results/phased_trace.md`, `phased_trace.json`, produced by
`loadtest/build_trace.py` and `loadtest/phased_verdict.py`). A job whose worker was killed
mid-generation appears as a single traced row with `runs=2` and `status=complete`, so the durability
path and the routing path are legible together in one record. The correlation id therefore makes
every request, synchronous or asynchronous, traceable from submission to serving backend.

### P9. Observable queue state and a reconcilable async path

Mechanism. The real queue state is a first-class Prometheus signal, not a proxy. A read-only
exporter (`async-worker/sqs_exporter.py`, Deployment `sqs-exporter`, scraped by the `sqs-exporter`
PodMonitor) polls SQS `GetQueueAttributes` under the async-worker service account and exposes
`sqs_queue_messages_visible` (waiting) and `sqs_queue_messages_in_flight` (being processed). The
async entry point is counted at the source: the submit API increments
`submit_api_submissions_total` on each accepted enqueue (scraped by the `submit-api` PodMonitor),
and each GPU run increments the durable DynamoDB `claim_count` and the per-pod `worker_claims_total`
counter. The two request paths are therefore separable in telemetry: the async instruments
(submissions, SQS depth, worker claims) move only for queued traffic, and synchronous traffic
moves only the per-tier vLLM interactive counters.

Verification. Under a concurrent mixed load, with a synchronous-only lead-in followed by an
asynchronous burst, the async-path signals read zero throughout the synchronous-only lead-in (SQS
depth, worker-claims rate, and submissions rate are all zero), which distinguishes the paths in the
telemetry itself. The async counts then reconcile: distinct jobs submitted equals distinct jobs
completed; the durable claim count equals distinct jobs plus re-claims of interrupted jobs; batch
completions (a raw counter delta over the window) equals the durable claim count; per-tier
completions sum to the total; and the queue returns to zero. The queue-depth series is exact at
steady state (the net queue change over the window is zero) and lags a sudden burst by up to about
the exporter poll interval plus SQS convergence, because SQS `ApproximateNumberOfMessages` is
eventually consistent. The authoritative claim count is the durable DynamoDB `claim_count`, which
survives worker scale-down; the per-pod `worker_claims_total` counter under-counts when KEDA
terminates workers mid-window and is a cross-check only. Proven by
`loadtest/verify_reconciliation.py` over the combined-demonstration window, with the depth trend
committed in `loadtest/results/request_flow_depth.csv` and shown on the Request Flow dashboard
(`loadtest/dashboards/build_request_flow.py`, uid `inference-request-flow`).

## 4. Proof that inference-aware routing is correctly implemented

Inference-aware routing is the central claim of the architecture, so it is proven in full here.

### 4.1 The mechanism, end to end

The routing decision is made once per request and is a function of current backend GPU state.

1. The client sends an OpenAI-compatible request to `/v1` on the gateway.
2. The HTTPRoute selects the tier's InferencePool by path and, for batch, by the
   `x-gateway-inference-objective` header (`manifests/03-routing/httproute.yaml`).
3. Envoy Gateway, reconfigured by the Envoy AI Gateway extension to treat the InferencePool as a
   backend, issues an ext_proc callout to the tier's Endpoint Picker
   (`infra/addons/install-gateway-api-inference.sh`, `DESIGN.md` build reconciliation 1).
4. The Endpoint Picker reads live metrics from the vLLM backends of that pool: key-value cache
   utilization, request-queue depth, and adapter residency, from the vLLM metrics endpoint
   (`DESIGN.md` Layer 1).
5. The Endpoint Picker scores the candidate endpoints on that live state and returns the selected
   endpoint.
6. The gateway routes the request to the selected endpoint, and vLLM serves it.

Because the selected endpoint is computed from the current internal state of the model servers,
and the computation runs on every request, the routing decision is closed-loop and per request.
A connection-level load balancer, by contrast, selects an endpoint from connection counters or a
static order and cannot account for which endpoint holds a request's prefix in cache or which
endpoint has the shortest queue. The Endpoint Picker uses exactly that backend-internal state,
which is what makes the router inference-aware.

### 4.2 The proof that it works

The measured evidence that the router uses backend state to improve placement is the prefix-cache
hit rate under a shared-prefix workload, measured against a baseline that does not use backend
state. The baseline routes the identical workload over the identical set of vLLM pods with a
plain round-robin load balancer and no Endpoint Picker, so the only variable is whether routing
uses backend state (`LOAD-TEST-RESULTS.md`, LT-2 method).

The headline measurement is the matched, gateway-equalized publication run: both arms enter through
the same gateway, so the only variable is whether routing uses backend state, and the round-robin
baseline is verified uniform (0.25 of requests per replica across four replicas). Under a
prefix-heavy workload whose working set exceeds per-replica cache, on Llama-3.1 8 billion on NVIDIA
L40S, the Endpoint Picker raises the prefix-cache hit rate to 97.2 percent against 89.3 percent for
round-robin and lowers the ninetieth-percentile time-to-first-token from 559 ms to 463 ms, a 17
percent reduction (about 96 ms) driven by the tail where cache misses land; the median is unchanged
(`loadtest/results/publication/PUBLICATION-REPORT.md`, `summary_nvidia.csv`). An earlier,
non-gateway-equalized measurement (LT-2 run 2) reported a larger 65 percent reduction (77 ms against
220 ms, hit rate 97.9 against 54.7 percent), but that run routed the round-robin arm through a
separate load balancer rather than the gateway and used a heavier working set (400 prefix groups,
driving the baseline hit rate down to 54.7 percent); its figure is superseded by the matched run
above and retained only as context (`LOAD-TEST-RESULTS.md`, LT-2 run 2).

The causal chain is direct. The router co-locates requests that share a prefix onto the endpoint
that already holds that prefix in cache. Co-location raises the prefix-cache hit rate. A higher
hit rate means more of each request's prefix is already computed, which lowers the
time-to-first-token. The hit-rate improvement (97.2 percent against 89.3 percent) is the direct
evidence that backend-aware placement is occurring: round-robin, which ignores cache state,
scatters each prefix group across all pods and pays a cache penalty, while the Endpoint Picker
concentrates each group on the endpoint that holds it. The tail-latency improvement (17 percent
lower ninetieth-percentile time-to-first-token, about 96 ms) is the consequence of that placement.
The size of the benefit is conditional: it appears only when the prefix working set exceeds
per-replica cache (section 5), which is why the same measurement at a working set that fits in cache
shows the two paths equal.

### 4.3 The characterized behavior: benefit scales with prefix-cache locality

The routing benefit is a function of prefix-cache locality in the workload. Under a workload with
shared prefixes and a working set that exceeds per-replica cache, the router delivers the
improvement in 4.2. Under a workload with no prefix locality, there is nothing for backend-aware
placement to exploit and routing converges to the baseline: with a small prefix working set that
every replica can hold entirely, the Endpoint Picker and round-robin both reach about 96 percent
hit rate (96.7 percent against 96.4 percent) and identical time-to-first-token (76 ms against
76 ms) (`LOAD-TEST-RESULTS.md`, LT-2 run 1). This is a property of inference-aware routing: the
benefit is exactly the cache penalty that backend-aware placement avoids, so it is present when
there is prefix locality to exploit and absent when there is none. The router does not degrade
placement in the no-locality case; it matches the baseline.

### 4.4 The asynchronous path routes identically, traced end to end

The asynchronous path routes through the same Endpoint Picker as the synchronous path, and this
is proven by tracing individual queued requests to their serving backend, not inferred. Each
queued request carries the correlation id described in section 2.3, and the gateway access log
records the correlation id with the selected upstream vLLM pod, so every queued request is
attributed to a specific backend pod, tier, node, and accelerator.

Under a concurrent mixed load of batch-tagged long requests on the asynchronous path and
interactive requests on the synchronous path, every batch request is served by a batch-tier pod
and every interactive request by an interactive-tier pod, with zero cross-tier placements: 30 of
30 traced async batch jobs are served on batch pods, and 120 of 120 interactive requests on
interactive pods (`loadtest/results/phased_trace.md`, the cross-tier verdict from
`loadtest/phased_verdict.py` and `loadtest/build_trace.py`). Placement on the asynchronous path
is backend-aware: queued jobs that share a prefix co-locate on one batch pod (three prefix
groups of ten jobs each, each group entirely on a single pod) and the batch prefix-cache hit rate
over the run is 87.7 percent (`loadtest/results/phased_trace.json`). A queued job whose worker is
killed mid-generation is recovered and completes on a batch pod, its trace showing the durability
path and the routing path in one row (a job with two runs, `status=complete`, on a batch-tier
pod). The routing property therefore holds for both the synchronous and the asynchronous path.

## 5. Operational characteristics

These are the operational behaviors of the architecture, stated as specification.

- Failure handling. On Endpoint Picker unavailability, both the interactive and the batch tier
  reject requests with a clean server error (FailClose), and both resume serving when the
  Endpoint Picker returns (P6, section 2.2).
- Routing benefit and cache locality. Inference-aware routing improves placement in proportion to
  prefix-cache locality in the workload. With shared-prefix workloads the router raises the
  cache hit rate and lowers time-to-first-token; with no prefix locality routing matches the
  baseline (section 4.3).
- Routing benefit follows a closed-form model. Cache-aware routing eliminates redundant cold-start
  cache replication: round-robin pays the cold-start (prefill) cost of each prefix once per replica,
  co-located routing pays it once. With N replicas, P distinct prefixes, and R requests per prefix, the
  Endpoint Picker hit rate is 1 minus 1/R and round-robin is 1 minus N/R, so the gap is (N minus 1)/R
  (`WRITEUP-FACTPACK.md` section E.1, `loadtest/results/publication/cache_derivation.md`). R is derived
  from the request counters (requests divided by distinct prefixes), independently of the hit rate. The
  model fits the cold-start point where the working set fits cache (NVIDIA large, cache 195,264 tokens
  derived, R 33: predicted 0.970 and 0.880, measured 0.972 and 0.892, TTFT p90 0.463 s versus 0.559 s,
  17 percent lower). The control (R 264) is a misfit, not a validating point: it measures a tie at
  0.998 against a predicted round-robin hit of 0.985, because the benchmark discards its warmup and the
  control's few cold events are all in that discarded warmup (the co-location probe warms all 6 of its
  prefixes before measurement), so the measured benefit is a floor for small working sets rather than a
  refutation. The architecture delivers two distinct
  benefits with different conditions. Compute saved (the hit-rate gap, prefill work avoided and
  throughput headroom) is present under cold-start replication or eviction, is permanent under prefix
  churn and under eviction, and amortizes toward zero only for a fixed working set reused indefinitely.
  Tail-latency reduction is present only when the co-located miss fraction is below the percentile
  threshold and the round-robin miss fraction is above it (miss_EPP less than f less than miss_RR, the
  general form). In the cold-start regime the miss fractions are 1/R and N/R, so this reduces to the
  window 10 less than R less than 10N at p90; under eviction the round-robin miss is eviction-driven
  and far above N/R (measured 0.589 at the eviction point against N/R of 0.133), so the general form is
  required and the R-based window is only the cold-start special case. Outside the window the tail gap
  collapses even when the hit-rate gap is large (churn has a 0.36 hit-rate gap but a 2 percent p90
  reduction, because at R=8 both misses exceed the threshold and both p90s are cold). A large
  cache-efficiency gain can therefore deliver no latency gain. The window model is binary (it predicts
  presence, not magnitude, so it does not distinguish the 17 percent cold-start reduction from the 25
  percent eviction reduction), unlike the quantitative hit-rate model. Predicted (holding R fixed): the hit-rate gap grows with fleet
  size (0.21 at N=8, 0.45 at N=16), though adding load with replicas raises R and offsets this. The
  model correctly does NOT apply to eviction runs (Neuron, LT-2). On Neuron the cache is retention-starved: max_num_seqs times
  max_model_len (8 times 2048 = 16,384) nearly equals the total cache (16,416), leaving 32 tokens of
  retention headroom, so prefixes cannot be held past request completion and hits occur only on
  temporal overlap (hit rate 0.251 versus 0.154); co-location then concentrates load without a
  compensating locality gain, so the tail inverts (TTFT p90 1.404 s versus 0.982 s). This
  retention-starved cache is a consequence of the conservative configuration the Neuron compiler forced.
  Deltas are within an accelerator; NVIDIA and Neuron latencies are not comparable.
- Model and accelerator scope. The correctness properties are verified on a Llama-3.1 8 billion
  parameter class model served on NVIDIA g6e nodes in a single AWS region (`versions.yaml`,
  `LOAD-TEST-RESULTS.md`). The architecture specifies an AWS Neuron inf2 provisioning path
  (section 2.5), whose accelerator-specific routing behavior is stated below.
- Neuron KV-cache metric semantics (accelerator-specific, prefix-caching dependent). The Endpoint
  Picker scores backends on `vllm:kv_cache_usage_perc`. On NVIDIA that metric is the fraction of
  paged KV blocks in use, a memory-pressure signal. On AWS Neuron the metric's meaning depends on
  the prefix-caching flag: with prefix caching OFF (the contiguous layout, one block per sequence
  sized to max-model-len) it is the fraction of occupied sequence slots and is independent of
  sequence length; with prefix caching ON (the blockwise layout) it is paged KV block utilization
  and tracks total resident tokens, the same meaning as on NVIDIA. This is measured on the running
  fork (`loadtest/results/spike-b/KV-SEMANTICS-FINDING.md`): a 250 times longer sequence leaves the
  OFF metric unchanged while four sequences quadruple it, whereas ON the metric rises with length
  and a single long sequence exceeds four short ones.
- Neuron preemption counter (accelerator-specific). On the Neuron contiguous path (prefix caching
  OFF) each block is sized to hold max-model-len, so a sequence is never preempted for KV space and
  `vllm:num_preemptions_total` is fixed at zero. Any autoscaler or alarm keyed on preemptions is
  therefore scoped to the NVIDIA pools, not the Neuron contiguous path.
- Inference-aware routing on Neuron (accelerator-specific). Inference-aware routing holds on Neuron
  with prefix caching ON: the Endpoint Picker places same-prefix requests on the same replica
  (measured co-location concentration 1.0 versus 0.5 for round-robin) and lowers time-to-first-token
  about 25 percent relative to cache-blind round-robin, so the routing layer is accelerator-agnostic
  in that configuration (`loadtest/results/spike-b/ROUTING-FINDING.md`). With prefix caching OFF the
  routing benefit does not exist on Neuron: there is no prefix cache to exploit and the scored metric
  means occupied slots rather than cache residency, so cache-aware routing carries no locality
  signal. This is a property of the architecture on the Neuron contiguous path, not a caveat: the
  valid configuration for inference-aware routing on Neuron is prefix caching ON.
- Durability semantic. No submitted job is lost. A non-interrupted job runs exactly once; an
  interrupted job is recovered and runs at least once (P2, P3, section 2.3).
- Asynchronous routing. The durable queue sits in front of the inference-aware gateway. The
  worker submits each queued job to the gateway with the batch tier header, so queued requests
  are routed by live backend state through the batch Endpoint Picker, identically to synchronous
  requests (section 2.3, section 4.4).
- Long-request handling. The gateway request timeout accommodates a full long generation, so a
  long batch request completes rather than being cut off mid-generation
  (`manifests/03-routing/httproute.yaml`).
- End-to-end traceability. Every request carries a correlation id (the deterministic job
  identity) through the queue, the DynamoDB item, the gateway access log, and the serving pod, so
  a durable per-request trace of the full journey is available (section 2.3).
- Timeout and lifecycle chain. The chain is strictly ordered so recovery is orderly, not racing:
  the gateway route timeout (600s) is less than the worker HTTP client timeout (660s), which is
  less than the DynamoDB claim lease (750s), which is less than the SQS visibility timeout (900s),
  which is less than the sweeper grace period (960s). A single backend attempt finishes before the
  gateway gives up; the gateway gives up, and the worker observes that, before the claim is
  stealable; and the claim is stealable before SQS redelivers. The route timeout has a margin of
  more than five over the measured maximum generation (112s for a 1879-token generation). This
  ordering is invariant INV10.
- Retry posture on inference routes. The gateway does not retry inference requests. A retry would
  re-issue a request whose DynamoDB claim already succeeded, producing a second GPU run below the
  idempotency layer, so retries are kept off and asserted by `make test-no-gateway-retry`.
- Infrastructure-rejection handling. The worker distinguishes an infrastructure rejection (a
  gateway 5xx from an Endpoint Picker outage, or an unreachable gateway) from a genuine job
  failure (a 4xx). On an infrastructure rejection it does not mark the job failed; it releases the
  claim so the job is re-claimable, extends the message visibility as a bounded backoff so the job
  is not pushed to the DLQ per redelivery, and backs off its poll loop so it does not hammer the
  gateway. A genuine 4xx is marked failed. Proven by `make test-infra-backoff`.
- Configuration tuning dependencies (settings that must be tuned together). The KEDA queue-depth
  threshold and the vLLM running-batch cap are tuned so the queue crosses the threshold before
  saturation. The claim lease exceeds the maximum generation time plus a margin. The sweeper grace
  exceeds the SQS visibility timeout. Per-tier maximum replica counts fit the NodePool instance
  limits.

## 6. Reproducibility

The architecture is reproducible from the committed repository. Versions are pinned in
`versions.yaml`; operations run through the `Makefile`.

Prerequisites (`DESIGN.md`, Stage 0). An AWS region that offers both inf2 and EFA-capable GPU
families (us-west-2 or us-east-1); Neuron quota raised above zero if the Neuron path is used;
sufficient GPU quota; and `versions.yaml` filled. A cost ceiling should be set before running the
GPU scenarios.

Build stages (`Makefile`). `make cluster-up` creates the cluster; `make addons` installs
Karpenter, KEDA, the Gateway API Inference Extension chart, and the device plugins. `make
stage-1` brings up single-tier routing and must pass `make test-routing-reachable` and `make
verify-invariants`. `make stage-2` adds the asynchronous front door and must pass `make
test-idempotency`. `make stage-3` adds the batch tier and must pass `make test-tier-isolation`
and `make verify-invariants`. `make observability` installs Prometheus and the queue-depth
autoscalers. `make stage-4` adds the Neuron path.

Verifying each correctness property independently:

- P1 and P2: `make test-idempotency` (exactly-once), `make test-ownership-guard` (claim
  ownership), and the LT-2 routing scenario for backend-aware placement.
- P2 and P3, offline: `make test-sweeper` proves the reconciler and re-runs the ownership guard
  with no cluster or GPU.
- P3, on cluster: the LT-6 durability scenario (`loadtest/run_lt6.sh`, arbitrated by
  `loadtest/analyze.py` `lt6d`).
- P4: `make test-tier-isolation` and the LT-4 scenario.
- P5: the LT-5 autoscaling scenario (`loadtest/run_lt5.sh`) and `make test-keda-queries`.
- P6: the LT-7 failure-mode scenario.
- P7: `make verify-invariants`.
- P8: the per-request end-to-end trace (`loadtest/build_trace.py`, `loadtest/phased_verdict.py`),
  committed in `loadtest/results/phased_trace.md` and `phased_trace.json`.
- P9: the combined-demonstration run (`loadtest/run_request_flow.sh`) and
  `loadtest/verify_reconciliation.py`, viewed on the Request Flow dashboard (uid
  `inference-request-flow`).

The committed load-test results and the per-property arbitration are in `LOAD-TEST-RESULTS.md`.
