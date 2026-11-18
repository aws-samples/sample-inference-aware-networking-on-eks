# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# Task runner. Every operation goes through a target. See DESIGN.md for gates.
SHELL := /bin/bash
NS := inference
# Every Python target goes through the project venv. Bootstrap it with `make venv`.
PY := ./.venv/bin/python
# The installer is pinned like every other dependency: an unpinned `--upgrade pip` resolves to
# whatever pip is newest on the day. Bump deliberately, then rerun make test-offline.
PIP_VERSION := 26.2

# Every gate below runs through GATE, which passes the exit code through unchanged and, on a
# failure, writes the full output to `.gate-failures/<gate>-<timestamp>.log`. See
# tests/capture-gate.sh for why (an unexplained `make dry-run` transient in batch 2 had already
# scrolled out of the terminal by the time anyone went looking for it).
GATE = bash tests/capture-gate.sh

.PHONY: preflight venv cluster-up cluster-down addons stage-1 stage-2 stage-3 stage-4 observability \
        arm-safety-net disarm-safety-net ensure-safety-net-armed \
        observability-crds observability-scalers network-policies \
        verify-invariants verify-addon-pins set-account set-worker-image hf-secret dry-run \
        test-keda-queries test-routing-reachable test-idempotency test-tier-isolation \
        test-ownership-guard test-sweeper test-infra-backoff test-no-gateway-retry \
        test-job-key test-worker-http-session test-redemption-route \
        test-generator-syntax test-cfn-merge-guard test-karpenter-ownership test-guards \
        test-invariant-efficacy test-no-thinking-leakage test-reqtrace-format test-chat-template-provenance test-message-contract test-thinking-live test-offline \
        test-promotion-route-shape check-running-images test-cluster \
        test-offline-is-offline test-running-images test-request-bounds test-cluster-down-context \
        validate

preflight:
	@echo ">> checking versions.yaml is filled and tools present"
	@! grep -q "FILL" versions.yaml || (echo "ERROR: versions.yaml has FILL values" && exit 1)
	@! grep -rq 'ENVOY_GATEWAY_SERVICE' manifests async-worker/*.yaml 2>/dev/null || (echo "ERROR: unresolved gateway placeholder ENVOY_GATEWAY_SERVICE in a manifest; the worker resolves GATEWAY_ENDPOINT at Stage 2 via the gateway-endpoint ConfigMap" && exit 1)
	@for t in kubectl helm eksctl aws jq; do command -v $$t >/dev/null || (echo "missing $$t" && exit 1); done
	@test -x $(PY) || { echo "missing $(PY): run 'make venv'"; exit 1; }
	@echo "OK"

# Project virtualenv. Every Python target expects it, so it is a real target rather than
# a step buried in the README. Idempotent.
venv:
	@test -d .venv || python3 -m venv .venv
	@./.venv/bin/pip install -q "pip==$(PIP_VERSION)"
	@./.venv/bin/pip install -q -r requirements-dev.txt
	@echo "OK  venv ready at .venv ($$($(PY) -V))"

cluster-up:
	eksctl create cluster -f infra/cluster/eksctl-cluster.yaml

# THE POST'S CLEANING UP SECTION TELLS THE READER TO RUN THIS, AND IT DID NOT EXIST.
# A walkthrough that names a target the Makefile does not define fails for every reader who
# follows it literally, which is the same shape as the install-device-plugins defect: a sequence
# that CONTAINS the right step does not help when the step is not what runs.
#
# ORDER MATTERS AND IS NOT COSMETIC. The autoscalers go first because a KEDA ScaledObject owns its
# target's replica count, so scaling a Deployment by hand while the ScaledObject exists makes the
# autoscaler put it straight back. Then the serving tiers, then the nodes, and only then the
# control plane. Accelerator emptiness is verified against the EC2 API rather than the cluster
# view, because Node objects linger after the instances behind them are gone.
#
# EVERY kubectl CALL IS BOUND TO A VERIFIED CONTEXT (deep security scan finding M2). Steps 1 to 4
# delete cluster-wide (every NodeClaim, every LoadBalancer Service in every namespace), so run
# against the wrong current context they would empty someone else's cluster. The guard resolves the
# context whose API server is this cluster's endpoint, refuses otherwise, and reports ABSENT when
# the cluster is already gone, in which case the kubectl steps are skipped and steps 5 and 6 still
# run. GNU make expands the whole recipe before running its first line, so CD_CTX is set for all.
# NO COMMA may appear in the message text below: it would split the $(if) arguments, and every
# kubectl step would then fail as a shell syntax error that the leading `-` hides.
CD_KUBECTL = $(if $(filter ABSENT,$(CD_CTX)),@echo "   skipped (the cluster no longer exists):" kubectl,kubectl --context '$(CD_CTX)')
cluster-down:
	$(eval CD_CTX := $(shell bash infra/cluster/require-cluster-context.sh infra/cluster/eksctl-cluster.yaml))
	@test -n "$(CD_CTX)" || { echo "REFUSING to tear down: the kubectl context is not verified (reason above)" >&2; exit 1; }
	@echo ">> 0/6 kubectl context verified against the cluster endpoint: $(CD_CTX)"
	@echo ">> 1/6 autoscalers first: a ScaledObject owns its target's replica count"
	-$(CD_KUBECTL) -n $(NS) delete scaledobject --all
	@echo ">> 2/6 scale every serving tier to zero"
	-$(CD_KUBECTL) -n $(NS) scale deploy -l app=vllm-llama3 --replicas=0
	-$(CD_KUBECTL) -n $(NS) scale deploy async-worker --replicas=0
	@echo ">> 3/6 release the nodes and CONFIRM against the EC2 API, not the cluster view"
	-$(CD_KUBECTL) delete nodeclaims --all
	@bash -c 'for i in $$(seq 1 40); do 	  n=$$(aws ec2 describe-instances --region us-west-2 	        --filters Name=instance-state-name,Values=running,pending 	                  "Name=instance-type,Values=g6*,g6e*,g5*,inf2*,p5*,trn*" 	        --query "length(Reservations[*].Instances[])" --output text); 	  echo "   accelerators still running: $$n"; 	  [ "$$n" = "0" ] && break; sleep 15; done; 	  [ "$$n" = "0" ] || { echo "REFUSING to delete the cluster while $$n accelerators are running" >&2; exit 1; }'
	@echo ">> 4/6 load balancers BEFORE the controller goes: delete every LoadBalancer Service and wait"
	@echo "   (an NLB left behind keeps its security groups, which can block VPC deletion)"
	-$(CD_KUBECTL) delete svc -A --field-selector spec.type=LoadBalancer --wait=true
	@bash -c 'for i in $$(seq 1 30); do \
	  n=$$(aws elbv2 describe-load-balancers --region us-west-2 \
	        --query "length(LoadBalancers[?starts_with(LoadBalancerName, \`k8s-inferenc\`)])" --output text); \
	  [ "$$n" = "0" ] && break; echo "   load balancers still present: $$n"; sleep 10; done; \
	  [ "$$n" = "0" ] || { echo "REFUSING to delete the cluster while $$n load balancers remain" >&2; exit 1; }'
	@echo ">> 5/6 control plane, addons and the eksctl-managed IAM"
	eksctl delete cluster -f infra/cluster/eksctl-cluster.yaml --wait
	@echo ">> 6/6 IAM that eksctl did not create: the Karpenter stack and the load balancer controller role"
	aws cloudformation delete-stack --stack-name Karpenter-inference-aware-eks --region us-west-2
	aws cloudformation wait stack-delete-complete --stack-name Karpenter-inference-aware-eks --region us-west-2
	-aws iam detach-role-policy --role-name inference-aware-eks-alb-controller \
	  --policy-arn "arn:aws:iam::$$(aws sts get-caller-identity --query Account --output text):policy/AWSLoadBalancerControllerIAMPolicy"
	-aws iam delete-role --role-name inference-aware-eks-alb-controller
	@echo ">> cluster gone. The queues, the table, the workload roles, the log group and the ECR"
	@echo "   repositories remain: see the Clean up epic for the commands that delete them."

addons:
	bash infra/addons/install-karpenter.sh
	bash infra/addons/install-keda.sh
	bash infra/addons/install-gateway-api-inference.sh crds   # Envoy Gateway + GIE CRDs only
	bash infra/addons/install-device-plugins.sh nvidia
	bash infra/addons/install-aws-load-balancer-controller.sh   # NLB, not the legacy in-tree CLB path

# Stage gates (see DESIGN.md). Each applies a subset then runs its test.
# The InferencePool + EPP trio is OWNED BY THE CHART (directive 4), so the pool is
# NOT hand-applied; manifests/03-routing/inferencepools.yaml is a reference shape.
stage-1: ensure-safety-net-armed
	kubectl apply -f manifests/00-namespace-rbac.yaml
	kubectl apply -f manifests/07-safety-net/safety-net.yaml   # BEFORE anything can provision
	kubectl apply -f manifests/01-nodepools/ec2nodeclass-gpu.yaml
	kubectl apply -f manifests/01-nodepools/nodepool-interactive.yaml
	bash infra/addons/install-gateway-api-inference.sh epp-interactive   # chart owns pool+EPP
	kubectl apply -f manifests/03-routing/envoyproxy-accesslog.yaml   # BEFORE the Gateway: pins its Service to ClusterIP
	kubectl apply -f manifests/03-routing/gateway.yaml
	kubectl apply -f manifests/03-routing/inferenceobjectives.yaml
	kubectl apply -f manifests/03-routing/httproute-stage1.yaml   # interactive-only; full route at stage-3
	kubectl apply -f manifests/02-serving/chat-template-qwen3-nonthinking.yaml   # every Qwen3 tier mounts it (INV20)
	kubectl apply -f manifests/02-serving/vllm-interactive.yaml
	$(MAKE) verify-invariants
	@echo ">> waiting for the interactive tier to be Ready (node launch, image pull, model load)"
	# Wait on POD readiness, not rollout status: a Karpenter launch, a capacity retry and a large
	# image pull can outlast the Deployment's 600 s progress deadline, and rollout status gives up there.
	kubectl -n $(NS) wait --for=condition=Ready pod -l app=vllm-llama3,tier=interactive --timeout=30m
	$(MAKE) test-routing-reachable
	$(MAKE) test-ingress-exclusivity   # the cluster half: no Service exposed that the files say is not

stage-2:
	bash infra/addons/setup-async-infra.sh          # SQS+DLQ, DynamoDB, IAM, Pod Identity
	bash infra/addons/setup-sweeper.sh              # orphan sweeper: IAM role + Pod Identity. WIRED 2026-09-24: this existed and nothing called it, so the sweeper ran without an identity and failed AccessDenied every minute.
	kubectl apply -f manifests/00-namespace-rbac.yaml
	kubectl apply -f async-worker/submit-api.yaml    # producer (enqueue + deterministic job_id)
	kubectl apply -f async-worker/deployment.yaml    # consumer (conditional-claim dedup)
	kubectl apply -f manifests/04-autoscaling/keda-async-worker.yaml
	$(MAKE) test-idempotency

stage-3: ensure-safety-net-armed
	kubectl apply -f manifests/01-nodepools/nodepool-batch.yaml
	bash infra/addons/install-gateway-api-inference.sh epp-batch         # chart owns batch pool+EPP
	kubectl apply -f manifests/02-serving/chat-template-qwen3-nonthinking.yaml   # idempotent; stage-1 applies it first
	kubectl apply -f manifests/02-serving/vllm-batch.yaml
	kubectl apply -f manifests/03-routing/httproute.yaml   # full Option A route (adds batch header rule)
	$(MAKE) verify-invariants
	@echo ">> waiting for the batch tier to be Ready (node launch, image pull, model load)"
	# Wait on POD readiness, not rollout status: a Karpenter launch, a capacity retry and a large
	# image pull can outlast the Deployment's 600 s progress deadline, and rollout status gives up there.
	kubectl -n $(NS) wait --for=condition=Ready pod -l app=vllm-llama3,tier=batch --timeout=30m
	$(MAKE) test-tier-isolation
	# keda-batch (scale-to-zero on a Prometheus metric) is applied by `make observability`,
	# NOT here: with minReplicaCount 0 and no Prometheus it would scale batch to 0 and
	# break the isolation test. Run `make observability` after this gate.

# Observability + metric-based autoscaling (design section 11). Installs the
# Prometheus stack, a vLLM PodMonitor (tier label), and the Prometheus-driven KEDA
# ScaledObjects for both tiers. Run after stage-3.
# SPLIT 2026-08-07, and the split is the fix for a REGRESSION rather than a tidy-up.
#
# THE CONFLICT, which one ordering cannot satisfy: `make dry-run` needs the monitoring CRDs to
# exist EARLY (it validates PodMonitor manifests against them), while the KEDA ScaledObjects must
# be applied LATE, after stage-3, because the batch ScaledObject has minReplicaCount 0 and would
# scale the batch tier away before test-tier-isolation can flood it.
#
# A single `observability` target did both, so any ordering of it satisfied one constraint and
# silently broke the other. The published walkthrough had moved it early, which validated the
# manifests and quietly undermined the tier-isolation test the post's platform claim rests on.
# Splitting makes both constraints expressible at once, which is INV12's own shape: when one knob
# is asked to encode two independent properties, separate the knobs.
observability: observability-crds observability-scalers

# EARLY. Prometheus and the monitoring CRDs the PodMonitor manifests reference. `make dry-run`
# fails with a precondition error without these.
observability-crds:
	bash infra/addons/install-observability.sh

# LATE, AFTER stage-3. The batch ScaledObject scales to zero, so applying it before
# test-tier-isolation removes the tier the test needs to saturate.
# OPTIONAL. Ingress NetworkPolicies for inference and the gateway data plane. The script refuses unless
# the VPC CNI network policy agent is enforcing, because an unenforced policy is inert.
network-policies:
	bash infra/addons/apply-network-policies.sh

observability-scalers:
	kubectl apply -f manifests/04-autoscaling/keda-interactive.yaml
	$(MAKE) test-keda-queries      # run with both tiers warm (batch not yet scale-to-zero)
	kubectl apply -f manifests/04-autoscaling/keda-batch.yaml   # enable batch scale-to-zero last

stage-4: ensure-safety-net-armed
	bash infra/addons/install-device-plugins.sh neuron        # advertises aws.amazon.com/neuron*
	kubectl apply -f manifests/01-nodepools/ec2nodeclass-neuron.yaml   # AL2023 Neuron AMI
	kubectl apply -f manifests/01-nodepools/nodepool-neuron.yaml
	kubectl apply -f manifests/02-serving/vllm-neuron.yaml
	@echo ">> Neuron AOT-compiles on first start (many minutes). Then confirm the fork's"
	@echo ">> actual metric names on the running pod before trusting KEDA/alarms (Spike B)."

verify-invariants:
	$(GATE) verify-invariants bash tests/verify_invariants.sh

verify-addon-pins:
	$(GATE) verify-addon-pins bash tests/test_addon_pins.sh

# Re-parameterise the example AWS account (111122223333) to your own across the whole repo.
# Usage: make set-account ACCOUNT_ID=<your 12-digit account id>
# This rewrites files in place, so it is fenced three ways: it refuses to run on a dirty
# working tree (git is then the undo, via `git checkout .`), it skips directories that must
# never be rewritten, and -I skips binaries. Previously it excluded only .git, so it also
# rewrote .venv/ and the archived measurement evidence under loadtest/results/.
hf-secret:
	@test -n "$$HF_TOKEN" || { echo "usage: HF_TOKEN=hf_xxx make hf-secret"; \
	  echo "  Only needed for a GATED model (meta-llama/Llama-3.1-8B-Instruct)."; \
	  echo "  The default ungated mirror (NousResearch/...) needs no Secret; the"; \
	  echo "  secretKeyRef is optional, so Stage 1 comes up without this."; exit 1; }
	@# THE TOKEN STAYS OUT OF OUTPUT AND OUT OF EVERY COMMAND LINE THIS RECIPE RUNS. `@` stops make
	@# echoing the commands, no line here expands the HF_TOKEN make variable, and the value reaches
	@# kubectl on stdin rather than as a --from-literal argument that `ps` would show. Pass it in the
	@# environment, as the usage line says: `make hf-secret HF_TOKEN=...` would put it in make's
	@# own argv, which `ps` shows for as long as make runs.
	@printf '%s' "$$HF_TOKEN" | kubectl create secret generic hf-token -n inference \
	  --from-file=token=/dev/stdin --dry-run=client -o yaml | kubectl apply -f -
	@echo ">> hf-token Secret applied in namespace inference (gated model path)"

set-account:
	@test -n "$(ACCOUNT_ID)" || { echo "usage: make set-account ACCOUNT_ID=<12-digit account id>"; exit 1; }
	@echo "$(ACCOUNT_ID)" | grep -qE '^[0-9]{12}$$' || { echo "ACCOUNT_ID must be 12 digits"; exit 1; }
	@git diff --quiet && git diff --cached --quiet || { \
	  echo "ERROR: the working tree has uncommitted changes."; \
	  echo "       set-account edits files in place. Commit or stash first, so that"; \
	  echo "       'git checkout .' can undo it if the result is wrong."; exit 1; }
	@files=$$(grep -rlI '111122223333' . \
	    --exclude-dir=.git --exclude-dir=.venv --exclude-dir=node_modules \
	    --exclude-dir=results --exclude-dir=build 2>/dev/null); \
	  test -n "$$files" || { echo ">> nothing to do: no occurrences of 111122223333"; exit 0; }; \
	  echo "$$files" | while read -r f; do \
	    perl -pi -e 's/111122223333/$(ACCOUNT_ID)/g' "$$f"; echo "  updated $$f"; done
	@echo ">> account set to $(ACCOUNT_ID). Review with 'git diff' before committing."
	@echo ">> After building the worker image, pin its tag with: make set-worker-image TAG=sha-<commit>"

# Pin YOUR build of the worker image. infra/addons/run-build.sh tags the image sha-<short commit>,
# and every manifest that runs it must carry that same tag (INV15 checks that they agree). Only the
# deployable manifests are edited: captured results and test fixtures keep the tag they recorded.
WORKER_IMAGE_FILES := $$(git ls-files -- 'manifests/*.yaml' 'async-worker/*.yaml' \
	loadtest/incluster-driver.yaml tests/validation/promotion-stub.yaml)
set-worker-image:
	@test -n "$(TAG)" || { echo "usage: make set-worker-image TAG=sha-<short commit>, as printed by infra/addons/run-build.sh"; exit 1; }
	@echo "$(TAG)" | grep -qE '^sha-[0-9a-f]{7,40}$$' || { echo "TAG must look like sha-<hex commit>"; exit 1; }
	@git diff --quiet && git diff --cached --quiet || { \
	  echo "ERROR: the working tree has uncommitted changes."; \
	  echo "       set-worker-image edits files in place. Commit or stash first, so that"; \
	  echo "       'git checkout .' can undo it if the result is wrong."; exit 1; }
	@for f in $(WORKER_IMAGE_FILES); do \
	  grep -qE 'inference-async-worker:sha-[0-9a-f]+' "$$f" || continue; \
	  perl -pi -e 's/inference-async-worker:sha-[0-9a-f]+/inference-async-worker:$(TAG)/g' "$$f"; \
	  echo "  updated $$f"; done
	@$(MAKE) --no-print-directory verify-invariants
	@echo ">> worker image pinned to $(TAG). Review with 'git diff' before committing."

# Server-side validation of every manifest against the live API, CRDs included.
# Comment-only files are skipped rather than fatal: manifests/01-nodepools/efa-device-plugin.yaml
# documents a Helm install instead of declaring objects, and `kubectl apply` answers
# "no objects passed to apply" for it. Aborting there left 13 of 31 manifests unvalidated,
# including all of manifests/03-routing, so this gate could never pass.
# Failures no longer abort the loop either: one bad manifest should not hide the rest.
dry-run: ; @$(GATE) dry-run $(MAKE) --no-print-directory dry-run-body

# The body, invoked only through the wrapper above. Split out rather than edited so the
# precondition checks and the per-manifest loop are unchanged from the version that ran.
dry-run-body:
	@echo ">> precondition: CRD groups the manifests require must already be installed."
	@kubectl get crd podmonitors.monitoring.coreos.com >/dev/null 2>&1 || { \
	  echo "ERROR: monitoring.coreos.com CRDs absent. Run 'make observability' first."; \
	  echo "       Without them the PodMonitor manifests fail for a reason that has nothing"; \
	  echo "       to do with the manifests, and a green result would depend on install order."; \
	  exit 1; }
	@kubectl get crd inferencepools.inference.networking.k8s.io >/dev/null 2>&1 || { \
	  echo "ERROR: Gateway API Inference Extension CRDs absent. Run the addons install first."; exit 1; }
	@fail=0; checked=0; skipped=0; exempt=0; \
	while IFS= read -r f; do \
	  if head -1 "$$f" | grep -q 'DRY-RUN-EXEMPT'; then \
	    echo "-- $$f  (EXEMPT, declared in the file: reference shape, never applied)"; \
	    exempt=$$((exempt+1)); continue; \
	  fi; \
	  if [ -z "$$(grep -vE '^[[:space:]]*(#|$$)' "$$f")" ]; then \
	    echo "-- $$f  (SKIPPED, principled: comments only, declares no Kubernetes object)"; \
	    echo "   the only expected skip is manifests/01-nodepools/efa-device-plugin.yaml, which"; \
	    echo "   documents a Helm install rather than declaring objects. Any OTHER skip is a finding."; \
	    skipped=$$((skipped+1)); continue; \
	  fi; \
	  echo "-- $$f"; \
	  if kubectl apply --dry-run=server -f "$$f"; then checked=$$((checked+1)); \
	  else fail=$$((fail+1)); fi; \
	done < <(find manifests async-worker -name '*.yaml' | sort); \
	echo "-- validated $$checked, skipped $$skipped, exempt $$exempt, failed $$fail"; \
	[ $$skipped -le 1 ] || { echo "ERROR: $$skipped manifests skipped, expected at most 1 (the EFA doc file). A growing skip count hides unvalidated manifests."; exit 1; }; \
	[ $$exempt -le 3 ] || { echo "ERROR: $$exempt manifests DRY-RUN-EXEMPT, expected at most 3, and these three by name: epp-reference.yaml and inferencepools.yaml (chart-owned, ruled 2026-09-22) and vllm-neuron.yaml (reference for the Neuron findings, ruled 2026-09-23). THE VALUE OF THIS GATE IS THE NAMED LIST, NOT THE COUNT: a ceiling with anonymous membership lets a fourth exemption hide behind an existing one. Exemption is chart-ownership, not a way to quiet a failing manifest, and a growing count hides unvalidated ones exactly as a growing skip count would."; exit 1; }; \
	[ $$fail -eq 0 ] || { echo "ERROR: $$fail manifest(s) failed server-side validation"; exit 1; }

test-keda-queries:    ; bash tests/smoke/run_keda_queries.sh
test-routing-reachable: ; bash tests/smoke/test_routing_reachable.sh
test-ownership-guard: ; $(GATE) test-ownership-guard $(PY) tests/validation/test_ownership_guard.py
# Sweeper proof (moto, GPU-free): recovers orphans, idempotent, healthy-safe, and does
# not regress exactly-once. Re-runs the ownership guard too, so this one target is the
# full offline correctness gate for the async front door (Gate B of the sweeper pass).
test-sweeper: ; $(GATE) test-sweeper sh -c '$(PY) tests/validation/test_sweeper.py && $(PY) tests/validation/test_ownership_guard.py'
# Worker infra-rejection backoff (moto, GPU-free): a batch EPP outage does not mark healthy
# jobs failed or push them to the DLQ; a genuine 4xx still fails (ARCHITECTURE-REVIEW.md F1).
test-infra-backoff: ; $(GATE) test-infra-backoff $(PY) tests/validation/test_infra_backoff.py
# Job identity is single-sourced in async-worker/jobkey.py; this asserts object identity,
# not just equal output, so a re-defined copy cannot pass.
test-job-key: ; $(GATE) test-job-key $(PY) tests/validation/test_job_key.py
# Worker HTTP path: pooled session, connect-only retry (a read retry could double-execute
# on a GPU), SQS batch size 1.
test-worker-http-session: ; $(GATE) test-worker-http-session $(PY) tests/validation/test_worker_http_session.py
# Redemption route GET /jobs/{job_id} (design v6.1 section 5.1 step 5): every actionable
# state distinguishable, percent-encoded '#' round-trips, no internal attribute leaks.
# Drives a real http.server on a loopback port, so URL parsing is exercised, not simulated.
test-redemption-route: ; $(GATE) test-redemption-route $(PY) tests/validation/test_redemption_route.py
# Section 7 guards. Required to EXIST before any classifier does, so they are proven before there
# is anything to guard. Properties: the kill switch flips under load without dropping requests,
# and each guard degrades to the floor rather than rejecting.
test-guards: ; $(GATE) test-guards $(PY) tests/validation/test_guards.py
# Chain B's committed constants: strictly ordered, and the two values it RESTATES (the route
# timeout and the load balancer idle timeout) must agree with the files that actually apply
# them. Not INV14, which needs the relay; this is the part checkable today.
test-chain-b: ; $(GATE) test-chain-b $(PY) tests/test_chain_b_constants.py
# The relay's classification duty: free signals in process, the 2,048-token context bound as a
# capacity fact rather than a difficulty guess, fail-open to the floor, and a client that
# retries nothing (the worker's connect retry is safe only because a claim sits below it).
test-relay-classifier: ; $(GATE) test-relay-classifier $(PY) tests/validation/test_relay_classifier.py
# The front doors' resource bounds (deep security scan finding M1): size, framing, read deadline,
# socket timeout and in-flight cap, against the REAL handlers on loopback with raw sockets.
test-request-bounds: ; $(GATE) test-request-bounds $(PY) tests/validation/test_request_bounds.py
# cluster-down deletes only on the cluster the eksctl config names (finding M2). Runs the real
# target with fake aws, kubectl and eksctl, and exercises every guard branch, the passing one included.
test-cluster-down-context: ; $(GATE) test-cluster-down-context bash tests/test_cluster_down_context.sh
# Per-caller identity at the ingress (decision signed 2026-08-04). The property that matters
# most is that the failure mode is DENY: an auth layer that fails open reads as protection.
# The promotion actor's decision logic (section 5.4). The first property protects CALLERS
# rather than capacity: a streaming caller is never silently truncated.
test-promotion: ; $(GATE) test-promotion $(PY) tests/validation/test_promotion.py

# THE BATCH-OPEN PRECONDITION, AS A TARGET RATHER THAN A PRACTICE.
# Three certificate lapses have now cost mid-batch time, and the check that would have caught
# all three was recommended two batches before it was written. Specifying a discipline twice
# is enough evidence that specifying it does not work; this ends it. Its output opens every
# batch report, so an unopened batch is visible in the record rather than only in the moment.

# THE COMMIT GATE. Two observed failure modes made inexpressible rather than discouraged:
# committing on a red gate, and a backtick-executed inline message. Discipline held in memory
# fails under fatigue; this project's answer has always been structure.
#   make commit MSG=.commitmsg-foo.txt
commit: ; @bash tests/commit-gate.sh "$(MSG)"

# Install the committed hooks. .git/hooks is not version controlled, so a hook that lives
# only in one working copy protects only that copy.
hooks: ; @git config core.hooksPath .githooks && echo "hooks installed: core.hooksPath -> .githooks"

# PRE-BRING-UP PRECONDITIONS. Run before scaling any accelerator workload above zero.
#
# WHY IT ASSERTS RATHER THAN INSTALLS. `make addons` already contains the device plugin, and the
# 2026-08-03 rebuild still came up without it, because the rebuild ran the addon scripts A LA CARTE
# rather than through the target. A sequence that contains the right step does not help if the
# sequence is not the thing that runs. So this checks the RESULT, not the recipe.
#
# THE INCLUSION TEST, from the 2026-08-05 session: anything whose absence causes PAID IDLE TIME and
# which can be verified WITHOUT a GPU belongs here. Both misses that session qualify: the cost
# safety net was absent (unbounded spend if the session were abandoned) and the device plugin was
# absent (22 minutes of a g6e idling while a pod sat Pending).
#
# NODE CAPACITY ARRIVING AND NODE CAPACITY BEING SCHEDULABLE ARE DIFFERENT CONDITIONS. That is the
# specific lesson: the node was healthy, joined, and carried the GPU taint, and the pod could not
# run on it because nothing advertised nvidia.com/gpu.
# ARM THE SAFETY NET. A deadline in the future, after which the CronJob scales serving to zero.
#
# WHY THIS TARGET EXISTS. The safety net reads deadline_epoch from a ConfigMap and treats 0 as
# DISARMED, which is the correct default (a net that fires when nobody armed it would kill an
# unrelated workload). But arming it was a manual step nobody had automated, so on 2026-08-07 a
# bring-up ran behind a preflight that said "cost safety net present" while every run of the net
# logged "safety-net DISARMED (deadline_epoch=0), noop".
#
#   make arm-safety-net MINUTES=90
# THE ARMING GUARD THE ACCELERATOR STAGES DEPEND ON. Idempotent: if the net is already armed with
# a future deadline it is left alone, so re-running a stage does not silently extend the bound.
#
# WHY A DEPENDENCY RATHER THAN A REMINDER. On 2026-08-07 four g6e.xlarge ran for three hours behind
# a preflight that said "cost safety net present" while the net logged "DISARMED, noop" every two
# minutes. Arming was a manual step, and a manual step in a sequence nobody re-reads is a step that
# does not happen. THE PREFLIGHT REFUSAL STAYS as the second layer: this one covers the target path,
# the preflight covers manifests applied directly. Two layers, and the consolidation lesson from the
# commit gate applies in reverse here because these two guards cover DIFFERENT entry points rather
# than the same one twice.
SAFETY_MINUTES ?= 120
# THE CONFIGMAP NAME IS DERIVED FROM THE MANIFEST THE CRONJOB MOUNTS, never restated here.
#
# THE DEFECT THIS EXISTS TO PREVENT, found by proving the net rather than by review, minutes after
# writing it: the first version of these targets wrote to `safety-net-config` while the CronJob
# mounts `spike-safety-net`. Arming reported "OK safety net ARMED, fires in 119 min" and was
# INERT: a governed value nothing consumes, which is the shape INV11 and INV14's consumption checks
# were built for, reproduced in the guard that bounds money. It was caught only because the proof
# step is "watch it fire", not "watch it report armed".
SAFETY_CM = $(shell awk '/name: config/{f=1} f&&/configMap:/{g=1} g&&/name:/{print $$2; exit}' manifests/07-safety-net/safety-net.yaml)
ensure-safety-net-armed:
	@dl=$$(kubectl -n inference get cm $(SAFETY_CM) -o jsonpath='{.data.deadline_epoch}' 2>/dev/null); \
	now=$$(date +%s); \
	if [ -n "$$dl" ] && [ "$$dl" != "0" ] && [ "$$dl" -gt "$$now" ]; then \
	  echo "OK   safety net already armed, fires in $$(( (dl - now) / 60 )) min"; \
	else \
	  $(MAKE) --no-print-directory arm-safety-net MINUTES=$(SAFETY_MINUTES); \
	fi

arm-safety-net:
	@test -n "$(MINUTES)" || { echo "usage: make arm-safety-net MINUTES=90"; exit 1; }
	@dl=$$(( $$(date +%s) + $(MINUTES) * 60 )); \
	kubectl -n inference create cm $(SAFETY_CM) --from-literal=deadline_epoch=$$dl \
	  --dry-run=client -o yaml | kubectl apply -f - >/dev/null; \
	echo "safety net ARMED: fires in $(MINUTES) min (deadline_epoch=$$dl)"

disarm-safety-net:
	@kubectl -n inference create cm $(SAFETY_CM) --from-literal=deadline_epoch=0 \
	  --dry-run=client -o yaml | kubectl apply -f - >/dev/null
	@echo "safety net DISARMED. Only correct with zero accelerators running; verify on the AWS side."

preflight-bringup:
	@fail=0; \
	kubectl -n inference get cronjob cost-safety-net >/dev/null 2>&1 \
	  && echo "OK   cost safety net present" \
	  || { echo "FAIL cost safety net ABSENT. Apply manifests/07-safety-net/safety-net.yaml BEFORE"; \
	       echo "     bring-up: after is when it is needed and too late to add."; fail=1; }; \
	dl=$$(kubectl -n inference get cm $(SAFETY_CM) -o jsonpath='{.data.deadline_epoch}' 2>/dev/null); \
	now=$$(date +%s); \
	if [ -z "$$dl" ] || [ "$$dl" = "0" ]; then \
	  echo "FAIL cost safety net is PRESENT but DISARMED (deadline_epoch=$${dl:-absent}). Every run is"; \
	  echo "     a no-op. PRESENT and WILL FIRE are different properties, and this check asserted the"; \
	  echo "     weaker one until 2026-08-07, when four g6e.xlarge ran for three hours behind a green"; \
	  echo "     preflight. Arm it: make arm-safety-net MINUTES=90"; fail=1; \
	elif [ "$$dl" -le "$$now" ]; then \
	  echo "FAIL cost safety net deadline is in the PAST ($$dl <= $$now), so it will fire immediately"; \
	  echo "     and scale the bring-up away. Re-arm it: make arm-safety-net MINUTES=90"; fail=1; \
	else \
	  echo "OK   cost safety net ARMED, fires in $$(( (dl - now) / 60 )) min"; \
	fi; \
	if kubectl get ds -A -o name 2>/dev/null | grep -qi neuron; then \
	  echo "OK   neuron device plugin installed"; \
	else \
	  echo "FAIL neuron device plugin ABSENT. Same failure as the nvidia one: an inf2 node joins,"; \
	  echo "     carries the aws.amazon.com/neuron taint, advertises NO neuron resource, and the pod"; \
	  echo "     sits Pending on a paid instance. 'make addons' installs the nvidia one only;"; \
	  echo "     'bash infra/addons/install-device-plugins.sh neuron' installs this one."; fail=1; \
	fi; \
	if kubectl get ds -A -o name 2>/dev/null | grep -q nvidia-device-plugin; then \
	  echo "OK   nvidia device plugin installed"; \
	else \
	  echo "FAIL nvidia device plugin ABSENT. 'make addons' contains it; running the addon scripts"; \
	  echo "     individually skips it. A node will join, carry the GPU taint, advertise NO"; \
	  echo "     nvidia.com/gpu, and your pod will sit Pending on a paid instance."; fail=1; \
	fi; \
	n=$$(kubectl get nodes -l karpenter.sh/nodepool -o jsonpath='{range .items[*]}{.status.allocatable.nvidia\.com/gpu}{"\n"}{end}' 2>/dev/null | grep -c '^[1-9]' || true); \
	if [ "$${n:-0}" -gt 0 ]; then echo "OK   $$n GPU node(s) advertise allocatable nvidia.com/gpu"; \
	else echo "NOTE no GPU node advertises allocatable nvidia.com/gpu yet. Expected before bring-up;"; \
	     echo "     re-run this AFTER the node joins and BEFORE concluding the bring-up is stuck."; fi; \
	[ $$fail -eq 0 ] || { echo "PRE-BRING-UP BLOCKED: fix the above before spending on an accelerator."; exit 1; }; \
	echo "PRE-BRING-UP OK"

# THE REBUILD INVENTORY, asserted. Generalises the IAM map to every object the running system needs.
# An object with no committed source is the finding, and it is cheaper to find here than at 22
# minutes of paid idle.
rebuild-inventory: ; @bash tests/check_rebuild_inventory.sh

# D-7's added first-byte cost, paired and interleaved. ZERO GPU: the quantity is the relay's
# added HOP, so a stub backend is the correct instrument rather than a compromise, since
# whatever the backend costs cancels in the paired difference.
measure-relay-overhead: ; @bash tests/validation/measure_relay_overhead.sh

# CLUSTER TEST, zero GPU. Mutates the relay for the duration and restores it. Not in test-offline.
# Asserts the SHAPE of a promoted re-placement in both directions, read from the SQS message.
test-promotion-route-shape: ; @bash tests/validation/test_promotion_route_shape.sh
# The running-images check on its own, for a quick cluster read without the whole invariant suite.
check-running-images: ; @$(PY) tests/check_running_images.py
# THE OFFLINE PROMISE, MEASURED. Strips credentials and kubeconfig and asserts the offline suite
# still passes, with a control probe that must FAIL under the same stripping.
test-offline-is-offline: ; @bash tests/test_offline_is_offline.sh
# THE STANDALONE BLOG POST. A SECOND generator beside the first, never replacing it. Byte-stable,
# and it asserts its own standalone property (no back-references, no em-dashes, Amazon EKS) against
# a positive control, so a clean reading cannot be a checker that matches nothing.
# THE THINKING-MODE GATE (spec-002 ruling 4.2). Qwen3 defaults to thinking ON and emits <think>
# INLINE IN content with no reasoning_content field, measured identically on Neuron vLLM 0.13.0 and
# GPU vLLM 0.24.0. A 64-token budget produced ZERO answer tokens. This asserts generated output
# carries no reasoning, and it proves itself: three controls, and the suite FAILS if any detection
# branch was never exercised by one. A checker whose branches are untested is only partly written.
test-no-thinking-leakage: ; @$(GATE) test-no-thinking-leakage $(PY) tests/test_no_thinking_leakage.py

# THE STANDING CLUSTER GATE SET. Everything that needs credentials and no accelerator.
#
# WHY THIS TARGET EXISTS AT ALL: tests/smoke/check_running_images.sh was written on 2026-08-04 with
# a committed injection proof, wired to `test-running-images`, and placed in NO AGGREGATE. It ran
# once, at authorship, and never again, while the defect it was built for sat live in the cluster
# for two days. A GATE IN NO STANDING SET IS NOT A GATE. See
# evidence/correction-inv15-cluster-half-2026-08-06.txt.
test-cluster: check-running-images test-running-images
	@echo ">> cluster gate set complete (credentials required, zero accelerator)"
# EFFICACY: for every invariant, inject the defect it exists to catch and assert it FAILS. A
# passing invariant proves the manifests are correct NOW; only injection proves the invariant
# would notice if they stopped being. Two invariants failed this question and were redesigned.
test-invariant-efficacy: ; $(GATE) test-invariant-efficacy bash tests/test_invariant_efficacy.sh
# Every document generator must parse. Covers the exact defect the v6.2.1 pass hit: a
# mismatched quote that would have shipped a wrong document had the build not been run.
test-generator-syntax: ; $(GATE) test-generator-syntax bash tests/test_generator_syntax.sh
# The CloudFormation merge guard. `aws cloudformation validate-template` was proven by injection
# to ACCEPT both failure modes, so the local checker is the only layer that rejects either.
test-cfn-merge-guard: ; $(GATE) test-cfn-merge-guard bash tests/test_cfn_merge_guard.sh
# CLUSTER-HALF of the Karpenter ownership property, companion to the offline INV16. Needs
# credentials, so it runs on the activation list rather than in test-offline.
test-karpenter-ownership: ; bash tests/smoke/check_karpenter_iam_ownership.sh
# CLUSTER HALF of INV12. The offline half reads committed files and was caught trusting an
# absent annotation as evidence of no exposure; Envoy Gateway defaults envoyService to
# LoadBalancer, so the live cluster had an open door the files said was closed. This checks
# what is actually running. Fails closed when the cluster is unreachable.
test-ingress-exclusivity: ; $(GATE) test-ingress-exclusivity bash tests/smoke/check_ingress_exclusivity.sh
# CLUSTER HALF of INV15. The offline half asserts the pins agree WITH EACH OTHER, which was
# green throughout the v8-versus-v10 incident because the three wrong pins agreed perfectly.
# The defect lived in the gap between the files and the cluster. This reads the running pods.
test-running-images: ; $(GATE) test-running-images bash tests/smoke/check_running_images.sh
test-no-gateway-retry: ; bash tests/validation/test_no_gateway_retry.sh
test-idempotency:     ; bash tests/validation/run_idempotency.sh
test-tier-isolation:  ; $(PY) tests/validation/test_tier_isolation.py

# THE TRACE PARSER AGREES WITH THE LIVE ACCESS-LOG FORMAT (2026-09-22). The format gained two fields on
# 2026-08-04 and all three parsers kept the old shape for seven weeks, invisible because every caller
# is cluster-gated. This renders the format FROM THE MANIFEST, so the next change fails offline.
test-reqtrace-format: ; @$(GATE) test-reqtrace-format $(PY) tests/validation/test_reqtrace_format.py

# EVERY QUEUE PRODUCER'S ROUTING REACHES THE WORKER'S WIRE CALL (2026-09-22). Real producers (promotion,
# submit API over a socket, sweeper after a worker death), ending at run_inference's arguments.
test-message-contract: ; @$(GATE) test-message-contract $(PY) tests/validation/test_message_contract.py

# THE NON-THINKING TEMPLATE IS WHAT ITS HEADER SAYS (2026-09-22). Source hash, exactly one recorded
# substitution against the pinned upstream, and render-equivalence to upstream with thinking off
# whatever the request passes, with a control proving the equivalence discriminates. Offline.
test-chat-template-provenance: ; @$(GATE) test-chat-template-provenance $(PY) tests/validation/test_chat_template_provenance.py

# THE PROOF HALF OF THE THINKING CONTROL, against the live cluster: every routable Qwen3 cell with
# ready capacity must answer at a 64-token budget with zero think tokens and finish_reason stop.
# Cells at zero replicas report PEND (never a pass); a run that checks nothing exits 2.
test-thinking-live: ; $(PY) tests/test_no_thinking_leakage.py --live

# Everything provable without a cluster, GPU, or AWS credentials. This is the suite a
# reader can run on a fresh clone after `make venv`, and the one to run before any commit.
test-offline: verify-invariants verify-addon-pins test-job-key test-worker-http-session \
              test-ownership-guard test-sweeper test-infra-backoff test-redemption-route \
              test-generator-syntax test-cfn-merge-guard test-guards test-chain-b \
              test-relay-classifier test-promotion \
              test-offline-is-offline \
              test-no-thinking-leakage test-reqtrace-format test-chat-template-provenance test-message-contract \
              test-invariant-efficacy test-request-bounds test-cluster-down-context
	@echo ">> offline suite complete (no cluster, no GPU, no credentials)"

validate: test-keda-queries test-routing-reachable test-idempotency test-tier-isolation
	@echo ">> full validation suite complete"


# ---- Load-test suite (loadtest/). Needs the scaled cluster, the client node group,
# and Prometheus port-forwarded. Set GATEWAY / SUBMIT_URL in the environment. ----
.PHONY: loadtest-deps loadtest-client-up loadtest-client-down loadtest-lt3 loadtest-lt8 \
        loadtest-lt9 loadtest-sync
loadtest-deps:        ; ./.venv/bin/pip install -r loadtest/requirements.txt
loadtest-client-up:   ; eksctl create nodegroup -f loadtest/client-nodegroup.yaml
loadtest-client-down: ; eksctl delete nodegroup --cluster inference-aware-eks --name loadgen
loadtest-lt3:         ; bash loadtest/run_lt3.sh
# LT-8 needs P, JOBS, PREFIX_REPS from loadtest/results/lt8/cache_derivation_batch.md (one live read).
loadtest-lt8:         ; bash loadtest/run_lt8.sh
loadtest-lt9:         ; bash loadtest/run_lt9.sh
# Generic sync driver for LT-1/LT-2/LT-4/LT-7. Parametrize:
#   make loadtest-sync WORKLOAD=prefix-heavy TIER=interactive ROUTER=epp USERS=100 TIME=5m
WORKLOAD ?= interactive-short
TIER ?= interactive
ROUTER ?= epp
USERS ?= 50
TIME ?= 5m
loadtest-sync: ; GATEWAY="$(GATEWAY)" WORKLOAD="$(WORKLOAD)" TIER="$(TIER)" ROUTER="$(ROUTER)" \
	locust -f loadtest/locust_sync.py --headless -u $(USERS) -r 20 --run-time $(TIME) --only-summary
