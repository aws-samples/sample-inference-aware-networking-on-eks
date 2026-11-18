#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# ---------------------------------------------------------------------------------------------
# AUTHORING CHECKLIST FOR A NEW INVARIANT. Read this before writing one, not before reviewing one.
#
# It lives HERE, at the top of the harness a new fixture is copied from, because the same lesson
# written in a conventions file was learned twice by two different invariants in two days. Four
# lines at the point of authorship beat a convention file at the point of review.
#
#   1. CONSUMPTION CHECKS PARSE STRUCTURE, NEVER TEXT ADJACENCY. "the file contains X and also
#      contains Y" is not "X is configured from Y". INV14 and INV11 both shipped this mistake and
#      both were caught by their own fixture rather than by review. Use a parser
#      (tests/check_relay_chain_source.py is the pattern).
#   2. EVERY NEW INVARIANT SHIPS WITH ITS FIXTURE, in the same change set. An invariant nobody has
#      tried to defeat is not evidence.
#   3. THE FIXTURE QUOTES ITS INCIDENT from the source, and reproduces THAT state rather than a
#      plausible neighbour. INV15's fixture tested a paraphrase and certified a check that was green
#      throughout the incident it existed to prevent.
#   4. A PASSING PATH CARRIES DEBT-PASSING-PATH until it has executed against the real target.
#      Injection proves the logic given its inputs; it does not prove the inputs are obtainable.
#   5. A DIAGNOSIS WITHOUT A CURE IS A COMMENT, and it is worse than no diagnosis because it
#      RETIRES THE CONCERN WHILE LEAVING THE DEFECT. When you diagnose a defect here, name the cure
#      and make the cure VERIFIABLE. The demonstration is the paragraph below about INV15 and
#      v8-versus-v10: written 2026-08-04, correct in every word, and it produced no check for two
#      days while reading as though the matter were handled. ITS CURE NOW EXISTS AND IS NAMED THERE.
#   6. A COMMIT TITLE IS A CLAIM. Before committing, ask the one-line question of any title that
#      asserts a wiring, a fix or a proof: DOES THE DIFF CONTAIN IT? This is judgment and not a
#      gate, and it is recorded as practice because the alternative is pretending it can be
#      mechanised. The costliest instance: "INV15's cluster half" named a wiring never performed,
#      and was read as fact for four batches.
# ---------------------------------------------------------------------------------------------
# EFFICACY HARNESS: for every invariant, inject the defect it exists to catch and assert it FAILS.
#
# WHY THIS EXISTS. Two invariants have been asked "would you fail on the defect you exist to catch?"
# and BOTH needed redesigning: INV7 compared line numbers as a proxy for a precedence the spec does
# not define that way, and INV15's first sketch checked consistency without correctness. Two for two
# is a strong prior against the rest, so the question is now asked of every entry, mechanically and
# repeatedly, rather than once per review.
#
# The distinction this harness enforces: a passing invariant proves the manifests are currently
# correct. It does NOT prove the invariant would notice if they stopped being correct. Only
# injection proves that, and only a harness makes it true after the next edit as well.
#
# THE BOUNDARY OF WHAT THIS GATE CAN CERTIFY, adopted 2026-08-04 as a CONDITION ON THE METHOD
# rather than as a note about any one entry:
#
#     INJECTION PROVES THE LOGIC GIVEN ITS INPUTS. IT DOES NOT PROVE THE INPUTS ARE OBTAINABLE.
#     For any check with a cluster half, the PASSING state must be exercised against a real
#     cluster before the check counts as proven.
#
# The demonstration is INV12. It survived an eight-state injection proof, every FAIL branch firing
# exactly as designed, and then its OK branch failed on a correct cutover because that branch
# required zero externally exposed Services while the relay must BE one. Every injection had
# exercised a failing state; nothing had ever reached the passing state, so nothing had discovered
# it was unreachable. A gate that only ever proves things go red cannot tell you they can go green.
#
# Applies forward to INV13 (its OK branch needs a serving replica, so it is unproven until one
# exists) and to INV14 (proven green against the applied cutover on 2026-08-04). It is recorded
# here, in the meta-gate itself, because this gate certifies fixtures and this is precisely the
# edge of what a fixture can certify.
#
# THE SECOND CONDITION ON THE METHOD, adopted 2026-08-04 alongside the first:
#
#     A FIXTURE MUST REPRODUCE THE INCIDENT, NOT A PARAPHRASE OF IT, AND THE INCIDENT IS QUOTED
#     FROM ITS SOURCE RATHER THAN REMEMBERED.
#
# The demonstration is INV15. Batch 2's audit certified it thirteen-for-thirteen and that audit was
# honest: the fixture (pins DISAGREEING with each other) did fail the check, exactly as designed.
# But the v8-versus-v10 incident was pins AGREEING with each other and disagreeing with the CLUSTER.
# The fixture encoded a plausible neighbour of the incident, so it certified a check that was green
# throughout the very event it was built to prevent.
#
# THE CURE, NAMED HERE ON 2026-08-06 BECAUSE THIS PARAGRAPH WENT TWO DAYS WITHOUT ONE. INV19
# (tests/check_running_images.py, fixtures below) reads the CLUSTER and is injection-proven against
# CAPTURED states of the real 2026-08-06 drift. tests/smoke/check_running_images.sh, written in the
# same commit as this paragraph, reads pod status and is now in `make test-cluster` rather than in
# no aggregate at all, which is why it never ran. See evidence/correction-inv15-cluster-half-2026-08-06.txt.
# This loop is closed; checklist item 5 above exists so the next one does not stay open as long.
#
# Both conditions have the same root. The first says a fixture cannot tell you a passing state is
# reachable; the second says a fixture cannot tell you it tested the right failing state. Neither is
# discoverable by reading the fixture; both need the source of the incident in hand.
#
# THIS GATE'S OWN FIRST REAL CATCH, recorded as its justifying incident: INV14's consumption check
# was two independent greps, "contains configMapKeyRef" and "contains
# RELAY_UPSTREAM_TIMEOUT_SECONDS". Both can hold while unrelated. The moment the relay gained a
# SECOND ConfigMap reference for the discovered gateway address, the hardcoding injection satisfied
# both greps and silently stopped failing. The fixture caught it; a human reading the check would
# very likely not have. That is what this harness is for.
#
# Each case: apply a defect, assert verify_invariants.sh reports THAT invariant as FAIL, revert,
# and confirm the suite is green again. A case that fails to fail is the finding.
#
# Offline. No cluster, no credentials.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1
# THIS HARNESS IS OFFLINE, and INV19 is the one invariant that reaches a cluster. Skipping it here
# keeps that promise true and keeps a kubectl round trip out of thirty-odd inner runs. INV19 is
# covered below by CAPTURED CLUSTER STATES, which are better evidence than a live read taken inside
# a loop that is not testing it: a capture is the incident, and it replays on any machine forever.
export INV19_SKIP=1

# ONE RUN AT A TIME. This harness INJECTS DEFECTS INTO THE WORKING TREE and reverts them, so two
# concurrent runs corrupt each other's subjects and produce DID-NOT-CATCH lines about invariants
# that are perfectly healthy. That happened on 2026-08-06: a backgrounded run and a foreground run
# overlapped, INV1 reported DID NOT CATCH, and reproducing the same injection in isolation showed
# INV1 catching it exactly as designed. The finding was in the harness, not the invariant, and the
# output gave the reader no way to tell. A lock makes the situation unreachable rather than rare.
LOCK=.efficacy.lock
if ! mkdir "$LOCK" 2>/dev/null; then
  echo "REFUSED: another efficacy run holds $LOCK. This harness mutates the working tree, so two"
  echo "runs at once produce DID-NOT-CATCH lines about healthy invariants. Wait, or remove $LOCK if"
  echo "you are certain no run is in flight."
  exit 1
fi
trap 'rmdir "$LOCK" 2>/dev/null' EXIT
BAK=$(mktemp -d); trap 'restore_all; rm -rf "$BAK"; rmdir "$LOCK" 2>/dev/null' EXIT
fail=0; n=0

save(){ mkdir -p "$BAK/$(dirname "$1")"; cp "$1" "$BAK/$1"; }
restore_all(){ (cd "$BAK" 2>/dev/null && find . -type f 2>/dev/null | while IFS= read -r f; do cp "$f" "$OLDPWD/${f#./}" 2>/dev/null; done) || true; }
restore(){ cp "$BAK/$1" "$1"; }

# case <INV> <file> <perl-expr> <description>
#
# THE EXPRESSION RUNS THROUGH `perl -0777`, WHICH SLURPS THE WHOLE FILE. Two consequences bite, and
# both bit on 2026-08-04 while adding the mirror fixtures below:
#   - `s///` without /g replaces only the FIRST match in the ENTIRE file, not the first per line.
#   - `^` and `$` anchor to the start and end of the STRING unless /m is given.
# A fixture with either mistake reports "DID NOT CATCH", which reads as an invariant failure and is
# actually a fixture failure. The two are indistinguishable from the output alone, so the rule is to
# confirm the injection actually changed the file before believing the invariant is at fault.
case_inv(){
  local inv="$1" file="$2" expr="$3" desc="$4"
  n=$((n+1))
  save "$file"
  perl -0777 -pi -e "$expr" "$file"

  # ASSERT THE INJECTED STATE ACTUALLY EXISTS BEFORE JUDGING THE INVARIANT.
  #
  # Without this, "DID NOT CATCH" names the wrong layer. A weak check and a failed injection produce
  # the identical line, and the reader has no way to tell which they are looking at. That cost a
  # detour on 2026-08-04: three fixtures reported DID NOT CATCH and every one was a FIXTURE bug,
  # because `perl -0777` slurps the whole file, so `s///` without /g replaces only the first match
  # in the ENTIRE file and `^` anchors to the string rather than each line. The invariants were fine
  # the whole time.
  #
  # The check is deliberately the weakest thing that distinguishes the two: did the bytes change at
  # all. It cannot tell whether the RIGHT change was made, and claiming otherwise would be the
  # proxy-for-a-property mistake this project keeps finding. What it does guarantee is that a
  # DID-NOT-CATCH line is about the invariant and never about the fixture.
  if cmp -s "$BAK/$file" "$file"; then
    printf "FAIL %-6s INJECTION DID NOTHING, so this case proves nothing about the invariant: %s\n" "$inv" "$desc"
    printf "       the expression left the file byte-identical. Check the perl expression, not the\n"
    printf "       invariant: -0777 slurps the file, so s/// needs /g for every match and ^ needs /m.\n"
    restore "$file"
    fail=1
    return
  fi

  local out; out=$(bash tests/verify_invariants.sh 2>&1)
  restore "$file"
  if printf '%s' "$out" | grep -qE "^FAIL ${inv}\b"; then
    printf "OK   %-6s INJECTED and caught: %s\n" "$inv" "$desc"
  else
    printf "FAIL %-6s DID NOT CATCH its own target defect: %s\n" "$inv" "$desc"
    printf "       (the injection DID change the file, so this is the invariant, not the fixture)\n"
    printf '%s' "$out" | grep -E "^(FAIL|PEND)" | sed 's/^/       observed: /' | head -3
    fail=1
  fi
}

# HARDENING 3: THE SUITE MUST BE GREEN BEFORE ANY DEFECT IS INJECTED.
#
# THIRD INSTANCE OF THE SAME AMBIGUITY, which is why it is now asserted rather than assumed. A
# DID-NOT-CATCH line had three possible causes: a weak invariant, a fixture whose expression matched
# nothing, and a tree that was already dirty. The byte-diff assertion in case_inv removed the second.
# This removes the third: a leaked injection from an interrupted run, or a concurrent run, or an
# uncommitted edit, all show up here as a red starting state instead of as a slander against a
# healthy invariant. Two causes left, and they are distinguishable.
pre=$(bash tests/verify_invariants.sh 2>&1)
if ! printf '%s' "$pre" | grep -q "ALL INVARIANTS PASS"; then
  echo "REFUSED: the suite is NOT green before any injection, so every DID-NOT-CATCH line below"
  echo "would be unattributable. Fix the tree first. Failing invariants:"
  printf '%s' "$pre" | grep -E '^FAIL' | sed 's/^/  /'
  exit 1
fi
echo "=== pre-flight: suite green before any injection, so a DID-NOT-CATCH line is attributable ==="

echo "=== injecting each invariant's target defect ==="

# INV1: the chart's pool selector and the Deployment pod labels drift apart, so the pool selects
# nothing and the Endpoint Picker has no endpoints to score.
case_inv INV1 infra/addons/install-gateway-api-inference.sh \
  's/matchLabels\.app=vllm-llama3/matchLabels.app=vllm-DRIFTED/' \
  "chart pool selector no longer matches the Deployment pod labels"

# INV2: both objectives point at ONE pool, which collapses tier isolation silently.
case_inv INV2 manifests/03-routing/inferenceobjectives.yaml \
  's/name: vllm-llama3-batch/name: vllm-llama3-interactive/' \
  "both InferenceObjectives point at the interactive pool"

# INV3: a pool references an Endpoint Picker the chart does not deploy.
case_inv INV3 manifests/03-routing/inferencepools.yaml \
  's/vllm-llama3-batch-epp/vllm-llama3-batch-WRONG-epp/' \
  "endpointPickerRef names an EPP the chart never creates"

# INV4: KEDA targets the InferencePool, which has no /scale subresource, so the scaler sits inert.
case_inv INV4 manifests/04-autoscaling/keda-interactive.yaml \
  's/kind: Deployment/kind: InferencePool/' \
  "ScaledObject targets an InferencePool instead of a Deployment"

# INV5: a tier loses its accelerator toleration, so it no longer lands on its own NodePool and the
# LT-4 tier isolation property quietly stops being true. Moved here from ELSEWHERE (4247c11) so the
# proof runs in every checkout, including a history-free export, instead of naming a commit.
case_inv INV5 manifests/02-serving/vllm-batch.yaml \
  's/^[ \t]*tolerations:\n(?:[ \t]*- \{ key: (?:nvidia\.com\/gpu|aws\.amazon\.com\/neuron)[^\n]*\n)+//mg' \
  "the batch tier declares no accelerator toleration"

# INV7: the historical defect exactly. The batch rule matches the header only, so a longer-path
# interactive rule outranks it under Gateway API precedence and batch traffic reaches the
# interactive pool. Moved here from ELSEWHERE (4247c11).
case_inv INV7 manifests/03-routing/httproute.yaml \
  's/- path:\n\s+type: PathPrefix\n\s+value: \/v1\n\s+headers:(\n\s+- type: Exact\n\s+name: x-gateway-inference-objective\n\s+value: batch\n\s+- type: Exact\n\s+name: x-inference-model-class\n\s+value: standard\n\s+backendRefs:\n\s+- group: inference\.networking\.k8s\.io\n\s+kind: InferencePool\n\s+name: vllm-llama3-batch\n)/- headers:$1/' \
  "the batch rule matches the objective header but not the /v1 path"

# INV9: the worker's objective is flipped to interactive, which routes queued work past the relay's
# vantage point; and the objective is removed outright. Moved here from ELSEWHERE (ca279d3).
case_inv INV9 async-worker/deployment.yaml \
  's/INFERENCE_OBJECTIVE, value: "batch"/INFERENCE_OBJECTIVE, value: "interactive"/' \
  "the worker's INFERENCE_OBJECTIVE is interactive instead of batch"
case_inv INV9 async-worker/deployment.yaml \
  's/^[ \t]*- \{ name: INFERENCE_OBJECTIVE, value: "batch" \}\n//m' \
  "the worker sets no INFERENCE_OBJECTIVE at all"

# INV6: the manifest asserts a FailOpen the platform provably drops (LT-7 + config_dump).
case_inv INV6 manifests/03-routing/inferencepools.yaml \
  's/failureMode: FailClose          # invariant 6: batch is FailClose/failureMode: FailOpen          # invariant 6: batch is FailClose/' \
  "a pool asserts FailOpen, which Envoy AI Gateway v1.0.0 does not honour"

# INV8: an object lands in a namespace where nothing selects it.
case_inv INV8 manifests/04-autoscaling/keda-interactive.yaml \
  's/namespace: inference/namespace: default/' \
  "a manifest declares a namespace other than inference"

# INV10: the timeout chain loses its strict ordering, which is the R.5 defect exactly: equal values
# race, and a redelivered worker can double-run a job the first is still writing.
case_inv INV10 async-worker/deployment.yaml \
  's/CLAIM_TTL_SECONDS, value: "750"/CLAIM_TTL_SECONDS, value: "600"/' \
  "claim lease equals the route timeout, so the chain is no longer strictly ordered"

# INV15: the historical defect exactly, manifests pinned to a counter tag with no provenance.
case_inv INV15 async-worker/deployment.yaml \
  's/inference-async-worker:sha-[0-9a-f]+/inference-async-worker:v8/' \
  "image pin reverts to a counter tag carrying no provenance"

# INV16: an EC2NodeClass uses spec.role, so Karpenter mints an instance profile outside the stack.
case_inv INV16 manifests/01-nodepools/ec2nodeclass-gpu.yaml \
  's/  instanceProfile: KarpenterNodeInstanceProfile-inference-aware-eks/  role: KarpenterNodeRole-inference-aware-eks/' \
  "EC2NodeClass uses spec.role, so Karpenter mints unowned IAM"

# INV12, RE-INJECTED 2026-08-04 AGAINST ITS NEW LOGIC. Its previous proof (dd35138) covered the
# old rule, which counted external Services rather than attributing them, and that rule has been
# corrected: it demanded ZERO external exposures and so made its own POST-RELAY state unreachable.
# A proof of a superseded rule is not a proof of the current one, so INV12 leaves ELSEWHERE and is
# fixtured here. That is the migration backlog trending to zero, as recorded.
#
# The case that matters most is the SECOND DOOR: a stray external Service alongside the relay is
# exactly the bypass INV12 exists to close, and the old rule could not tell it from the relay.
case_inv INV12 manifests/04-autoscaling/keda-interactive.yaml \
  's/kind: ScaledObject/kind: Service\nspec:\n  type: LoadBalancer/' \
  "a SECOND externally exposed Service appears alongside the relay (the bypass)"

# The half-finished cutover: the relay is added but the gateway keeps its external annotation, so
# both doors are open at once. This is the state the one-change-set rule exists to forbid.
case_inv INV12 manifests/03-routing/envoyproxy-accesslog.yaml \
  's/  telemetry:/  provider:\n    type: Kubernetes\n    kubernetes:\n      envoyService:\n        annotations:\n          service.beta.kubernetes.io\/aws-load-balancer-connection-idle-timeout: "690"\n  telemetry:/' \
  "half-finished cutover: the relay exists AND the gateway is still externally exposed"

# The relay's exposure loses the label that identifies it, so it becomes indistinguishable from a
# stray. Proves the check keys on the label rather than on the Service name.
case_inv INV12 manifests/08-relay/relay.yaml \
  's/    inference-aware-eks\/ingress-role: relay/    inference-aware-eks\/ingress-role: something-else/' \
  "the relay exposure loses its ingress-role label and reads as an unpermitted door"

# The COMFORTABLE PROXY case, and the reason INV12 gained a positive assertion. Envoy Gateway
# defaults envoyService to LoadBalancer, so merely dropping the annotation leaves the data plane
# just as reachable with the AWS default idle timeout. Confirmed against the live cluster on
# 2026-08-04, where the gateway Service was type LoadBalancer with an active external hostname while
# the manifest carried no annotation at all. Removing the explicit ClusterIP must go RED.
case_inv INV12 manifests/03-routing/envoyproxy-accesslog.yaml \
  's/        type: ClusterIP\n//' \
  "the gateway envoyService stops pinning ClusterIP and silently reverts to the LoadBalancer default"

# THE TEMPORARY DOOR (added 2026-11-02). A load-test manifest is applied to the same cluster, and
# loadtest/lt2-rr-service.yaml was an internet-facing LoadBalancer over unauthenticated vLLM pods,
# outside every invariant, until a content security scan found it. INV12 now scans loadtest/.
case_inv INV12 loadtest/lt2-rr-service.yaml \
  's/  type: ClusterIP/  type: LoadBalancer/' \
  "a load-test manifest exposes the vLLM pods through a LoadBalancer"

# INV11, FIXTURED IN THE SAME CHANGE SET THAT IMPLEMENTED IT, so it is born with its failing
# states proven and its passing state executed against the live relay in the same batch.
#
# The defect: P at or above the route timeout. The gateway gives up before the deadline arrives, so
# the caller receives a 504 instead of a job id and the promotion path NEVER EXECUTES while
# appearing fully configured. A promise that outlives the machinery enforcing it is not enforceable.
case_inv INV11 manifests/03-routing/chain-b-timeouts.yaml \
  's/PROMOTION_DEADLINE_SECONDS: "120"/PROMOTION_DEADLINE_SECONDS: "600"/' \
  "P EQUALS the route timeout, so the gateway gives up before the deadline fires"

case_inv INV11 manifests/03-routing/chain-b-timeouts.yaml \
  's/PROMOTION_DEADLINE_SECONDS: "120"/PROMOTION_DEADLINE_SECONDS: "0"/' \
  "P is zero, so every request promotes on arrival and nothing is ever served interactively"

# The decorative-value case, the same shape INV14's third fixture catches: P is set and correctly
# ordered, and the relay does not read it, so the deadline the actor enforces is not the committed
# one and the ordering is true on paper only.
case_inv INV11 manifests/08-relay/relay.yaml \
  's/                  key: PROMOTION_DEADLINE_SECONDS/                  key: SOME_OTHER_KEY/' \
  "the relay stops reading P from the ConfigMap, making the committed deadline decorative"

# INV14, FIXTURED AT THE SAME CHANGE SET THAT IMPLEMENTED IT. The rule this project has paid for
# twice: equal values race.
case_inv INV14 manifests/03-routing/chain-b-timeouts.yaml \
  's/RELAY_UPSTREAM_TIMEOUT_SECONDS: "630"/RELAY_UPSTREAM_TIMEOUT_SECONDS: "600"/' \
  "relay upstream EQUALS the route timeout, so Chain B is no longer strictly ordered"

# RETARGETED 2026-08-07 with the NLB migration. This fixture used to edit the Classic
# `connection-idle-timeout` annotation, which no longer exists, so it silently injected NOTHING.
# THE HARNESS CAUGHT ITS OWN STALE FIXTURE: the injection-changed-the-bytes assertion reported
# "INJECTION DID NOTHING" and named the fixture rather than slandering INV14. That assertion was
# added after three fixtures were misdiagnosed as invariant failures, and this is it earning its
# place on a defect nobody was looking for.
case_inv INV14 manifests/08-relay/relay.yaml \
  's/tcp\.idle_timeout\.seconds=690/tcp.idle_timeout.seconds=620/' \
  "the exposure idle timeout drops BELOW the relay upstream timeout"

# INV14 GAINED A CASE 2026-08-07 when the relay migrated from a Classic Load Balancer to an NLB and
# L moved from an annotation to a listener attribute. The migration turned INV14 RED with an empty
# exposure value, which is the invariant working: a chain check that survives a platform change
# without noticing has stopped measuring the chain. This case guards the state AFTER the fix.
#
# BOTH ANNOTATION FORMS PRESENT. On an NLB the Classic annotation is INERT, so a relay carrying both
# has two values with only one reaching the data plane, and the inert one reads perfectly well.
# That is the reassuring-proxy family: the comfortable reading is the wrong one.
case_inv INV14 manifests/08-relay/relay.yaml \
  's|^spec:\n  type: LoadBalancer|    service.beta.kubernetes.io/aws-load-balancer-connection-idle-timeout: "690"\nspec:\n  type: LoadBalancer|m' \
  "the relay carries BOTH the Classic idle-timeout annotation and the NLB listener attribute"

# The decorative-ConfigMap case: the ordering is well-formed on paper and the relay does not read
# it. Nothing else in the repository would notice.
case_inv INV14 manifests/08-relay/relay.yaml \
  's/              valueFrom:\n                configMapKeyRef:\n                  name: chain-b-timeouts\n                  key: RELAY_UPSTREAM_TIMEOUT_SECONDS/              value: "630"/' \
  "the relay HARDCODES its timeout instead of reading the ConfigMap, making the chain decorative"

# INV18, FIXTURED IN THE SAME CHANGE SET THAT IMPLEMENTED IT, and both halves, because each is
# defeatable alone: an incomplete registry, and a call site that invents a path the registry never
# sanctioned.

# HALF 1, COMPLETENESS. The realistic incident is not someone deleting a registry entry: it is a
# NEW MODEL arriving in a serving manifest while the registry sits still, which is how every other
# drift in this repository has happened. So the manifest moves, not the registry.
case_inv INV18 manifests/02-serving/vllm-neuron.yaml \
  's|            - Qwen/Qwen3-1.7B|            - SomeOrg/Newly-Swapped-Model|' \
  "a serving manifest swaps in a model with NO model-facts entry"

# HALF 1, MIRROR, AND IT IS THE ONE THAT MATTERS FOR THE CHECKER'S OWN CORRECTNESS. The Neuron
# manifest names its model POSITIONALLY after `vllm serve`; the NVIDIA manifests use --model=.
# A checker that greps for --model= finds two of three, reports completeness, and is exactly the
# truncated-enumeration family this project keeps finding (head -4 on a model list, head -1 on an
# AWS error, wc -l counting "No resources found"). This case injects on the OTHER syntax.
case_inv INV18 manifests/02-serving/vllm-interactive.yaml \
  's|--model=Qwen/Qwen3-8B|--model=SomeOrg/Unregistered-8B|' \
  "MIRROR: the --model= syntax swaps in an unregistered model, not the positional one"

# REGISTRY SELF-CONSISTENCY. Caught by the registry's own consistency check: an instruction-tuned
# entry (the retired TinyLlama) now names the raw endpoint, which applies no chat template.
case_inv INV18 docs/specs/model-facts.yaml \
  's|    content_generation_endpoint: /v1/chat/completions\n    content_generation_note: >-\n      /v1/completions is VALID|    content_generation_endpoint: /v1/completions\n    content_generation_note: >-\n      /v1/completions is VALID|' \
  "the registry says an instruction-tuned model takes the RAW endpoint"

# HALF 2, AN INVENTED PATH. The 25th call site, which is the whole reason the audit table was not
# enough: a new script addresses a model through a path nobody sanctioned, and no table catches it
# because the table was written before the script existed.
#
# THE SPLIT LITERAL BELOW IS NOT A FLOURISH, it is the check applying to its own fixture. Half 2
# reads TEXT, so ANY committed file that merely mentions an unsanctioned path trips it, including
# the file that tests it. Written whole, this fixture left the suite red after revert. The same
# lesson landed twice in one batch (another module's explanatory comment named a real surface
# path in prose and tripped the check too), which is what makes it a property of the method rather
# than an accident: a text-scanning invariant has no way to tell a USE from a MENTION, and the
# honest response is to keep mentions out of committed text rather than to exempt the files that
# make them.
INVENTED_PATH="/v1/re""sponses"
case_inv INV18 loadtest/pub_routing.py \
  "s|^import argparse|import argparse\nNEW_CALL_SITE = \"${INVENTED_PATH}\"  # a path the registry never sanctioned|m" \
  "a NEW call site invents an endpoint path the registry does not know"

# INV20, FIXTURED IN THE SAME CHANGE SET THAT IMPLEMENTED IT, one case per link in the chain, because
# each link is defeatable alone and a chain is only as proven as its least-injected link.
case_inv INV20 manifests/02-serving/vllm-batch.yaml \
  's#            - --chat-template=/etc/vllm/chat-templates/qwen3-nonthinking\.jinja\n##' \
  "a tier drops --chat-template and silently serves upstream thinking-ON"
# MIRROR on the OTHER argument syntax: the Neuron manifest passes flag and value as two list items.
# A checker that only parses --flag=value reports three of four tiers covered and calls it complete.
case_inv INV20 manifests/02-serving/vllm-neuron.yaml \
  's#            - --chat-template\n            - "/etc/vllm/chat-templates/qwen3-nonthinking\.jinja"\n##' \
  "MIRROR: the Neuron tier drops the two-item --chat-template form"
case_inv INV20 manifests/02-serving/vllm-premium.yaml \
  's#          volumeMounts:\n            - \{ name: chat-template[^\n]*\n##' \
  "the flag stays but the mount goes: vLLM would point at a path that does not exist"
case_inv INV20 manifests/02-serving/chat-template-qwen3-nonthinking.yaml \
  's|(qwen3-nonthinking\.jinja: \|-\n)|$1    {#- a hand edit nobody reviewed -#}\n|' \
  "the ConfigMap drifts from the reviewed .jinja file (what reaches the pod is not what was reviewed)"
case_inv INV20 manifests/02-serving/chat-templates/qwen3-nonthinking.jinja \
  's|(    )(\{\{- \x27<think>\\n\\n</think>\\n\\n\x27 \}\}\n)|$1\{%- if enable_thinking is defined and enable_thinking is false %\}\n$1    $2$1\{%- endif %\}\n|' \
  "the reviewed template restores the upstream thinking branch, so a caller can switch thinking back on"

# INV19, AND ITS FIXTURE IS THE INCIDENT ITSELF RATHER THAN A REPRODUCTION OF IT.
#
# INV19 is CLUSTER-GATED, so case_inv cannot reach it: injecting into a file cannot change what a
# cluster is running. The fixture is therefore a CAPTURED CLUSTER STATE, taken from the live cluster
# on 2026-08-06 BEFORE the drift was rolled away, and committed. That ordering was deliberate: roll
# first and the true state is gone, leaving a fixture that is a reconstruction of the incident, which
# is precisely the disease this harness exists to catch. The capture is the incident, byte for byte.
#
# TWO DIRECTIONS AND ONE HOLE. The drift capture must FAIL the new check; the agreed capture must
# PASS it, so the check is not a refusal that never passes; and INV15's logic must be shown GREEN
# over the drift capture, which is the hole recorded as a fact rather than as an argument.
case_capture(){
  local expect="$1" file="$2" desc="$3" pin="$4"
  n=$((n+1))
  if [ ! -f "$file" ]; then
    printf "FAIL INV19  fixture missing: %s\n" "$file"; fail=1; return
  fi
  # JUDGED AGAINST THE PIN COMMITTED WHEN THE STATE WAS CAPTURED, never against today's manifests.
  # Without this, moving the pin (2026-09-22, f7a9a0759a93 -> 313f6d149c7e) turned the AGREED control
  # red, and the DRIFT control stayed red for the wrong reason (both workloads off, not one).
  ${PYBIN:-./.venv/bin/python} tests/check_running_images.py --from-json "$file" --committed-pin "$pin" >/dev/null 2>&1
  local rc=$?
  if [ "$rc" -eq "$expect" ]; then
    printf "OK   INV19  CAPTURED STATE behaves as designed (rc=%s): %s\n" "$rc" "$desc"
  else
    printf "FAIL INV19  CAPTURED STATE gave rc=%s, expected %s: %s\n" "$rc" "$expect" "$desc"
    ${PYBIN:-./.venv/bin/python} tests/check_running_images.py --from-json "$file" --committed-pin "$pin" 2>&1 | sed 's/^/       /' | head -3
    fail=1
  fi
}
case_capture 1 tests/fixtures/running-images-DRIFT-2026-08-06.json \
  "the live relay two states behind its manifest, captured before the roll-forward" sha-f7a9a0759a93
case_capture 0 tests/fixtures/running-images-AGREED-2026-08-06.json \
  "the same cluster after the roll-forward, so the check is not a refusal that never passes" sha-f7a9a0759a93
# Finished Jobs are history (2026-11-01): skipped. A Job still running is a workload: checked.
case_capture 0 tests/fixtures/running-images-FINISHED-JOB-2026-11-01.json \
  "Failed and Complete Jobs on an old tag after a correct re-pin, so history cannot hold INV19 red" sha-new000000000
case_capture 1 tests/fixtures/running-images-ACTIVE-JOB-2026-11-01.json \
  "an ACTIVE Job on an old tag, so the finished-Job skip cannot hide an in-flight workload" sha-new000000000

# THE HOLE, ASSERTED RATHER THAN ARGUED. INV15's logic is manifest-to-manifest and is GREEN over the
# drift capture, exactly as it was green through v8-versus-v10. This case fails if INV15 ever starts
# catching this state.
#
# HARDENING 5: WHAT TO DO WHEN THIS FIRES, because the natural response is the OPPOSITE of the
# intent. THIS CASE EXISTS TO FORCE THE REDUNDANCY QUESTION, NOT TO KEEP INV15 WEAK. If it goes red,
# somebody has taught INV15 to see the cluster, which is good. The correct response is then to ask
# whether INV19 still earns its place, and to DELETE ONE OF THEM ON PURPOSE. The wrong response, and
# the one a reader would reach for at 2am, is to weaken INV15 until the tripwire is green again.
# A tripwire whose firing is repaired by damaging the thing that improved is worse than no tripwire.
n=$((n+1))
# CAPTURED FIRST, THEN MATCHED. `verify_invariants.sh | grep -q` under `set -o pipefail` reports a
# FALSE NEGATIVE: grep -q exits on the first match, verify_invariants dies of SIGPIPE, and pipefail
# hands the pipeline that non-zero status. This case reported "INV15 is no longer green" on its
# first run while INV15 was green, which is the same false-negative shape as the missing --region
# earlier in this batch: a confident wrong answer produced by the instrument, not the subject.
inv15_snapshot=$(bash tests/verify_invariants.sh 2>&1)
if printf '%s' "$inv15_snapshot" | grep -qE '^OK +INV15'; then
  printf "OK   INV15  CONFIRMED BLIND to the manifest-versus-cluster case, which is why INV19 exists\n"
else
  printf "FAIL INV15  is no longer green here; INV19's justification needs re-reading, not deleting\n"
  fail=1
fi

# ---------------------------------------------------------------------------------------------
# MIRROR AND TRUE-INCIDENT FIXTURES, added 2026-08-04 by the fixture re-audit.
#
# WHY. Every fixture above injects ONE side of a two-file comparison, and the incident each guards
# has two directions. Proving one direction and calling the invariant proven is the same shortcut
# INV15 was caught taking: a fixture that encodes a PARAPHRASE of the incident passes the gate
# without guarding against the incident. Each of these was verified to CATCH before being added, so
# none is decorative, and together they make the gate's certification match its claim.

# INV1's fixture moves the CHART selector. Drift has two directions, and the other one is likelier:
# a Deployment relabelled during ordinary serving work while the chart sits still.
case_inv INV1 manifests/02-serving/vllm-interactive.yaml \
  's/app: vllm-llama3/app: vllm-DRIFTED/g' \
  "MIRROR: the DEPLOYMENT label drifts instead of the chart selector"

# INV2's fixture collapses both objectives onto interactive. The mirror collapses onto batch, which
# is the direction that silently sends interactive traffic to spot capacity.
case_inv INV2 manifests/03-routing/inferenceobjectives.yaml \
  's/name: vllm-llama3-interactive/name: vllm-llama3-batch/' \
  "MIRROR: both objectives collapse onto the BATCH pool instead"

# INV3's fixture edits the POOL manifest. The realistic incident is the opposite: a CHART UPGRADE
# renames the EPP release while the committed manifests sit still and nobody edits them.
case_inv INV3 infra/addons/install-gateway-api-inference.sh \
  's/vllm-llama3-\${tier}/vllm-renamed-\${tier}/' \
  "MIRROR: a chart upgrade renames the EPP release while the manifests sit still"

# INV10's fixture sets ONE pair equal. The R.5 incident had route, worker HTTP and claim lease ALL
# 600 simultaneously, which is the state actually observed rather than a two-value simplification.
case_inv INV10 async-worker/deployment.yaml \
  's/CLAIM_TTL_SECONDS, value: "750"/CLAIM_TTL_SECONDS, value: "600"/; s/HTTP_TIMEOUT_SECONDS, value: "660"/HTTP_TIMEOUT_SECONDS, value: "600"/' \
  "TRUE INCIDENT (R.5): route, worker HTTP and claim lease ALL 600 at once"

# INV16's fixture uses the GPU NodeClass. The teardown incident involved whichever NodeClass had
# been applied, so every one of them is exercised rather than the one that happened to be edited.
case_inv INV16 manifests/01-nodepools/ec2nodeclass-neuron.yaml \
  's/^  instanceProfile: .*$/  role: KarpenterNodeRole-inference-aware-eks/m' \
  "the NEURON EC2NodeClass uses spec.role, minting IAM outside the stack"

case_inv INV16 manifests/01-nodepools/ec2nodeclass-efa.yaml \
  's/^  instanceProfile: .*$/  role: KarpenterNodeRole-inference-aware-eks/m' \
  "the EFA EC2NodeClass uses spec.role, minting IAM outside the stack"

# ---------------------------------------------------------------------------------------------
# SELF-ENFORCEMENT. Everything above proves the invariants that HAVE a fixture. The property that
# decides whether this gate holds or decays is different: a NEWLY ADDED invariant carrying no
# fixture must make this gate RED, not be silently skipped.
#
# It used to skip. Verified by injecting a dummy "INV99" with no fixture: verify_invariants.sh
# reported 14 OK and this harness still exited 0, because it only ever iterated its own case list.
# A gate that only checks what it was told about cannot guard the gates it guards.
#
# Now every invariant verify_invariants.sh actually REPORTS must be accounted for: it either has a
# case above, or it is on the explicit allowlist below with a stated reason. Same shape as the
# dry-run skip bound and the INV12 PEND state: absence is not silence.
echo
echo "=== self-enforcement: every reported invariant must be accounted for ==="
# ELSEWHERE: injected in their OWN commits, each carrying the SHA of that proof, so the label is an
# AUDITABLE CLAIM rather than an assertion about history nothing can check. This is a MIGRATION
# BACKLOG trending to zero, not a permanent home: when an entry's fixture moves into this suite, its
# ELSEWHERE label moves with it.
ELSEWHERE=""
# NO_CHECK: no check exists to inject against. Legal ONLY for the registry statuses named in the
# cross-check below, each with its reason.
NO_CHECK="INV13"
COVERED="INV1 INV2 INV3 INV4 INV5 INV6 INV7 INV8 INV9 INV10 INV11 INV12 INV14 INV15 INV16 INV18 INV19 INV20"
ELSEWHERE_IDS=$(printf '%s\n' $ELSEWHERE | cut -d: -f1 | tr '\n' ' ')
REPORTED=$(bash tests/verify_invariants.sh 2>&1 | grep -oE '^(OK|FAIL|PEND) +INV[0-9]+' | grep -oE 'INV[0-9]+' | sort -u)
for inv in $REPORTED; do
  case " $COVERED $ELSEWHERE_IDS $NO_CHECK " in
    *" $inv "*) ;;
    *) echo "FAIL $inv is reported by verify_invariants.sh but has NO efficacy fixture and is on no allowlist. Add an injection case, or add it to NO_CHECK with a stated reason. An invariant nobody has tried to defeat is not evidence."
       fail=1 ;;
  esac
done
[ "$fail" -eq 0 ] && echo "OK   every reported invariant has a fixture or a stated exemption"

# ---------------------------------------------------------------------------------------------
# CROSS-CHECK: the allowlist and the registry POLICE EACH OTHER. Two independently editable records
# describing the same facts will drift, and the drift is silent, so each is made to constrain the
# other. Same construction as INV12's eight states: the mute-button path is closed by making the
# contradictory combination fail rather than by trusting nobody will write it.
#
# The mapping, defined once:
#   NO_CHECK    legal ONLY for registry status RESERVED, CLUSTER-GATED-AWAITING-CLUSTER,
#               or REGISTERED-NOT-IMPLEMENTED (relay-blocked). An ENFORCING entry here is a
#               contradiction: it HAS a check, so something must try to defeat it.
#   ELSEWHERE   legal ONLY for ENFORCING entries, and REQUIRES a commit SHA that exists.
echo
echo "=== cross-check: allowlist labels against the registry statuses ==="
# Registry statuses are carried here in one place, mirroring section 10.3 of the design. Keeping the
# mirror tiny and adjacent is deliberate: a second full copy would be another record to drift.
# DEBT-PASSING-PATH: entries whose OK branch has never executed against the real target. An
# invariant authored before its configuration exists has only ever been read against files
# describing something that was not running, so its passing state is unproven no matter how many
# injections its failing states survived. Cleared by execution, not by review.
debt_status(){ case "$1" in
  # INV13 CLEARED 2026-08-05: its passing path executed against a real generation on a g6e.xlarge
  # in the authorized GPU session. It found THREE defects in the probe on that first execution
  # (pod selector, request-body quoting, greedy model-id match), which is the marker paying for
  # itself: none of that code had ever run, because every green reading had been a SKIP.
  # INV18 is CLEAR BY CONSTRUCTION rather than by default, and the distinction is worth one line so
  # nobody has to infer it from the absence of an entry. It reads only COMMITTED FILES: the
  # registry, the serving manifests, and git ls-files. There is no cluster
  # half, so its passing path IS the green reading of `make verify-invariants`, which executed in
  # the same pass that added it. The marker exists for checks whose OK branch needs a resource
  # that may not exist (INV12's Service, INV13's serving replica); this one has none.
  *) echo CLEAR;; esac; }

reg_status(){ case "$1" in
  INV15) echo ENFORCING;;
  INV13) echo CLUSTER-GATED-AWAITING-CLUSTER;;
  *) echo ENFORCING;; esac; }
for inv in $NO_CHECK; do
  st=$(reg_status "$inv")
  case "$st" in
    RESERVED|CLUSTER-GATED-AWAITING-CLUSTER|REGISTERED-NOT-IMPLEMENTED)
      echo "OK   $inv in NO_CHECK, registry says $st" ;;
    *) echo "FAIL $inv is in NO_CHECK but the registry says $st. An ENFORCING invariant HAS a check, so something must try to defeat it. Either add a fixture or correct the registry."
       fail=1 ;;
  esac
done
for pair in $ELSEWHERE; do
  inv=${pair%%:*}; sha=${pair##*:}
  st=$(reg_status "$inv")
  if [ "$st" != "ENFORCING" ]; then
    echo "FAIL $inv is in ELSEWHERE but the registry says $st. ELSEWHERE means 'proven in another commit', which only makes sense for an entry that enforces."
    fail=1
  elif ! git cat-file -e "${sha}^{commit}" 2>/dev/null; then
    echo "FAIL $inv claims its injection proof is in commit ${sha}, which does not exist in this repository."
    fail=1
  else
    echo "OK   $inv in ELSEWHERE, registry ENFORCING, proof commit ${sha} exists"
  fi
done

echo
echo "=== DEBT-PASSING-PATH: invariants whose OK branch has never executed for real ==="
# NOT a failure. It is a LEDGER, printed every run so the debt cannot be forgotten between the
# batch that incurs it and the batch that clears it. A debt nobody prints is a debt nobody pays.
debt_n=0
for inv in $COVERED $ELSEWHERE_IDS $NO_CHECK; do
  inv=${inv%%:*}
  st=$(debt_status "$inv")
  if [ "$st" != "CLEAR" ]; then
    debt_n=$((debt_n+1))
    case "$inv" in
      INV13) echo "  $inv  $st  needs one serving replica to cancel against; awaits the GPU authorization." ;;
      *)     echo "  $inv  $st" ;;
    esac
  fi
done
[ "$debt_n" -eq 0 ] && echo "  none: every fixtured invariant has executed its passing path against the real target"
echo "  (INV12 taught this the expensive way; INV14 was cleared deliberately on 2026-08-04.)"

echo
echo "=== final state must be green again ==="
if bash tests/verify_invariants.sh >/dev/null 2>&1; then echo "OK   suite green after all injections reverted"
else echo "FAIL suite NOT green after revert; an injection leaked"; bash tests/verify_invariants.sh 2>&1 | grep -E '^FAIL'; fail=1; fi

echo
echo "NOT INJECTED HERE, and each for a stated reason rather than an omission:"
echo "  INV13  CLUSTER-GATED-AWAITING-CLUSTER. Needs a serving replica to cancel."
echo
if [ "$fail" -eq 0 ]; then echo "PASS: all $n injected invariants fail on the defect they exist to catch"
else echo "EFFICACY FAILURES ABOVE: an invariant does not catch its own target defect"; fi
exit $fail
