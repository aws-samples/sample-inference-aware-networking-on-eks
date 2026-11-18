# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Stage 3 gate: SLA tier isolation (Option A).

Asserts both halves, in order:

  Gate A, routing. A batch-headered request is served by a pod labelled tier=batch running
  on a node labelled tier=batch, and an interactive request likewise. Proven from the Envoy
  access log, which pairs the request's correlation id with the upstream pod the Endpoint
  Picker selected. This is the authoritative record, not an inference from timing.

  Gate B, isolation. Under a saturating batch flood, interactive p95 TTFT stays under
  INTERACTIVE_TTFT_TARGET_MS.

Why this test is shaped defensively. An earlier version measured only Gate B, hardcoded a
stale model name, and never checked the HTTP status. Run against a cluster with every
serving replica scaled to zero it reported "PASS: interactive isolated from batch flood"
after 51 consecutive HTTP 500s, because an error response with an empty body returns fast
and an unchecked stream yields no lines. A gate that passes when nothing is running is
worse than no gate. So:

  - the model name is read from the live Deployment, never hardcoded (a stale literal
    yields a 404, which reads as a broken route instead of a stale test);
  - every response is status-checked, and a response that streams zero chunks fails;
  - zero ready replicas is a hard failure, not a fast pass;
  - the client-to-gateway network floor is measured, and the run aborts if that floor is
    a large share of the target, because then the number says more about where the client
    is sitting than about tier isolation. Run this from inside the cluster's region.

Run: ./.venv/bin/python tests/validation/test_tier_isolation.py
Requires: GATEWAY, kubectl context on the cluster, both tiers scaled up.
"""
import concurrent.futures as cf
import importlib
import os
import statistics
import sys
import time
import uuid

import requests

# The pod/node attribution join is shared with loadtest/build_trace.py so the two cannot
# drift; see loadtest/cluster_probe.py.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "loadtest"))
cp = importlib.import_module("cluster_probe")

# DISCOVERED, not required. `make stage-3` calls this test, and demanding an environment variable
# the published walkthrough never mentions made stage-3 fail AS PRINTED on 2026-08-08, one stage
# after the same class was closed for the shell callers and this Python sibling was missed.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
from discover import reachable_gateway  # noqa: E402

GATEWAY, _gw_cleanup = reachable_gateway()
import atexit; atexit.register(_gw_cleanup)
print(f"gateway (reachable): {GATEWAY}")
TARGET_MS = int(os.environ.get("INTERACTIVE_TTFT_TARGET_MS", "500"))
# Abort if the bare client-to-gateway round trip already eats this share of the target.
FLOOR_FRACTION = float(os.environ.get("TTFT_FLOOR_FRACTION", "0.2"))
FLOOD = int(os.environ.get("BATCH_FLOOD_REQUESTS", "40"))
SAMPLES = int(os.environ.get("INTERACTIVE_SAMPLES", "10"))

TIERS = {"interactive": "vllm-llama3-interactive", "batch": "vllm-llama3-batch"}

# PREMIUM IS DELIBERATELY NOT IN TIERS. Added 2026-09-22 (the maintainer's ruling: skip loudly).
#
# TIERS serves two masters: preflight() hard-fails any member with zero ready replicas, and Gate B's
# flood is a two-tier experiment. Premium ships one replica and NO ScaledObject, so zero is its
# normal resting state; as a TIERS member it would fail this test on a healthy cluster. And premium
# cannot be reached by call()'s one-header form at all: a premium request carrying only the
# objective matches the one-header batch rule or the catch-all and is served by the STANDARD pool,
# which would make a premium assertion demonstrate the very downgrade the capability axis exists to
# prevent. So premium is its own case: both two-header pairs, attributed to the winning route rule.
#
# SKIP IS LOUD AND NEVER A PASS. A conditional gate can rot into a gate that never runs, the
# DEBT-PASSING-PATH failure mode this repository tracks by name. The mitigation is procedural and
# was ruled with the skip: the premium bring-up and this case run together in the batch that
# introduced them, so the PASS branch has executed for real at least once, and the skip line is
# printed in every report so a run that skipped cannot be read as one that verified.
PREMIUM = "vllm-llama3-premium"
PREMIUM_PAIRS = (("interactive", "premium"), ("batch", "premium"))

# ECONOMY, the same shape and for the same reasons (L4 branch ruling, 2026-09-23). One replica and
# no ScaledObject, so zero is a legitimate resting state and a hard failure would be wrong; and its
# only routable pair is batch+economy, because the relay coalesces interactive+economy UP to
# standard before admission (6.9.1), so a rule for that pair would serve a cell nothing emits.
ECONOMY = "vllm-llama3-economy"
ECONOMY_PAIRS = (("batch", "economy"),)
ROUTE = "llm-route"


def call(tier: str, model: str, correlation_id: str = "", capability: str = "") -> float:
    """One streaming chat completion. Returns TTFT in ms.

    Raises on a non-2xx status, and on a response that streams no chunks: both are
    failures that the previous version recorded as fast, healthy latencies.
    """
    headers = {"content-type": "application/json"}
    if capability:
        # The TWO-HEADER form: both axes explicit, because the pool is selected from the pair and
        # never from either header alone (D-3). Used by the premium case.
        headers["x-gateway-inference-objective"] = tier
        headers["x-inference-model-class"] = capability
    elif tier == "batch":
        headers["x-gateway-inference-objective"] = "batch"
    if correlation_id:
        headers["x-correlation-id"] = correlation_id
    body = {"model": model,
            "messages": [{"role": "user", "content": "isolation probe"}],
            "stream": True}
    t0 = time.time()
    with requests.post(f"{GATEWAY}/v1/chat/completions", json=body, headers=headers,
                       stream=True, timeout=60) as r:
        r.raise_for_status()
        first = next(
            (ln for ln in r.iter_lines() if ln),
            None,
        )
        elapsed = (time.time() - t0) * 1000
    if first is None:
        raise AssertionError(
            f"{tier} request {correlation_id or ''} returned HTTP {r.status_code} but "
            "streamed zero chunks, so no token was ever generated"
        )
    return elapsed


def network_floor_ms() -> float:
    """Median client-to-gateway round trip, measured on a path Envoy answers itself.

    No upstream is involved, so this is pure network plus proxy overhead: the floor under
    every TTFT this test can observe.
    """
    samples = []
    for _ in range(5):
        t0 = time.time()
        try:
            requests.get(f"{GATEWAY}/__tier_isolation_floor_probe", timeout=15)
        except requests.RequestException as e:
            raise AssertionError(f"gateway {GATEWAY} is not reachable: {e}") from e
        samples.append((time.time() - t0) * 1000)
    return statistics.median(samples)


def preflight() -> str:
    """Resolve the served model and refuse to run without real capacity."""
    model = os.environ.get("MODEL") or cp.served_model(TIERS["interactive"])
    print(f"model (from the live Deployment): {model}")

    ready = {t: cp.ready_replicas(d) for t, d in TIERS.items()}
    print(f"ready replicas: {ready}")
    empty = [t for t, n in ready.items() if n < 1]
    if empty:
        raise AssertionError(
            "FAIL: no capacity to test. Tiers with zero ready replicas: "
            f"{', '.join(empty)}. Tier isolation cannot be demonstrated against a "
            "scaled-to-zero cluster; scale the serving tiers up and re-run."
        )

    floor = network_floor_ms()
    budget = TARGET_MS * FLOOR_FRACTION
    print(f"client-to-gateway network floor: {round(floor)} ms "
          f"(must stay under {round(budget)} ms, {int(FLOOR_FRACTION * 100)}% of the "
          f"{TARGET_MS} ms target)")
    if floor > budget:
        raise AssertionError(
            f"FAIL: the network floor alone is {round(floor)} ms of the {TARGET_MS} ms "
            "target, so a p95 measured here describes client distance, not tier "
            "isolation. Run this from inside the cluster's region, or raise "
            "INTERACTIVE_TTFT_TARGET_MS deliberately."
        )
    return model


def gate_a_routing(model: str) -> None:
    """Each objective is served by its own tier, proven from the gateway access log."""
    cids = {tier: f"tier-iso-{tier}-{uuid.uuid4().hex[:12]}" for tier in TIERS}
    for tier, cid in cids.items():
        ttft = call(tier, model, correlation_id=cid)
        print(f"  {tier:12s} request ok, TTFT {round(ttft)} ms, cid={cid}")

    pods, nodes = cp.pod_map(), cp.node_tiers()
    traces, deadline = {}, time.time() + 45
    while time.time() < deadline:
        traces = cp.envoy_traces()
        if all(c in traces for c in cids.values()):
            break
        # Deliberate fixed wait in a measurement or test script: the access log is flushed asynchronously.
        time.sleep(3)  # nosemgrep

    for tier, cid in cids.items():
        got = cp.attribute(cid, traces, pods, nodes)
        print(f"  {tier:12s} -> pod={got['pod']} pod_tier={got['pod_tier']} "
              f"node={got['node']} node_tier={got['node_tier']} code={got['code']}")
        assert got["pod_tier"] == tier, (
            f"FAIL routing: a {tier} request was served by pod {got['pod']} which is "
            f"labelled tier={got['pod_tier']}"
        )
        assert got["node_tier"] == tier, (
            f"FAIL routing: a {tier} request was served on node {got['node']} which is "
            f"labelled tier={got['node_tier']}; the pod landed on the wrong node pool"
        )
    print("PASS gate A: each objective was served by its own tier (pod and node labels)")


def live_route_rules() -> list:
    """The HTTPRoute's rules as the API server holds them (not the committed file)."""
    import json
    return json.loads(cp.sh("kubectl", "-n", cp.NS, "get", "httproute", ROUTE, "-o", "json"))["spec"]["rules"]


def tier_skip_reason(deployment: str) -> str:
    """Empty when the tier can be tested; otherwise the reason, printed verbatim as the SKIP line."""
    try:
        n = cp.ready_replicas(deployment)
    except cp.ProbeError as e:
        if "NotFound" in str(e) or "not found" in str(e):
            return f"{deployment} is not deployed"
        raise  # an unreadable cluster is an error, never a skip
    if n < 1:
        return (f"{deployment} has 0 ready replicas (one replica and no ScaledObject by design, so zero is "
                f"its resting state; scale it to 1 to verify)")
    return ""


def gate_a_capability(label: str, deployment: str, pairs: tuple) -> str:
    """Every pair of a capability class reaches ITS tier BY ITS TWO-HEADER RULE. PASS or SKIP.

    Three assertions per pair, each defeatable alone: the pod and node are premium (where it
    landed), the gateway saw modelclass=premium (what it was asked), and the rule that WON matches
    exactly this pair with two headers (why it landed there). The third is the one that proves
    precedence by header count rather than by list order or by luck: with premium the only replica
    up, a fall-through to the one-header batch rule would land on the same pod and pass the first.
    """
    why = tier_skip_reason(deployment)
    if why:
        print(f"SKIP gate A {label}: {why}. {label.capitalize()} routing was NOT verified this run. "
              f"THIS IS NOT A PASS.")
        return "SKIP"
    model = os.environ.get(f"{label.upper()}_MODEL") or cp.served_model(deployment)
    print(f"  {label} model (from the live Deployment): {model}")
    rules = live_route_rules()
    cids = {pair: f"tier-iso-{pair[0]}-{pair[1]}-{uuid.uuid4().hex[:12]}" for pair in pairs}
    for (sla, cap), cid in cids.items():
        ttft = call(sla, model, correlation_id=cid, capability=cap)
        print(f"  {sla + '+' + cap:20s} request ok, TTFT {round(ttft)} ms, cid={cid}")

    pods, nodes = cp.pod_map(), cp.node_tiers()
    traces, deadline = {}, time.time() + 45
    while time.time() < deadline:
        traces = cp.envoy_traces()
        if all(c in traces for c in cids.values()):
            break
        # Deliberate fixed wait in a measurement or test script: poll interval, because the access log is flushed asynchronously.
        time.sleep(3)  # nosemgrep

    for (sla, cap), cid in cids.items():
        got = cp.attribute(cid, traces, pods, nodes)
        print(f"  {sla + '+' + cap:20s} -> rule={got['rule']} pod={got['pod']} pod_tier={got['pod_tier']} "
              f"node_tier={got['node_tier']} modelclass={got['modelclass']} code={got['code']}")
        assert got["pod_tier"] == label and got["node_tier"] == label, (
            f"FAIL {label} routing: {sla}+{cap} served by pod tier={got['pod_tier']} on node "
            f"tier={got['node_tier']}")
        assert got["modelclass"] == cap, (
            f"FAIL {label} routing: the gateway logged modelclass={got['modelclass']}, so the "
            "capability header never reached it")
        try:
            idx = int(got["rule"].rsplit("/", 1)[1])
            won = {h["name"]: h["value"] for h in rules[idx]["matches"][0].get("headers", [])}
        except (ValueError, IndexError, KeyError) as e:
            raise AssertionError(f"FAIL {label} routing: cannot resolve winning rule {got['rule']!r}: {e}")
        want = {"x-gateway-inference-objective": sla, "x-inference-model-class": cap}
        assert won == want, (
            f"FAIL {label} routing: {sla}+{cap} was won by rule {idx} matching {won}, not the two-header "
            f"{label} rule {want}. A one-header rule outranking it is the silent downgrade.")
    print(f"PASS gate A {label}: every two-header pair won by its own {label} rule, served on {label}")
    return "PASS"


def gate_a_premium() -> str:
    return gate_a_capability("premium", PREMIUM, PREMIUM_PAIRS)


def gate_a_economy() -> str:
    return gate_a_capability("economy", ECONOMY, ECONOMY_PAIRS)


def gate_b_isolation(model: str) -> None:
    """Interactive TTFT holds under a saturating batch flood."""
    baseline = call("interactive", model)
    print(f"  baseline interactive TTFT: {round(baseline)} ms")

    failures, samples = [], []
    with cf.ThreadPoolExecutor(max_workers=20) as ex:
        futs = [ex.submit(call, "batch", model) for _ in range(FLOOD)]
        # Deliberate fixed wait in a measurement or test script: let the flood build.
        time.sleep(2)  # nosemgrep
        for _ in range(SAMPLES):
            try:
                samples.append(call("interactive", model))
            except Exception as e:  # noqa: BLE001
                failures.append(f"interactive sample: {e}")
        for f in cf.as_completed(futs):
            try:
                f.result()
            except Exception as e:  # noqa: BLE001
                failures.append(f"batch flood: {e}")

    print(f"  requests: {SAMPLES} interactive samples, {FLOOD} batch flood, "
          f"{len(failures)} failed")
    if failures:
        raise AssertionError(
            f"FAIL: {len(failures)} of {SAMPLES + FLOOD} requests failed, so the "
            "latency below is not a measurement of a working system. First: "
            f"{failures[0]}"
        )
    p95 = statistics.quantiles(samples, n=20)[-1]
    print(f"  interactive p95 TTFT under batch flood: {round(p95)} ms "
          f"(target {TARGET_MS})")
    assert p95 < TARGET_MS, (
        f"FAIL: interactive starved, p95 {round(p95)} >= {TARGET_MS}"
    )
    print("PASS gate B: interactive isolated from the batch flood")


def main() -> int:
    if "--gate-a-only" in sys.argv[1:]:
        # ROUTING ONLY, NO LATENCY. Gate A proves WHICH TIER served a request, from the gateway's
        # access log; it does not measure time, so the client-to-gateway floor preflight() enforces
        # for Gate B is irrelevant to it. Added 2026-09-23, when a driver in Europe measured a
        # 594 ms floor against a 500 ms target and preflight correctly refused the whole file:
        # Gate B needs an in-region client, and coupling Gate A to that made the routing assertion
        # unrunnable for a reason that has nothing to do with routing.
        model = os.environ.get("MODEL") or cp.served_model(TIERS["interactive"])
        ready = {t: cp.ready_replicas(d) for t, d in TIERS.items()}
        empty = [t for t, n in ready.items() if n < 1]
        if empty:
            raise AssertionError(f"FAIL: no capacity to test. Tiers with zero ready replicas: {empty}")
        print(f"== gate A only: routing (objective -> tier), model {model}, ready {ready} ==")
        gate_a_routing(model)
        premium, economy = gate_a_premium(), gate_a_economy()
        print("PASS: routing holds for interactive and batch; premium: "
              + ("PASS" if premium == "PASS" else "SKIPPED, NOT VERIFIED")
              + "; economy: " + ("PASS" if economy == "PASS" else "SKIPPED, NOT VERIFIED"))
        return 0

    if "--economy-only" in sys.argv[1:]:
        print("== gate A economy only (the batch+economy two-header pair -> economy tier) ==")
        return 0 if gate_a_economy() == "PASS" else 2

    if "--premium-only" in sys.argv[1:]:
        # STANDALONE PREMIUM CASE: no interactive/batch preflight and no Gate B, so premium can be
        # verified without paying for two more GPU nodes. Exit 2 on SKIP (PEND, never a pass).
        print("== gate A premium only (both two-header premium pairs -> premium tier) ==")
        return 0 if gate_a_premium() == "PASS" else 2
    print("== preflight ==")
    model = preflight()
    print("== gate A: routing (objective -> tier) ==")
    gate_a_routing(model)
    print("== gate A premium: routing (both two-header premium pairs -> premium tier) ==")
    premium = gate_a_premium()
    print("== gate A economy: routing (the batch+economy two-header pair -> economy tier) ==")
    economy = gate_a_economy()
    print("== gate B: isolation (interactive TTFT under batch flood) ==")
    gate_b_isolation(model)
    print("PASS: tier isolation holds (routing and latency); premium: "
          + ("PASS" if premium == "PASS" else "SKIPPED, NOT VERIFIED")
          + "; economy: " + ("PASS" if economy == "PASS" else "SKIPPED, NOT VERIFIED"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
