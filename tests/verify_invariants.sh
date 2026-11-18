#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# Verify the 10 cross-manifest invariants (INV1 through INV10) from DESIGN.md. Static checks on the
# manifests, with INV5 additionally needing a live cluster for the toleration-versus-taint match.
# Exit non-zero on any failure. Run after EVERY manifest edit.
set -uo pipefail
R=manifests
fail=0
ok(){ printf "OK   %s\n" "$1"; }
no(){ printf "FAIL %s\n" "$1"; fail=1; }

# TIERS ARE DERIVED, NOT HARDCODED (2026-08-03). Thirteen lines across seven invariants carried the
# literals "interactive" and "batch", so a third tier landed completely unguarded: the Neuron tier
# is deployed and NO tier-aware invariant covers it, which spec 001 called the most useful fact in
# the document. Deriving the list from the serving manifests means a new tier is checked the moment
# it exists, instead of the moment somebody remembers to widen a loop.
# Source of truth: the vLLM serving Deployments, one per tier, named vllm-<tier>.yaml.
TIERS=$(ls $R/02-serving/vllm-*.yaml 2>/dev/null | sed 's|.*/vllm-||; s|\.yaml$||' | sort)
[ -n "$TIERS" ] || { echo "FAIL cannot derive the tier list from $R/02-serving/vllm-*.yaml"; exit 1; }
# ROUTABLE tiers are those with an InferencePool. Neuron is deployed but not yet routable (design
# v6.2 section 4.2), so pool-shaped invariants check the routable subset while node-shaped ones
# (INV5 taints and tolerations) check EVERY tier. Conflating the two is what left Neuron unguarded.
ROUTABLE=""
for t in $TIERS; do grep -q "name: vllm-llama3-$t\b" "$R/03-routing/inferencepools.yaml" 2>/dev/null && ROUTABLE="$ROUTABLE $t"; done
ROUTABLE=$(echo $ROUTABLE)
echo "-- tiers discovered: $(echo $TIERS | tr '\n' ' ') | routable: $ROUTABLE"

P=$R/03-routing/inferencepools.yaml
O=$R/03-routing/inferenceobjectives.yaml
H=$R/03-routing/httproute.yaml
K=$R/04-autoscaling

# INV2: each objective's poolRef points at its own pool (names differ, correct pairing)
iref=$(awk '/name: interactive/{f=1} f&&/poolRef/{g=1} g&&/name: vllm-llama3-/{print $2; exit}' "$O")
bref=$(awk '/name: batch/{f=1} f&&/poolRef/{g=1} g&&/name: vllm-llama3-/{print $2; exit}' "$O")
if [ "$iref" = "vllm-llama3-interactive" ] && [ "$bref" = "vllm-llama3-batch" ]; then
  ok "INV2 objectives point at their own pools"; else no "INV2 objective poolRef wrong ($iref / $bref)"; fi

# INV3: each pool's endpointPickerRef.name must equal the EPP the chart deploys, which is
# <release>-epp with the release vllm-llama3-<tier> (install-gateway-api-inference.sh deploy_epp).
# Deriving the expected name from the chart script, not grepping the pool file for a literal it was
# written against, is what makes this a real guard: DESIGN.md warns that a check which greps the file
# it protects can never fail. This fails if the pool manifests and the chart release naming disagree.
GIE3=infra/addons/install-gateway-api-inference.sh
inv3_ok=1; got=""; want=""
grep -qF 'vllm-llama3-${tier}' "$GIE3" || inv3_ok=0   # chart must name the release vllm-llama3-<tier>
for t in $ROUTABLE; do
  want="vllm-llama3-${t}-epp"
  got=$(awk -v p="vllm-llama3-$t" '
    $0 ~ ("name: " p "$") {inpool=1; next}
    inpool && (/^kind:/ || /^---/) {inpool=0}
    inpool && /endpointPickerRef:/ {g=1; next}
    inpool && g && /name:/ {print $2; exit}
  ' "$P")
  [ "$got" = "$want" ] || { inv3_ok=0; break; }
done
if [ "$inv3_ok" -eq 1 ]; then
  ok "INV3 endpointPickerRef matches the chart EPP name vllm-llama3-<tier>-epp"
else
  no "INV3 endpointPickerRef mismatch vs chart EPP name (got=$got want=$want)"
fi

# INV4: no ScaledObject targets an InferencePool; each targets a Deployment
if grep -A3 'scaleTargetRef' "$K"/*.yaml | grep -v '^[[:space:]]*#' | grep -v '#' | grep -q 'InferencePool'; then
  no "INV4 a ScaledObject targets a pool"
elif grep -A3 'scaleTargetRef' "$K"/*.yaml | grep -q 'kind: Deployment'; then
  ok "INV4 KEDA targets Deployments"
else no "INV4 no Deployment target found"; fi

# INV6: failureMode asserts the DELIVERED behavior, not an aspiration. Envoy AI Gateway
# v1.0.0 does not honor FailOpen for InferencePool (LT-7 + config_dump evidence), so both
# tiers are effectively FailClose on this stack. The manifests must therefore NOT assert a
# FailOpen that the platform drops. This checks: interactive is FailClose, batch is
# FailClose (matching reality), and no pool asserts an undelivered FailOpen. Design intent
# for batch remains FailOpen; restore it (and this check) when the stack maps failureMode.
ic=$(awk '/name: vllm-llama3-interactive/{f=1} f&&/failureMode:/{print $2; exit}' "$P")
bc=$(awk '/name: vllm-llama3-batch/{f=1} f&&/failureMode:/{print $2; exit}' "$P")
if [ "$ic" = "FailClose" ] && [ "$bc" = "FailClose" ] && ! grep -qE 'failureMode:[[:space:]]*FailOpen' "$P"; then
  ok "INV6 failureMode matches delivered behavior (both FailClose on this stack)"
else
  no "INV6 failureMode asserts undelivered behavior (interactive=$ic batch=$bc; FailOpen not honored by AI Gateway v1.0.0)"
fi

# INV7: the batch rule must match BOTH the /v1 PathPrefix AND the batch header.
# Gateway API precedence is longest-matching-path first, then header-match count,
# NOT list order. A header-only batch rule (default path /) therefore loses to the
# interactive /v1 rule and batch traffic silently serves on interactive. So parse the
# batch rule BLOCK (from its `- matches:` through its InferencePool backendRef) and
# assert that same block carries a PathPrefix /v1 and x-gateway-inference-objective:
# batch. A pool-order-only or header-presence-only check would pass that regression;
# this one does not.
batch_rule=$(awk '
  /^[[:space:]]*-[[:space:]]*matches:/ { blk=""; cap=1 }
  cap { blk = blk $0 "\n" }
  /name:[[:space:]]*vllm-llama3-batch[[:space:]]*$/ && cap { printf "%s", blk; exit }
' "$H")
has_path=$(printf '%s' "$batch_rule" | grep -cE 'value:[[:space:]]*/v1')
has_hdr=$(printf '%s'  "$batch_rule" | grep -c  'x-gateway-inference-objective')
has_val=$(printf '%s'  "$batch_rule" | grep -cE 'value:[[:space:]]*batch[[:space:]]*$')
if [ -n "$batch_rule" ] && [ "$has_path" -ge 1 ] && [ "$has_hdr" -ge 1 ] && [ "$has_val" -ge 1 ]; then
  ok "INV7 batch rule matches both /v1 path and batch header"
else
  no "INV7 batch rule missing /v1 path or batch header (path=$has_path hdr=$has_hdr val=$has_val)"
fi
# INV7 second half, REDESIGNED 2026-08-03. It used to compare `head -1` LINE NUMBERS of two pool
# names and call the smaller one "first". That is a proxy for a precedence the file's own comment
# says is NOT list order, so it asserted something Gateway API does not promise and passed for a
# reason unrelated to the property. It now asserts real specificity: longest path, then HEADER
# COUNT, then query params, per the HTTPRouteRule spec. This is a hard prerequisite of the
# two-header design, whose determinism rests on header count breaking the tie after path length.
PYB="./.venv/bin/python"; [ -x "$PYB" ] || PYB=python3
if "$PYB" tests/check_httproute_precedence.py "$H" >/tmp/inv7.out 2>&1; then
  ok "INV7 rule precedence by Gateway API specificity (not list order): $(tail -1 /tmp/inv7.out | sed 's/^OK *//')"
else
  no "INV7 rule precedence: $(grep -v '^FAIL' /tmp/inv7.out | head -2 | tr '\n' ' ')"
fi

# INV8: every workload namespace: line is inference. `monitoring` is an allowed
# system namespace (kube-prometheus-stack + the Prometheus service alias live there
# by design, like Karpenter/KEDA/gateway charts do).
stray=$(grep -rh 'namespace:' $R async-worker/*.yaml 2>/dev/null | grep -v '#' | awk '{print $2}' | grep -vE '^(inference|monitoring)$' | wc -l | tr -d ' ')
if [ "$stray" -eq 0 ]; then ok "INV8 namespace inference everywhere (monitoring allowed)"; else no "INV8 stray namespace ($stray)"; fi

# INV1: the InferencePool selector must equal each vLLM Deployment's pod-template labels. The
# selector is chart-owned: install-gateway-api-inference.sh pins modelServers.matchLabels.app=<app>
# and .tier=<tier>. This cross-checks that same app against the interactive AND batch Deployment
# pod labels (not just a presence grep): change the chart's app without changing the manifests, or
# vice versa, and this fails. The tier arm is templated (${tier}), so it is asserted per tier.
GIE=infra/addons/install-gateway-api-inference.sh
sel_app=$(grep -oE 'modelServers\.matchLabels\.app=[A-Za-z0-9._-]+' "$GIE" | head -1 | cut -d= -f2)
sel_tier_tmpl=$(grep -c 'modelServers\.matchLabels\.tier=${tier}' "$GIE")
inv1_ok=1
[ -n "$sel_app" ] || inv1_ok=0
[ "$sel_tier_tmpl" -ge 1 ] || inv1_ok=0
for t in $ROUTABLE; do
  m="$R/02-serving/vllm-$t.yaml"
  grep -qE "labels:[[:space:]]*[{][^}]*app:[[:space:]]*${sel_app}[^}]*tier:[[:space:]]*$t" "$m" || inv1_ok=0
done
if [ "$inv1_ok" -eq 1 ]; then
  ok "INV1 pool selector (app=$sel_app, tier=<tier>) matches every routable tier's Deployment labels ($ROUTABLE)"
else
  no "INV1 pool selector does not match Deployment pod labels (chart app=$sel_app, tier templated=$sel_tier_tmpl)"
fi
# INV5, BROADENED 2026-08-03 from interactive-only to EVERY tier. This was the priority named in
# spec 001 section 9.1a, because the toleration-to-taint match IS the tier isolation property that
# LT-4 verified: a tier whose toleration is missing schedules somewhere it should not, and the
# isolation result quietly stops being true. Checking only the interactive manifest meant Neuron and
# batch could both regress silently. Static half here; the live taint comparison still needs a
# cluster.
inv5_ok=1; inv5_detail=""
for t in $TIERS; do
  m="$R/02-serving/vllm-$t.yaml"; [ -e "$m" ] || continue
  if ! grep -qE 'tolerations:|key: (nvidia\.com/gpu|aws\.amazon\.com/neuron)' "$m"; then
    inv5_ok=0; inv5_detail="$inv5_detail $t"
  fi
done
if [ "$inv5_ok" -eq 1 ]; then
  ok "INV5 every tier ($(echo $TIERS | tr '\n' ' ')) declares an accelerator toleration (verify vs NodePool taint on cluster)"
else
  no "INV5 tier(s) with no accelerator toleration:$inv5_detail. That match IS the LT-4 tier isolation property."
fi

# INV9: the async worker routes through the inference-aware GATEWAY, not a vLLM Service. Routing the
# worker at a Service round-robins across pods and bypasses the Endpoint Picker (the exact defect the
# async-through-gateway change fixed). The Envoy Service name is cluster-specific, so the committed
# manifest sources GATEWAY_ENDPOINT from the gateway-endpoint ConfigMap that setup-async-infra.sh
# creates by discovery. Guard: GATEWAY_ENDPOINT comes from that ConfigMap (or, if hardcoded as a
# value, is a gateway URL and not a vllm-llama3-*:8000 Service), AND INFERENCE_OBJECTIVE is set.
W=async-worker/deployment.yaml
obj=$(grep 'INFERENCE_OBJECTIVE' "$W" | grep -oE 'value: "[^"]*"' | sed 's/value: "//;s/"$//')
cm=$(awk '/name: GATEWAY_ENDPOINT/{f=1} f&&/configMapKeyRef/{print; exit}' "$W" | grep -oE 'name: [A-Za-z0-9-]+' | awk '{print $2}')
gwval=$(awk '/name: GATEWAY_ENDPOINT/{f=1} f&&/value:/{print; exit}' "$W" | grep -oE 'value: "[^"]*"' | sed 's/value: "//;s/"$//')
inv9_ok=0; src="unset"
if [ "$cm" = "gateway-endpoint" ]; then
  inv9_ok=1; src="configMap:gateway-endpoint"
elif [ -n "$gwval" ] && echo "$gwval" | grep -q 'envoy' \
     && ! echo "$gwval" | grep -qE 'vllm-llama3-(interactive|batch):8000' \
     && ! echo "$gwval" | grep -qE 'ENVOY_GATEWAY_SERVICE|FILL|<[^>]+>|[A-Z][A-Z0-9_]{5,}'; then
  # a hardcoded gateway URL is acceptable only if it is a real Service name: reject the
  # ENVOY_GATEWAY_SERVICE placeholder and any unresolved placeholder (a real Envoy Service name is
  # lowercase, so an uppercase run signals a placeholder). The old check matched 'envoy' inside
  # 'envoy-gateway-system' and so passed on the placeholder.
  inv9_ok=1; src="value:$gwval"
else
  src="${cm:+configMap:$cm}${gwval:+value:$gwval}"
fi
# INV9 objective check, TIGHTENED in v6.1 per 0b A13 part 2(a). It previously asserted only that
# INFERENCE_OBJECTIVE was non-empty, which passes on EVERY non-empty value including
# `interactive`. That was the internal bypass: A9 rules the relay a complete vantage point for
# interactive-pool load partly because the async worker sends objective batch and therefore never
# reaches that pool, but the worker calls the gateway at its in-cluster ClusterIP
# (ARCHITECTURE-REVIEW.md R.4) and so structurally bypasses the relay. With a non-empty test, one
# edit to that value would silently route async traffic at the interactive pool, past the relay,
# and the reservation guard would under-count with nothing failing. Equality with the literal
# `batch` is what turns that documented property into an enforced one.
if [ "$inv9_ok" -eq 1 ] && [ "$obj" = "batch" ]; then
  ok "INV9 worker routes through the gateway ($src, objective=$obj), not a vLLM Service"
elif [ "$inv9_ok" -eq 1 ] && [ -z "$obj" ]; then
  no "INV9 INFERENCE_OBJECTIVE is absent; it must be set to the literal 'batch' (0b A13 part 2a)"
elif [ "$inv9_ok" -eq 1 ]; then
  no "INV9 INFERENCE_OBJECTIVE is '$obj', must be the literal 'batch': any other value routes async traffic past the relay and breaks the A9 vantage point (0b A13 part 2a)"
else
  no "INV9 worker gateway target invalid ($src objective=$obj)"
fi

# INV10: the timeout chain is strictly ordered so recovery is orderly, not racing
# (ARCHITECTURE-REVIEW.md R.5): route timeout < worker HTTP client timeout < claim lease < SQS
# visibility. Values from the manifests + the SQS setup script.
rt=$(grep -oE 'request: "[0-9]+s"' "$H" | head -1 | grep -oE '[0-9]+')
ht=$(grep 'HTTP_TIMEOUT_SECONDS' "$W" | grep -oE '"[0-9]+"' | tr -d '"')
lt=$(grep 'CLAIM_TTL_SECONDS' "$W" | grep -oE '"[0-9]+"' | tr -d '"')
vt=$(grep -oE 'VIS_TIMEOUT=[0-9]+' infra/addons/setup-async-infra.sh | head -1 | grep -oE '[0-9]+')
if [ -n "$rt" ] && [ -n "$ht" ] && [ -n "$lt" ] && [ -n "$vt" ] && \
   [ "$rt" -lt "$ht" ] && [ "$ht" -lt "$lt" ] && [ "$lt" -lt "$vt" ]; then
  ok "INV10 timeout chain ordered (route $rt < http $ht < lease $lt < visibility $vt)"
else
  no "INV10 timeout chain not ordered (route=$rt http=$ht lease=$lt visibility=$vt; need route<http<lease<visibility)"
fi

# INV12 (NEW in v6.1, per 0b A13 part 2b). Ingress exclusivity: the relay must be the SOLE
# externally reachable interactive entry, and the gateway data plane must be cluster-internal.
# Without this, a client can address the gateway directly, arrive unclassified at the interactive
# default rule, and evade the reservation guard, the upgrade-rate cap and classification, with no
# failure and no alert. That is the external bypass A13 records.
#
# INV12 is NOT INV11: spec 001 section 3.1 already reserves INV11 for the promotion budget
# assertion P < 600, so taking that number here would collide with one already spent.
#
# SEQUENCING, and why this check has three outcomes rather than two. A13 part 1 states plainly
# that this asserts a topology that does not exist YET: today the gateway data plane IS externally
# exposed, because manifests/03-routing/envoyproxy-accesslog.yaml sets an AWS load balancer
# annotation on envoyService. The cutover lands with the Phase 2 relay. So a plain pass/fail check
# would go red on a correct repository for the whole of Phase 1.
#
# The failure mode to avoid is the one DESIGN.md warns about: a not-yet-true invariant parked as a
# skipped test that nobody revisits. So this does not skip. It recognises exactly two legitimate
# topologies and fails on anything else:
#   PRE-RELAY  the annotation is present AND no relay manifest exists  -> PENDING, reported loudly
#   POST-RELAY no external exposure on the gateway AND a relay exists  -> PASS
#   anything else (both, or neither, or a stray LoadBalancer/NodePort) -> FAIL
# A half-finished cutover therefore fails, which is the case worth catching.
EP=$R/03-routing/envoyproxy-accesslog.yaml
# NOTE on the idiom: `grep -c` PRINTS 0 and also EXITS 1 when there are no matches, so the
# tempting `$(grep -c ... || echo 0)` yields the two-line string "0\n0" and every later numeric
# test on it breaks. That is the same shape as this project's empty-vector trap.
gw_annot=$(grep -c 'aws-load-balancer' "$EP" 2>/dev/null | head -1)
[ -n "$gw_annot" ] || gw_annot=0
# THE POSITIVE ASSERTION, added 2026-08-04 after the annotation-absence proxy was caught being
# comfortable. Envoy Gateway DEFAULTS envoyService to LoadBalancer, so an absent annotation leaves
# the data plane exactly as reachable, just with the AWS default idle timeout. Confirmed in the live
# cluster: the gateway Service was type LoadBalancer with an active external hostname while this
# file carried no annotation at all. So POST-RELAY additionally requires the type to be pinned to
# ClusterIP in writing. Absence of an exposure marker is not evidence of no exposure.
gw_clusterip=$(grep -A2 'envoyService:' "$EP" 2>/dev/null | grep -c 'type:[[:space:]]*ClusterIP' | head -1)
[ -n "$gw_clusterip" ] || gw_clusterip=0
relay_files=$(ls $R/08-relay/*.yaml 2>/dev/null | wc -l | tr -d ' ')

# CORRECTED 2026-08-04, and the correction is the substance of this block rather than a tidy-up.
# The POST-RELAY branch required ZERO externally exposed Services. The specification it implements
# (GATE-implementation-readiness.md, relay-coverage row) says the relay is THE SOLE EXTERNALLY
# EXPOSED interactive entry. Sole means ONE, not none. As written, the post-cutover state was
# UNREACHABLE: committing a correct cutover (marker deleted, relay present, gateway annotation
# removed) still failed, because the relay's own LoadBalancer counted against it. An invariant whose
# passing state cannot be reached does not protect a property, it blocks one.
#
# So the count is no longer "how many external Services exist" but "how many external Services are
# NOT the relay's". Exactly one exposure is permitted and it must be the relay's, identified by the
# label `inference-aware-eks/ingress-role: relay` rather than by matching a name as a string, so
# renaming the Service cannot silently create a second permitted door.
#
# Two legitimate topologies, everything else fails, unchanged in shape:
#   PRE-RELAY  marker present, no relay, gateway annotation present, no external Service  -> PEND
#   POST-RELAY marker gone, relay present, gateway annotation gone, exactly one relay-labelled
#              external Service and no other                                              -> PASS
#   anything else, including a half-finished cutover                                      -> FAIL
#
# The scan is per-file so a Service can be attributed. A repository-wide `grep | wc -l` cannot tell
# which file the exposure came from, and attribution is the whole of what changed here.
#
# THE LOAD-TEST MANIFESTS ARE IN SCOPE TOO (added 2026-11-02). They are applied to the same cluster,
# temporarily, and a temporary door is still a door: loadtest/lt2-rr-service.yaml was an
# internet-facing LoadBalancer over unauthenticated vLLM pods, outside every invariant, until a
# content security scan found it.
ext_total=0; ext_relay=0; ext_stray=""
for f in $(find $R -name '*.yaml' 2>/dev/null) async-worker/*.yaml loadtest/*.yaml; do
  [ -e "$f" ] || continue
  n=$(grep -cE '^[[:space:]]*type:[[:space:]]*(LoadBalancer|NodePort)[[:space:]]*$' "$f" 2>/dev/null | head -1)
  [ -n "$n" ] || n=0
  [ "$n" -eq 0 ] && continue
  ext_total=$((ext_total + n))
  if grep -q 'inference-aware-eks/ingress-role:[[:space:]]*relay' "$f" 2>/dev/null; then
    ext_relay=$((ext_relay + n))
  else
    ext_stray="$ext_stray $f"
  fi
done
ext_other=$((ext_total - ext_relay))

# THE POSITIVE MARKER. PEND is a state someone ASSERTED, not one inferred from absence.
# Without this, deleting the relay manifests and restoring the annotation (a full rollback)
# silently converted a FAIL back into a PEND: absence of everything looked identical to
# not-started-yet. The marker makes the two distinguishable, so the only way to reach PEND is to
# have committed a file that says so.
marker=$R/PHASE-PRE-RELAY.marker
has_marker=0; [ -f "$marker" ] && has_marker=1
if [ "$has_marker" -eq 1 ] && [ "$relay_files" -eq 0 ] && [ "$gw_annot" -ge 1 ] && [ "$ext_total" -eq 0 ]; then
  printf "PEND INV12 ingress exclusivity: PRE-RELAY, asserted by %s (gateway data plane externally exposed, no relay yet). Enforces at the Phase 2 cutover; delete the marker in the SAME change set that adds the relay, moves the annotation to the relay exposure at the Chain B value, and leaves the relay as the only external Service.\n" "$marker"
elif [ "$has_marker" -eq 0 ] && [ "$relay_files" -ge 1 ] && [ "$gw_annot" -eq 0 ] \
     && [ "$gw_clusterip" -ge 1 ] && [ "$ext_relay" -eq 1 ] && [ "$ext_other" -eq 0 ]; then
  ok "INV12 ingress exclusivity: POST-RELAY, marker retired, relay present, gateway envoyService PINNED to ClusterIP (not merely un-annotated), and the relay is the SOLE external exposure (1 relay, 0 other)"
else
  no "INV12 ingress exclusivity in an unrecognised state (marker=$has_marker relay_manifests=$relay_files gateway_lb_annotation=$gw_annot gateway_clusterip=$gw_clusterip external_relay=$ext_relay external_other=$ext_other${ext_stray:+ stray:$ext_stray}). Only two states are legitimate: PRE-RELAY (marker=1 relay=0 annotation>=1 external=0) or POST-RELAY (marker=0 relay>=1 annotation=0 envoyService-type-ClusterIP exactly-one-relay-exposure and no other). Absence of everything is a FAILURE, not a pending state."
fi

# INV16 (NEW 2026-08-03). Every EC2NodeClass uses spec.instanceProfile and NEVER spec.role.
# WHY THIS IS AN INVARIANT AND NOT A NOTE. With `role`, Karpenter MINTS an instance profile per
# EC2NodeClass, outside the CloudFormation stack. Those unowned IAM objects are what made the
# 2026-08-03 teardown need four attempts: the stack could not delete a node role held by profiles
# it did not know about. The overlay creates ONE stack-owned profile and every NodeClass references
# it, so Karpenter creates no IAM at all. Without this check, the next EC2NodeClass added the old
# way silently restores the defect, and it would not surface until a teardown months later.
# The two fields are mutually exclusive under a CEL rule at the pinned v1.14.0, so this asserts the
# positive (instanceProfile present) AND the negative (role absent) rather than trusting exclusivity.
inv16_ok=1; inv16_detail=""
for f in $R/01-nodepools/ec2nodeclass-*.yaml; do
  [ -e "$f" ] || continue
  has_ip=$(grep -cE '^[[:space:]]*instanceProfile:[[:space:]]*[A-Za-z]' "$f")
  has_role=$(grep -cE '^[[:space:]]*role:[[:space:]]*[A-Za-z]' "$f")
  if [ "$has_ip" -lt 1 ] || [ "$has_role" -ge 1 ]; then
    inv16_ok=0; inv16_detail="$inv16_detail $(basename $f)(instanceProfile=$has_ip role=$has_role)"
  fi
done
if [ "$inv16_ok" -eq 1 ]; then
  ok "INV16 every EC2NodeClass uses spec.instanceProfile, never spec.role (Karpenter mints no unowned IAM)"
else
  no "INV16 an EC2NodeClass uses spec.role or lacks spec.instanceProfile:$inv16_detail. With spec.role Karpenter mints an instance profile per NodeClass OUTSIDE the CloudFormation stack, which is what made the 2026-08-03 teardown need four attempts."
fi

# INV15 (implemented 2026-08-03, reserved earlier in the registry). All image pins for this
# project's own image AGREE across every manifest, and none is a counter-style tag.
#
# WHY. The committed manifests pinned :v8 in three places while the cluster actually ran :v10, so
# the manifests described an image that was not serving. Reconstructing what HAD served needed
# blob-hash archaeology a week later (BUILD-NOTES 2026-08-02). Two separate properties failed
# there, so this asserts both: the pins must AGREE with each other, and the tag must be
# commit-traceable rather than a hand-incremented counter that carries no provenance.
# The build stamps org.opencontainers.image.revision, so a sha- tag is checkable at the registry.
IMG_RE='[0-9]{12}\.dkr\.ecr\.[a-z0-9-]+\.amazonaws\.com/inference-async-worker:[A-Za-z0-9._-]+'
pins=$(grep -rhoE "$IMG_RE" $R async-worker/*.yaml 2>/dev/null | sed 's/.*://' | sort -u)
npins=$(printf '%s\n' "$pins" | grep -c . || true)
counters=$(printf '%s\n' "$pins" | grep -cE '^v[0-9]+$' || true)
if [ "$npins" -eq 0 ]; then
  no "INV15 no image pin found at all; the manifests should pin this project's image"
elif [ "$npins" -gt 1 ]; then
  no "INV15 image pins DISAGREE across manifests ($(printf '%s ' $pins)). That is the v8-versus-v10 defect: the manifests would describe an image that is not the one running."
elif [ "$counters" -ge 1 ]; then
  no "INV15 image pin '$pins' is a counter-style tag. Counters carry no provenance; use the sha-<commit> tag the in-cluster build produces, which is checkable via org.opencontainers.image.revision."
else
  ok "INV15 image pins agree across manifests and are commit-traceable ($pins)"
fi

# INV11 (IMPLEMENTED 2026-08-04, with the promotion actor that made it checkable). The promotion
# deadline P is STRICTLY INSIDE Chain B, asserted from the committed configuration.
#
# WHY P IS THE ONE THAT MUST BE SHORTEST. P is a PROMISE MADE TO THE CALLER: "if this has not
# answered in P seconds, it becomes an asynchronous job and you get an id". A promise that outlives
# the machinery enforcing it is not enforceable. If P were at or above the route timeout, the
# gateway would give up before the deadline arrived, the caller would receive a 504 rather than a
# job id, and the promotion path would never execute at all while appearing to be configured.
#
# BORN WITHOUT THE DEBT, deliberately. INV12 was authored before its topology existed and its OK
# branch failed the first time it met reality; INV14 sat in the same position and was cleared by
# execution. This one lands WITH the configuration it asserts, and its passing path is executed
# against the live relay in the same batch that writes it. That is the cheap version of the lesson.
CBT11=$R/03-routing/chain-b-timeouts.yaml
p_val=$(grep -E '^\s+PROMOTION_DEADLINE_SECONDS:' "$CBT11" 2>/dev/null | grep -oE '"[0-9]*"' | tr -d '"')
p_route=$(grep -E '^\s+ROUTE_TIMEOUT_SECONDS:' "$CBT11" 2>/dev/null | grep -oE '[0-9]+' | head -1)
if [ -z "$p_val" ]; then
  printf "PEND INV11 promotion budget: P is unset in %s. Unset is a legitimate state only while no promotion actor exists; once the actor ships, P must be set and this enforces.\n" "$CBT11"
elif [ -z "$p_route" ]; then
  no "INV11 promotion budget: the route timeout is missing from $CBT11, so P has nothing to be inside of"
elif [ "$p_val" -ge "$p_route" ]; then
  no "INV11 promotion budget: P ($p_val) is not strictly less than the route timeout ($p_route). The gateway would give up before the deadline arrived, so the caller gets a 504 instead of a job id and the promotion path never executes while appearing configured."
elif [ "$p_val" -le 0 ]; then
  no "INV11 promotion budget: P ($p_val) is not positive, so every request would promote on arrival"
else
  # The relay must also READ it, for the same reason INV14 checks R's source: a value nothing
  # consumes is decorative, and the ordering would be true on paper and absent in the data plane.
  # STRUCTURAL, not a grep. The first version of this check looked for the STRING
  # PROMOTION_DEADLINE_SECONDS anywhere in the relay manifest, and the env var's own `name:` line
  # kept it satisfied after the `key:` was pointed elsewhere. Its own fixture caught that, which is
  # the same lesson INV14 learned: a check that infers structure from text adjacency measures the
  # wrong thing.
  if ! ${PYBIN:-./.venv/bin/python} tests/check_relay_chain_source.py PROMOTION_DEADLINE_SECONDS >/dev/null 2>&1; then
    no "INV11 promotion budget: P is set and strictly ordered, but the relay does not read PROMOTION_DEADLINE_SECONDS, so the deadline the actor enforces is not the committed one"
  else
    ok "INV11 promotion budget: P=$p_val strictly inside Chain B (P < route $p_route), and the relay reads it from the ConfigMap"
  fi
fi

# INV14 (IMPLEMENTED 2026-08-04, at the Phase 2 cutover that made it checkable). Chain B is
# strictly ordered, asserted STATICALLY from the committed files with no cluster and no running
# relay, exactly as INV10 does for Chain A.
#
#     P  <  600 (route)  <  R (relay upstream)  <  L (external exposure idle)
#
# WHY IT COULD NOT EXIST BEFORE. Two of the three values had no committed home: R was a number in a
# comment, and L sat on the gateway's envoyService rather than on a relay exposure. Both now exist
# as configuration, so the ordering became assertable at the same change set that created them.
#
# NO CROSS-CHAIN INEQUALITY. Chain A (INV10) and Chain B govern different request paths, and
# relating them would constrain both for no reason. INV14 does not read Chain A's values at all.
#
# THE THIRD ASSERTION IS THE ONE THAT MATTERS MOST. It is not enough that the ConfigMap holds a
# well-ordered R: the relay must actually READ it. A Deployment that hardcodes its own timeout
# leaves the ConfigMap decorative, the ordering true on paper and absent in the data plane, and
# nothing else in this repository would notice.
RELAY_DIR=$R/08-relay
CBT=$R/03-routing/chain-b-timeouts.yaml
if [ ! -d "$RELAY_DIR" ] || [ -z "$(ls $RELAY_DIR/*.yaml 2>/dev/null)" ]; then
  printf "PEND INV14 Chain B ordering: no relay manifest yet, so R and L have no committed home. Enforces at the Phase 2 cutover.\n"
else
  inv14_ok=1; inv14_why=""
  cb_route=$(grep -E '^\s+ROUTE_TIMEOUT_SECONDS:' "$CBT" 2>/dev/null | grep -oE '[0-9]+' | head -1)
  cb_relay=$(grep -E '^\s+RELAY_UPSTREAM_TIMEOUT_SECONDS:' "$CBT" 2>/dev/null | grep -oE '[0-9]+' | head -1)
  # L is read from the RELAY's annotation, which is the value that will actually be applied, rather
  # than from the ConfigMap's restatement of it. Reading the restatement would make this invariant
  # agree with itself while the data plane did something else.
  # L IS READ FROM WHERE THE PLATFORM WILL ACTUALLY APPLY IT, and that location moved on 2026-08-07
  # when the relay migrated from a Classic Load Balancer to a Network Load Balancer provisioned by
  # the AWS Load Balancer Controller.
  #
  #   Classic : service.beta.kubernetes.io/aws-load-balancer-connection-idle-timeout: "690"
  #   NLB     : ...aws-load-balancer-listener-attributes.TCP-80: tcp.idle_timeout.seconds=690
  #
  # THIS INVARIANT CAUGHT THE MIGRATION, which is the whole reason it exists: the moment the Classic
  # annotation was replaced, INV14 went red with exposure='' rather than quietly reading a stale
  # value or defaulting. A chain check that survives a platform change without noticing is a chain
  # check that has stopped measuring the chain.
  cb_expo=$(grep -hoE 'tcp\.idle_timeout\.seconds=[0-9]+' $RELAY_DIR/*.yaml 2>/dev/null | grep -oE '[0-9]+' | head -1)
  # The Classic form is accepted as a FALLBACK so this check keeps working against any manifest that
  # has not migrated, and its presence ALONGSIDE the NLB form is a defect: on an NLB the Classic
  # annotation is inert, and an inert value that reads correctly is the reassuring-proxy failure.
  if [ -z "$cb_expo" ]; then
    cb_expo=$(grep -hE 'aws-load-balancer-connection-idle-timeout:' $RELAY_DIR/*.yaml 2>/dev/null | grep -oE '[0-9]+' | head -1)
  elif grep -qE 'aws-load-balancer-connection-idle-timeout:' $RELAY_DIR/*.yaml 2>/dev/null; then
    inv14_ok=0
    inv14_why="the relay carries BOTH the Classic connection-idle-timeout annotation and the NLB listener attribute. On an NLB the Classic one is inert, so two values would disagree with only one of them reaching the data plane."
  fi
  for v in "$cb_route" "$cb_relay" "$cb_expo"; do
    [ -n "$v" ] || { inv14_ok=0; inv14_why="a Chain B value is missing (route='$cb_route' relay='$cb_relay' exposure='$cb_expo')"; }
  done
  if [ "$inv14_ok" -eq 1 ]; then
    if [ "$cb_route" -ge "$cb_relay" ]; then
      inv14_ok=0; inv14_why="route ($cb_route) is not strictly less than relay upstream ($cb_relay); equal values race, which is the R.5 defect"
    elif [ "$cb_relay" -ge "$cb_expo" ]; then
      inv14_ok=0; inv14_why="relay upstream ($cb_relay) is not strictly less than the exposure idle timeout ($cb_expo); the load balancer would give up before the relay does"
    fi
  fi
  # THE CONSUMPTION CHECK, done STRUCTURALLY. It was originally two independent greps ("contains
  # configMapKeyRef" and "contains RELAY_UPSTREAM_TIMEOUT_SECONDS"), which can BOTH be true while
  # the two have nothing to do with each other. That is not hypothetical: the relay gained a second
  # ConfigMap reference for the discovered gateway address, and from that moment the injected defect
  # (hardcoding the timeout as a literal `value:`) satisfied both greps and the fixture stopped
  # failing. The efficacy harness caught it. The check now parses the pod spec and asserts the
  # variable's actual source. Same lesson as INV7's line-number proxy: a check that infers structure
  # from text adjacency is measuring the wrong thing.
  if [ "$inv14_ok" -eq 1 ]; then
    if ! ${PYBIN:-./.venv/bin/python} tests/check_relay_chain_source.py >/dev/null 2>&1; then
      inv14_ok=0
      inv14_why="$(${PYBIN:-./.venv/bin/python} tests/check_relay_chain_source.py 2>&1 | grep '^FAIL' | head -1 | sed 's/^FAIL //')"
      [ -n "$inv14_why" ] || inv14_why="the relay does not source RELAY_UPSTREAM_TIMEOUT_SECONDS from the chain-b-timeouts ConfigMap, so the committed ordering would not be the one the relay applies"
    fi
  fi
  if [ "$inv14_ok" -eq 1 ]; then
    ok "INV14 Chain B strictly ordered from committed files: route $cb_route < relay upstream $cb_relay < exposure idle $cb_expo, and the relay sources R from the ConfigMap"
  else
    no "INV14 Chain B ordering: $inv14_why"
  fi
fi

# INV18 (NEW 2026-08-06, ruling 4 item 4). THE MODEL-FACTS REGISTRY IS COMPLETE AND NOT BYPASSED.
#
# WHY THIS INVARIANT AND NOT ANOTHER AUDIT. docs/specs/AUDIT-model-call-sites.md tabulates 24 call
# sites and finds exactly one defective, which is reassuring and which is a PHOTOGRAPH. It says
# nothing about the 25th site. INV18 governs the models themselves, so that "how may this model be
# addressed" is written once and checked.
#
# TWO HALVES, because each is defeatable alone:
#   1. COMPLETENESS. Every model in a serving manifest has a registry entry. A model nobody wrote
#      facts for is a model nobody decided how to address.
#   2. NO INVENTED PATHS. Every OpenAI-style endpoint literal in the tree is one the registry knows.
#      This is the half that catches a NEW call site rather than an existing one.
REG=docs/specs/model-facts.yaml
if [ ! -f "$REG" ]; then
  printf "PEND INV18 model-facts registry: %s absent. Enforces once the registry exists.\n" "$REG"
else
  inv18_why=$(${PYBIN:-./.venv/bin/python} tests/check_model_facts.py 2>&1 | grep '^FAIL' | head -1 | sed 's/^FAIL //')
  if [ -z "$inv18_why" ]; then
    ok "INV18 model-facts registry complete, and no call site invents an endpoint"
  else
    no "INV18 model-facts registry: $inv18_why"
  fi
fi

# INV20 (NEW 2026-09-22, the maintainer's ruling on the thinking control). ONE NON-THINKING TEMPLATE, MOUNTED
# ON EVERY QWEN3 TIER, AGREEING EVERYWHERE IT IS NAMED: the committed file never consults
# enable_thinking, its ConfigMap carries the same bytes, every Qwen3 serving manifest mounts it and
# passes --chat-template (either argument syntax).
# WHY IT EXISTS: the first live premium generation leaked an unclosed <think> block while no
# committed file or manifest flag forced thinking off.
# JUDGED ON THE EXIT CODE, not on the presence of a FAIL line: a checker that crashes prints no FAIL
# line, and reading "no FAIL line" as a pass would turn a traceback into green.
inv20_out=$(${PYBIN:-./.venv/bin/python} tests/check_chat_template.py 2>&1); inv20_rc=$?
if [ "$inv20_rc" -eq 0 ]; then
  ok "INV20 $(printf '%s' "$inv20_out" | sed -n 's/^OK //p' | head -1)"
else
  inv20_why=$(printf '%s' "$inv20_out" | sed -n 's/^FAIL //p' | head -1)
  no "INV20 chat template: ${inv20_why:-checker exited $inv20_rc without a FAIL line: $(printf '%s' "$inv20_out" | tail -1)}"
fi

# INV19 (NEW 2026-08-06). EVERY RUNNING WORKLOAD USING THIS PROJECT'S IMAGE RUNS THE COMMITTED PIN.
#
# THE FINDING UNDER THE FINDING, and it is worth stating before the check. INV15 was written for the
# v8-versus-v10 incident and asserts that the pins AGREE WITH EACH OTHER. In that incident THE PINS
# AGREED: all three said v8 while the cluster ran v10. So INV15's first assertion was green
# throughout the event it cites as its reason for existing, and it always would have been.
#
# The gap was DIAGNOSED IN WRITING on 2026-08-04 at the top of tests/test_invariant_efficacy.sh,
# and only the FIXTURE was corrected. THE CURE WAS NEVER BUILT. Its second instance arrived on
# 2026-08-06: the live relay running sha-1e40093ac21e against a committed sha-f7a9a0759a93, with
# every offline gate green. That is the same shape as the call-site audit before ruling 4: a
# diagnosis is not a cure, and this project has now produced that shape twice.
#
# CLUSTER-GATED BY NECESSITY, and it PENDs rather than passing when no cluster is reachable. A check
# that cannot reach its subject has not checked it. verify_invariants.sh is otherwise entirely
# offline and stays that way: this is the only entry that reaches out, and it degrades to PEND so
# `make test-offline` is unchanged without credentials.
if [ "${INV19_SKIP:-0}" = "1" ]; then
  # THE EFFICACY HARNESS SETS THIS. That harness runs verify_invariants.sh once per injection, and
  # it declares itself offline with no cluster and no credentials. INV19 is the only entry that
  # reaches out, so leaving it live would (a) quietly make the harness depend on a cluster and
  # (b) add a kubectl round trip to every one of thirty-odd runs. INV19 has its own fixtures, which
  # replay CAPTURED cluster states and are strictly better evidence than a live read inside a loop
  # that is not testing it.
  printf "PEND INV19 running images: skipped by INV19_SKIP (the efficacy harness runs offline and covers this invariant with captured-state fixtures instead). NOT a pass.\n"
elif ! command -v kubectl >/dev/null 2>&1; then
  printf "PEND INV19 running images: kubectl is not on PATH, so running images were not read. NOT a pass.\n"
else
  inv19_out=$(${PYBIN:-./.venv/bin/python} tests/check_running_images.py 2>&1)
  case $? in
    0) ok "INV19 running images: $(printf '%s' "$inv19_out" | sed 's/^OK //')" ;;
    2) printf "PEND INV19 running images: %s\n" "$(printf '%s' "$inv19_out" | sed 's/^PEND //' | head -1)" ;;
    *) no "INV19 running images: $(printf '%s' "$inv19_out" | grep '^FAIL' | tail -1 | sed 's/^FAIL //')" ;;
  esac
fi

echo
if [ "$fail" -eq 0 ]; then echo "ALL INVARIANTS PASS"; else echo "INVARIANT FAILURES ABOVE"; fi
exit $fail
