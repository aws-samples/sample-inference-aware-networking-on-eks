# Design and build guide

The source of truth for how to build, verify, and extend this project. Read it fully before running anything. Where this file conflicts with a manifest, this file and the primary upstream sources win. Agents working on the repo should also read `DESIGN.md`, which carries the operating rules that go with these conventions.

## What this project is

An inference-aware networking layer on Amazon EKS for large language model text inference. It adopts the llm-d well-lit paths (intelligent inference scheduling and disaggregated serving) for the routing and serving layers, and adds the AWS-specific pieces that llm-d does not provide: a durable async front door (Amazon SQS plus a DynamoDB idempotency table), AWS custom silicon support (Inferentia and Trainium via Neuron), and the AWS ingress and node seams. The goal is a validated, reproducible reference architecture, proven on a live cluster with measured pass or fail results, not a document.

The design is implemented stage by stage, each stage verified against an explicit gate, preserving a set of cross-manifest invariants that this design has broken before. Correctness here means a manifest applies cleanly AND behaves as the design says. Several past defects applied cleanly and then behaved wrong, so that is the failure mode worth hunting.

## Prime directives (non-negotiable)

Project conventions. Other files in the repo reference these by number, so the numbering is stable.

1. **Every version-sensitive claim is verified against a primary source.** Primary sources are the upstream project docs and the AWS docs, not blog posts and not this repo's prose. The Gateway API Inference Extension, Karpenter, KEDA, vLLM, and the AWS Neuron SDK all change fast. Where a manifest field or a metric name matters, it is confirmed with `kubectl explain` against the live cluster, or against the CRD reference for the exact version installed. AND THE AUTHORITATIVE DOCUMENT IS NOT ALWAYS THE WHOLE AUTHORITY: a licence, a policy, or a spec may incorporate a second document by reference, and reading only the first one yields a confident, primary-sourced, wrong answer. One instance so far, and it is the strongest kind because the reader was being careful: the Llama 4 EU multimodal exclusion was marked REFUTED after reading both the LICENSE file and the model card, neither of which contains it, because it lives in the separately-hosted Acceptable Use Policy. This failure mode is worse than reading a blog post, since a secondary source announces its own weakness while a primary source read too narrowly does not. Before concluding a term is ABSENT, establish what the document incorporates; absence of evidence in one file is not evidence of absence across the instrument.

2. **`versions.yaml` is the single source of truth for every version, and it is pinned before the build starts.** Every `FILL` value resolves to a real validated version, with `verified_on` set. No manifest hardcodes a version that contradicts it.

3. **The cross-manifest invariants in the "Invariants" section below are never broken.** `make verify-invariants` runs after any manifest edit and all checks must pass. A change that would alter an invariant is surfaced rather than absorbed by editing the invariant.

4. **The gateway, InferencePool, and Endpoint Picker trio is installed from the Helm chart, not hand-applied.** The manifests in `manifests/03-routing/` for the EPP are reference shapes to check the chart output against. The EPP flags are version-specific and will drift. Install from the chart at the pinned version, then diff its output against the reference shapes to confirm intent.

5. **The build proceeds in stages and no gate is skipped.** Each stage has a definition of done that must pass before the next stage starts. Stage 1 is deliberately the smallest thing that can be measured, and everything after is additive.

6. **No em-dashes anywhere in the repo.** Colons, commas, periods, or parentheses instead. Write "Amazon EKS", never bare "EKS", with ONE standing exception: AWS product and feature names are quoted exactly as AWS names them, so "EKS Pod Identity" is correct and must not be rewritten (see ARCHITECTURE-REVIEW.md 2.6). The exception is recorded here so the same line is not re-litigated at every review. Never depend on shell word splitting: iterate with an explicit array or a `printf ... | while IFS= read -r` loop, and quote every expansion. This host runs zsh, which does NOT split unquoted parameter expansions the way bash does, and the failure is silent rather than loud: two values arrive as one argument. It has cost this project twice, once in an image-provenance audit and once mid-teardown. The rule is stated instead of the two case studies, because a third case study would not have prevented a third instance. Every CloudWatch log group this project creates carries an EXPLICIT retention policy, set at creation. A group with no retention bills indefinitely and grows forever, and it is the delivered-behaviour failure mode of a resource that every list-based sweep reports as fine. And distrust the COMFORTABLE reading hardest: a proxy that ALARMS gets investigated and self-corrects, while a proxy that REASSURES gets accepted and propagates into decisions. Seven instances so far, one of them comfortable: CloudWatch storedBytes read 0 against 351 MB of confirmed ingest, and believing it would have written "free" into a billing criterion. Verify against the authority (describe-update, IncomingBytes, ls-remote, config_dump), never the convenient field beside it. At the 2026-08-03 rebuild there were zero log groups in the account, because control-plane logging was never enabled in the eksctl config, so the usual orphan class did not exist here. That is a reason, not a guarantee: the moment logging is enabled, or any component starts writing, the retention decision is made at that point rather than deferred to a cleanup.

7. **Anything that cannot be verified is recorded as unverified, not asserted.** Capacity, quota, and fork-only Neuron behavior frequently cannot be checked from the build host. Those gaps are flagged and left open rather than filled with an assumption. AND A MECHANISM STORY REQUIRES A REPLICATION BEFORE IT IS WRITTEN DOWN: one sample licenses reporting the NUMBER, not EXPLAINING it. Two instances so far, the 65 percent eviction figure and a connection-pooling story invented to explain a single run's tail in the D-7 relay measurement, which the second and third runs contradicted. Both were plausible mechanisms fitted to one sample and both were caught by this project's own replication rather than by review. The asymmetry is why it belongs in this convention rather than beside it: a number from one sample is honest when labelled as one sample, whereas an explanation from one sample smuggles in a causal claim the data cannot carry, and the explanation is what gets repeated downstream while the caveat is what gets dropped. Appended here rather than added as a new item 8, because DESIGN.md declares a one-to-one mapping onto this numbering and other files cite it by number. AND A FAILED GATE NAMES ITS OWN BINDING CONSTRAINT, WHICH IS OFTEN NOT THE THING UNDER TEST: when a measurement cannot certify, establish WHICH input starved it before concluding anything about the subject. This is the same asymmetry as the mechanism-story rule one clause above: the honest report is "this corpus cannot exercise this test", and the tempting one is a conclusion about the subject.

## Architecture, condensed

Request path, four layers, all on Amazon EKS:

- **Layer 1, entry point.** Envoy Gateway (Gateway API) behind an ALB, with the Gateway API Inference Extension Endpoint Picker (EPP) doing inference-aware routing: it scores backends on live GPU state (KV-cache utilization, queue depth, LoRA residency) read from the vLLM metrics endpoint. Adopted from llm-d and the Gateway API Inference Extension. For async traffic, requests enter a queue instead of holding a connection.
- **Layer 2, queue broker.** Amazon SQS as the durable buffer, a DynamoDB table as the idempotency and job-state store. The async worker claims jobs with a conditional write so a duplicate retry never double-allocates a GPU. This is the contribution llm-d does not have.
- **Layer 3, inference engine.** vLLM (prefix caching enabled) behind the EPP, one Deployment per SLA tier. Adopted from llm-d.
- **Layer 4, node provisioning.** Karpenter provisions GPU nodes (NVIDIA g6e or p5) and Neuron nodes (inf2 or trn2), split into per-tier NodePools. NVIDIA and AWS custom silicon both supported. The Neuron half is the contribution.

Two SLA tiers, interactive and batch, isolated by Option A (physical fleet isolation): separate pools, EPPs, Deployments, NodePools, and ScaledObjects, with an HTTPRoute header match directing batch traffic to the batch pool.

## What is adopted vs contributed

- **Adopted (do not reinvent):** Layer 1 routing (EPP, prefix-cache-aware and load-aware scoring), Layer 3 serving (vLLM plus the llm-d scheduler), disaggregated prefill and decode. These are llm-d well-lit paths. Install them from the chart.
- **Contributed (this is the novel work):** the durable async front door with idempotency (Layer 2), the Neuron path (Layer 4 on Inferentia and Trainium), the AWS ingress seams (ALB idle timeout, SSE keepalive), and the two spikes.

## Build reconciliations (live implementation, 2026-07-19)

Ground truth learned by standing this up on a real cluster. Where these differ from
the prose above, THESE win for the live build. Full detail in `BUILD-NOTES.md`.

1. **The gateway stack is "Envoy Gateway PLUS Envoy AI Gateway" (design 4.1), and the
   AI Gateway layer is load-bearing.** Base Envoy Gateway rejects an InferencePool
   backendRef (ResolvedRefs=False, InvalidKind, HTTP 500). The Envoy AI Gateway
   controller (v1.0.0) runs an extension server; Envoy Gateway is reconfigured via
   `extensionManager` + `backendResources: InferencePool v1`
   (`infra/addons/envoy-gateway-values.yaml`) to route to pools.
2. **The InferencePool + EPP are chart-owned** (`inferencepool` chart, `provider=none`).
   `manifests/03-routing/inferencepools.yaml` and `epp-reference.yaml` are REFERENCE
   SHAPES, not applied live. `make stage-1/3` install the EPP chart per tier.
3. **HTTPRoute precedence is by longest path, then header count, NOT list order.** The
   batch rule must also match `/v1` (not header-only) or batch traffic silently routes
   to interactive. Fixed in `httproute.yaml`; INV7 checks pool ref order.
4. **Auth is EKS Pod Identity, not IRSA**, for Karpenter, the async worker, submit-api,
   and KEDA. Simpler, no OIDC annotations.
5. **A GatewayClass + Gateway** (`manifests/03-routing/gateway.yaml`) and an **async
   submit API** (`async-worker/submit_api.py` + `submit-api.yaml`) were added; the repo
   shipped neither and both are required (the Gateway is referenced by the route; the
   submit API is the producer the idempotency test calls).
6. **Device plugins need affinity/name care**: the NVIDIA plugin's default affinity wants
   NFD labels we do not run, so it targets `karpenter.k8s.aws/instance-gpu-manufacturer`.
   Neuron advertises `aws.amazon.com/neuron` and `aws.amazon.com/neuroncore`
   (the old `aws.amazon.com/neurondevice` was renamed to `neuron` and removed in Neuron 2.20).
7. **Capacity**: batch Spot for g5/g6/g6e was unavailable in-region; the batch NodePool
   diversifies families and falls back to on-demand. GPU Deployments use `Recreate` so a
   rollout does not transiently request a second GPU and oversize the node.
8. Stage 1 uses ungated `Qwen/Qwen2.5-7B-Instruct`; Neuron uses ungated
   `TinyLlama/TinyLlama-1.1B-Chat-v1.0`. Swap to the design's Llama with an HF token.
10. **A diagnosis without a cure is a comment**, and it is worse than no diagnosis because it
   retires the concern while leaving the defect. A diagnosed defect must name a VERIFIABLE cure,
   and a cure that no standing gate set calls is the same comment wearing a filename. Origin:
   the 2026-08-04 diagnosis of INV15's manifest-versus-cluster blindness, whose cure existed in
   the same commit, in no aggregate target, and ran once.
11. **An implicit default or implicit ordering must never determine the scope of a claim.**
   Pass scoping flags explicitly, name the object you read rather than indexing an unordered
   list, prefer counts over line-counted prose, and never truncate an enumeration you will draw
   a conclusion from. Origin: a missing `--region` reported a present queue as nonexistent;
   `.items[0]` read a terminating pod; `head -4` on a fifteen-model list hid the model that was needed.
13. **A class fix is not done until the enumeration that found it has been re-run BY NAME**
   across every language and file type in the repository, with the enumeration itself recorded
   beside the fix. Searching for a SYNTAX finds one spelling of the class. Four instances in one
   batch, 2026-08-08: the `111122223333` placeholder account fixed on the worker on 2026-08-05 and
   left in three siblings; the `GATEWAY` discovery class closed for shell callers while its Python
   sibling failed one stage later, because the enumeration grepped `${VAR:?}`; reader-size replicas
   applied to the Deployments and not to the KEDA ScaledObjects that override them, taking peak
   concurrency to 11 against a projection of 2. Retroactive run over the identifier classes the
   same day: seven distinct file extensions affected, so an enumeration assuming "documentation"
   would have missed six of them. `tools/build_public_export.py` records its own enumeration and
   FAILS on a surviving identifier rather than reporting a healthy-looking substitution count.

12. **A gate named in an instruction must exist as a committed, FINDABLE artifact before
   compliance with it can be asked or claimed.** A requirement with no findable artifact is a
   rumor, and it fails in both directions at once: working toward it is unverifiable, and
   refusing it looks like a gap. FINDABLE is part of the rule rather than a nicety. Origin: the
   same-session publication rule was real, measured, and committed since 2026-07-28, as prose
   three hundred lines into a fact pack under a heading about a topology confirmation, which is
   the same property as not existing for every reader who does not already know it is there.
   Cure: `docs/blog_post/CLAIMS-AUDIT.md`.

9. **The orphan sweeper (design section 3.4) is IMPLEMENTED** (`async-worker/sweeper.py`,
   `manifests/06-sweeper/sweeper-cronjob.yaml`). SQS redelivery recovers almost every
   interrupted job on its own (a redelivered copy re-claims through the stale-claim steal
   path). The one case it cannot cover is a job stuck in `processing` whose SQS message is
   GONE (a dedup-or-redelivery race with a killed worker deletes the only copy). LT-6
   stranded exactly one such orphan per 300 (299/300). The sweeper is a reconciler CronJob
   (every 1 min, `concurrencyPolicy: Forbid`) that scans for those orphans and re-enqueues a
   message so a worker re-claims. Correctness rules that make it safe: (a) it acts ONLY on
   `processing` items with an expired lease (never a `complete` item, never a live-worker
   item with a future lease); (b) its grace period is DERIVED from the queue's own
   VisibilityTimeout plus a margin, so grace always exceeds visibility and the sweeper never
   races SQS's own redelivery; (c) a `requeued_at` cooldown with a conditional write makes it
   idempotent against itself and against a second sweeper pod. Recovery is at-least-once for
   interrupted jobs (a swept job runs at least once; if the original worker finished
   inference but died before writing `complete`, the re-run is a second GPU run). This is the
   SAME at-least-once property interrupted jobs already had, and it does NOT weaken the
   exactly-once guarantee for non-interrupted jobs (LT-3), which are never orphans and which
   the sweeper never touches. The claim/key/state logic is shared with the worker via
   `async-worker/jobstore.py`; the claim write now persists `client_id` + `payload` on the
   item so a message-less orphan can be reconstructed. Least privilege: the sweeper is the
   ONLY component granted `dynamodb:Scan` (role `inference-sweeper`, plus `UpdateItem`,
   `sqs:SendMessage`, `sqs:GetQueueAttributes`). Proven GPU-free by `make test-sweeper` (6
   moto assertions) and on-cluster: LT-6 now reads 300/300, with the sweeper recovering the
   1 message-less orphan and SQS redelivery recovering the other interrupted jobs.

## Repo map

```
DESIGN.md                     this file
README.md                     human-facing overview
DESIGN.md                     operating rules for coding agents working on this repo
versions.yaml                 SINGLE SOURCE OF TRUTH for versions. Fill before building.
Makefile                      task runner. Every operation has a target.
infra/cluster/                EKS cluster definition (eksctl)
infra/addons/                 Karpenter, KEDA, Gateway API Inference Extension, device plugins
manifests/01-nodepools/       EC2NodeClass and NodePool per tier and accelerator, plus EFA
manifests/02-serving/         vLLM Deployments and Services (interactive, batch, neuron)
manifests/03-routing/         InferencePools, InferenceObjectives, HTTPRoute, EPP reference shapes
manifests/04-autoscaling/     KEDA ScaledObjects (interactive, batch, async worker)
async-worker/                 the real conditional-claim worker: code, Dockerfile, IAM
tests/smoke/                  fast checks (KEDA queries non-empty, routing reachable)
tests/validation/            the pass or fail tests that prove the design's claims
spikes/spike-a-l2-cache/     L2 KV cache tier characterization (four backends)
spikes/spike-b-neuron-epp/   llm-d EPP on Inferentia. The flagship novel result.
```

## Build sequence and gates

Run stages in order. Each gate must pass before the next stage. Use `make stage-N` where available.

### Stage 0: prerequisites (human-gated)
Before any build, the human must confirm: region chosen (must have both inf2 and EFA-capable GPU families; us-west-2 or us-east-1 are safe), Neuron quota raised above zero (default is zero), GPU quota sufficient, and `versions.yaml` filled. Do not proceed until these are confirmed. Neuron and large GPU instances are capacity-constrained; start small.

### Stage 1: routing works on one tier, one accelerator
Bring up the cluster, Karpenter, KEDA, the Gateway API Inference Extension chart, one EC2NodeClass, one interactive NodePool on `g6e`, one vLLM interactive Deployment, one InferencePool, one EPP (from the chart), the Gateway, and the interactive HTTPRoute rule.
**Definition of done:** a request to `/v1` reaches a vLLM pod through the EPP and returns a completion. `make test-routing-reachable` passes. `make verify-invariants` passes. The EPP is routing on live metrics (confirm the EPP scrapes the vLLM metrics endpoint and the scores are not uniform under skewed load).

### Stage 2: the async front door
Add the SQS queue, the DynamoDB idempotency table, the namespace and service accounts, the IRSA role and policy, the async worker Deployment, and its KEDA ScaledObject on queue depth.
**Definition of done:** `make test-idempotency` passes. This test submits the same job twice concurrently and asserts exactly one GPU job runs. This is the headline contribution; the test must prove the conditional claim works, including under an at-least-once SQS redelivery.

### Stage 3: the second SLA tier
Add the batch InferencePool, the batch EPP, the batch InferenceObjective (pointing at the batch pool), the batch vLLM Deployment, the batch NodePool on Spot, the batch KEDA ScaledObject, and the header-matched HTTPRoute rule.
**Definition of done:** `make test-tier-isolation` passes. A batch-headered request lands on a batch-tier node and an interactive request lands on an interactive node, and a saturating batch load does not raise interactive TTFT past its target. Both pools are reachable. `make verify-invariants` passes.

### Stage 4: Neuron
Add the Neuron EC2NodeClass considerations, the Neuron NodePool, and a Neuron vLLM Deployment using the AWS DLC. This is where the traps live. Read the "Neuron traps" section before starting.
**Definition of done:** a request served on an inf2 node returns a completion, AND the KEDA queries and alarms for the Neuron pool are confirmed against the fork's actual metric names (not assumed). If a metric name differs on the fork, fix the query before declaring done.

### Optional stage E: EFA disaggregated serving
Add the EFA EC2NodeClass (per the AWS P6-B200 layout), the EFA device plugin (not DRA under Karpenter), and the disaggregated serving path. Highest friction, lowest novelty. Do last, only if the workload is prefill-heavy or MoE.

## Invariants (never break these)

After any manifest edit, `make verify-invariants` must pass. These are the couplings this design has silently broken before:

1. **Pool selector equals Deployment labels.** Each InferencePool `spec.selector.matchLabels` (`app`, `tier`) must exactly match the labels on its vLLM Deployment. Interactive pool to interactive Deployment, batch to batch.
2. **Objective poolRef names differ and point at real pools.** The interactive InferenceObjective `poolRef.name` is the interactive pool; the batch objective's is the batch pool. If both point at one pool, isolation collapses silently.
3. **endpointPickerRef points at a real EPP.** Each pool's `endpointPickerRef.name` matches the EPP the chart deploys, which is `vllm-llama3-<tier>-epp` (`vllm-llama3-interactive-epp` for interactive, `vllm-llama3-batch-epp` for batch). The name is `<helm release>-epp` and the release is `vllm-llama3-<tier>`, so INV3 derives it from the chart script rather than from the pool manifest it checks.
4. **KEDA scaleTargetRef names a Deployment, never a pool.** InferencePool has no `/scale` subresource. Each ScaledObject targets its vLLM Deployment (or the async worker Deployment).
5. **Taints and tolerations pair up.** Every NodePool taint (`nvidia.com/gpu`, `aws.amazon.com/neuron`) has a matching toleration on the Deployment that must land there.
6. **failureMode asserts the DELIVERED behavior, not an aspiration.** Interactive is FailClose (a hard 5xx beats routing blind on user traffic). Batch was designed FailOpen (degraded routing beats dropping batch work), BUT the load test (LT-7) proved Envoy AI Gateway v1.0.0 does not honor FailOpen for InferencePool: it never sets the Envoy ext_proc failure_mode_allow flag (verified via config_dump), and the ext_proc filter it injects is not reachable by EnvoyPatchPolicy (injected after Envoy Gateway translation). v1.0.0 is the latest release, so there is no fix to pin. Therefore batch is set to FailClose to match delivered behavior, and BOTH tiers are effectively FailClose on this stack. The FailOpen design intent stands; restore it (and the differentiated invariant) when the platform maps failureMode. Do not leave the manifest asserting FailOpen, which the platform drops. Upstream report drafted in `upstream/ai-gateway-failuremode-issue.md` and FILED 2026-08-08 as https://github.com/envoyproxy/ai-gateway/issues/2502.
7. **HTTPRoute reaches both pools.** The header-matched batch rule (`x-gateway-inference-objective: batch`) must precede the catch-all interactive rule, and both backendRefs must be `InferencePool` in group `inference.networking.k8s.io` with correctly spelled pool names.
8. **Namespace is `inference` everywhere.**
9. **The async worker routes through the inference-aware gateway, not a vLLM Service.** The
   worker's `GATEWAY_ENDPOINT` names the Envoy gateway (not a `vllm-llama3-*:8000` Service) and
   `INFERENCE_OBJECTIVE` is set, so queued jobs are placed by the Endpoint Picker exactly like
   synchronous traffic. Targeting a Service would round-robin and bypass inference-aware routing.
10. **The timeout chain is strictly ordered.** Route timeout < worker HTTP client timeout < claim
    lease < SQS visibility (600 < 660 < 750 < 900), so a single attempt finishes before the gateway
    gives up, the gateway gives up before the claim is stealable, and the claim is stealable before
    SQS redelivers. Equal values race and can double-run one claimed job (ARCHITECTURE-REVIEW.md R.5).

## Known traps (read before the relevant stage)

- **Neuron KV-cache metric semantics (Stage 4, critical).** On NVIDIA, `vllm:kv_cache_usage_perc` is a fraction of paged KV blocks, a real memory-pressure signal. On Neuron with prefix caching OFF, NxD Inference uses a contiguous KV layout, sets block size to max-model-len, and allocates one block per sequence, so the same metric becomes a count of occupied sequence slots, not memory pressure. The EPP scores on this metric. The number resolves, the dashboard renders, and the routing decision means something different. With prefix caching ON, the layout is blockwise and semantics move back toward NVIDIA. Chunked prefill is not supported on Neuron. Confirm the prefix-caching flag and re-derive what the scorer is actually doing before trusting Neuron routing. This is the subject of Spike B.
- **Neuron preemption counter is dead, not just different (Stage 4).** On the contiguous path, NxD Inference sizes each block to fit max-model-len, so preemption is never needed and the plugin disables the scheduler's preemption checks. `vllm:num_preemptions_total` can only read zero on that path. Any KEDA scaler or alarm that keys on preemptions will sit green through a real incident. Scope those alarms to the NVIDIA pools.
- **Neuron LoRA is static (Stage 4).** NxD Inference loads LoRA adapters only at server startup; dynamic runtime loading is not supported. The dynamic-residency routing problem does not exist on Neuron; adapters are declared in the Deployment spec.
- **Neuron container equals fork (Stage 4).** The AWS DLC `vllm-inference-neuronx` ships the AWS Neuron fork of vLLM, not upstream. Multi-node Neuron inference is fork-only. Choosing the DLC is choosing the fork, which is also the only path to multi-node and multi-modal Neuron.
- **KEDA empty-vector trap (Stage 1 and beyond).** A Prometheus scaler with a misspelled metric name returns an empty vector, and `or vector(0)` turns that into a legitimate-looking zero, so the autoscaler silently never fires. `make test-keda-queries` asserts every scaler query returns a non-empty vector. Run it after any metric-name change.
- **EFA subnet IP exhaustion (Stage E).** A P6-B200 with all interfaces as EFA can consume many secondary IPs. Use the EC2NodeClass `networkInterfaces` layout (one primary ENA plus EFA-only interfaces, enumerated per network card, one EFA per card). Do not use a `count` field; it does not exist. Match the AWS published P6-B200 layout exactly.
- **Device plugin, not DRA, under Karpenter (all three device types).** This project provisions accelerators and EFA with the classic device plugins, not with Dynamic Resource Allocation (DRA), because DRA is not used with Karpenter here. This applies to all three: (1) EFA, whose DRA driver was found incompatible with Karpenter (Stage E); (2) NVIDIA GPUs, provisioned via the NVIDIA device plugin (Stage 1), not GPU DRA; (3) Neuron, provisioned via the Neuron device plugin (Stage 4), not the Neuron DRA driver. AWS now ships a Neuron DRA driver (recommended for Amazon EKS 1.34+), but adopting it needs Karpenter-compatibility testing that this project has not done, so the device plugin remains the path. Use the device plugin for all three.
- **EPP flags are illustrative (all stages).** The EPP manifest in `manifests/03-routing/` shows shape, not exact flags. Flags change between versions. Template from the chart at the pinned version and diff.

## Commands

All operations go through the Makefile. Key targets:

```
make preflight            check versions.yaml is filled and tools are present
make cluster-up           create the EKS cluster (eksctl)
make addons               install Karpenter, KEDA, Gateway API Inference Extension, device plugins
make stage-1              bring up single-tier routing (see gate above)
make stage-2              add the async front door
make stage-3              add the batch tier
make stage-4              add Neuron
make verify-invariants    run the 10 cross-manifest invariant checks (INV1-INV10). Run after every manifest edit.
make test-keda-queries    assert every KEDA scaler query returns a non-empty vector
make test-routing-reachable
make test-idempotency     the headline test: duplicate submission runs one GPU job
make test-tier-isolation
make validate             run the full Section 15.3 pass or fail suite
make spike-b              run the Neuron EPP semantics spike
make dry-run              server-side dry-run every manifest without applying
```

## Human decisions (not automatable)

- **Version pins.** A version enters `versions.yaml` only after verification against a primary source and human confirmation, never by guess.
- **Region, quota, capacity reservations.** Human-owned. What is needed gets flagged, and the build stops there.
- **Cost.** GPU and Neuron capacity dominates the cost of this project. Accelerators are provisioned deliberately, and `manifests/07-safety-net/` exists so a stalled session cannot leave them running.
- **Customer Zero and the managed comparison numbers.** These need a real workload and a human decision on what to publish.
- **Whether to publish, and where.**

## The two spikes (the reason this is research, not just a deployment)

- **Spike B (priority): llm-d EPP on Inferentia.** Nobody has published this. Run the EPP against a Neuron-backed InferencePool, with prefix caching on and off, and measure whether the prefix scorer earns anything when the KV metric changes meaning. If routing quality on Neuron is no better than round-robin, that is a publishable negative result. Single-node inf2 is enough; do not need multi-node. See `spikes/spike-b-neuron-epp/README.md`.
- **Spike A: L2 KV cache tier on self-managed EKS.** Characterize LMCache with four AWS backends (FSx for Lustre, S3 Express One Zone, ElastiCache Redis, and node-local disk). Redis is the floor to beat, since AWS states its managed tier beats Redis. The open question is whether the llm-d Lustre validation (run on GCP) transfers to FSx. See `spikes/spike-a-l2-cache/README.md`.

## Stage completion record

Each finished stage is recorded in `BUILD-NOTES.md` with: which gate passed, the invariant check result, any metric name corrected against the live cluster, and any version pinned. A red gate stops the build rather than deferring to the next stage. After Stages 1 through 4 the next action is Spike B, which produces the novel result the design exists to establish.
