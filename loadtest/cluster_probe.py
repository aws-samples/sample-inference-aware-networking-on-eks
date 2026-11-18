# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Single source for "which pod served this request, and on what hardware".

Extracted so the Stage 3 tier-isolation gate and the per-request trace builder cannot
drift apart. Both need the same three-step join, and before this module existed only
build_trace.py implemented it, which meant the gate documented a routing assertion it
did not make (see tests/validation/test_tier_isolation.py).

The join:
  1. Send the request with an x-correlation-id header.
  2. Read the Envoy access log. Its REQTRACE line pairs that correlation id with
     %UPSTREAM_HOST%, the vLLM pod IP:port the Endpoint Picker actually selected. This is
     the authoritative record; the format is defined in
     manifests/03-routing/envoyproxy-accesslog.yaml.
  3. Resolve pod IP -> pod name, pod tier label, node name, and the node's tier label.

Every helper fails loudly. A kubectl error must not degrade into an empty map, because an
empty map makes every request's attribution read as "unknown" while the caller still
emits a verdict.
"""
import json
import os
import re
import subprocess

NS = os.environ.get("NAMESPACE", "inference")
ENVOY_NS = os.environ.get("ENVOY_NS", "envoy-gateway-system")

# Defined by manifests/03-routing/envoyproxy-accesslog.yaml. If that format changes this
# regex stops matching and callers fail their assertions, which is the intended behaviour:
# a silent no-match would look like "no requests were traced".
#
# TWO FORMATS, BOTH PARSED, and the second exists because the first went stale silently. The
# two-header work (2026-08-04) added modelclass= and cluster= between objective= and method=, and
# this pattern was not updated, so from then on it matched NO current line: Gate A attribution
# could not resolve a request for any tier. Found 2026-09-22 on the first live premium request,
# whose REQTRACE line was present in the log and invisible to this parser. The fields are OPTIONAL
# rather than required because loadtest/results/ is immutable and holds 2,538 old-format lines
# (lt10/capture/envoy-access.log) that build_trace.py and phased_verdict.py must still replay.
# tests/validation/test_reqtrace_format.py renders the LIVE format string from the manifest and
# asserts this pattern parses it, so the next format change fails offline instead of in a gate.
#
# cluster= names the ROUTE RULE that won (httproute/<ns>/<route>/rule/<N>), which is stronger
# evidence than the upstream pod: it proves which match set took precedence, not merely where the
# request landed.
REQTRACE = re.compile(
    r"REQTRACE cid=(?P<cid>\S+) objective=(?P<obj>\S+)"
    r"(?: modelclass=(?P<cls>\S+))?(?: cluster=(?P<cluster>\S+))?"
    r" method=\S+ path=\S+ code=(?P<code>\S+) upstream=(?P<upstream>\S+)"
)

# Serving pods for the NVIDIA tiers. The Neuron Deployment carries app=vllm-neuron, not
# app=vllm-llama3, so it is NOT matched by this selector. That is deliberate here and is a
# known gap: the Neuron tier has no InferencePool or InferenceObjective, so nothing routes
# to it and there is no tier isolation to assert for it yet.
VLLM_SELECTOR = "app=vllm-llama3"


class ProbeError(RuntimeError):
    """A cluster read failed. Never swallowed: an empty result is not the same as a fact."""


def sh(*args: str) -> str:
    """Run a command and return stdout. Raises on a non-zero exit."""
    # Every caller passes literal kubectl arguments: list form, no shell, no external input.
    proc = subprocess.run(args, capture_output=True, text=True)  # nosec B603  # nosemgrep
    if proc.returncode != 0:
        raise ProbeError(
            "command failed (rc=%d): %s\n%s"
            % (proc.returncode, " ".join(args), (proc.stderr or "").strip())
        )
    return proc.stdout


def pod_map(selector: str = VLLM_SELECTOR) -> dict:
    """podIP -> {pod, tier, node} for every matching serving pod."""
    out = sh("kubectl", "-n", NS, "get", "pods", "-l", selector, "-o", "json")
    m = {}
    for it in json.loads(out)["items"]:
        ip = it.get("status", {}).get("podIP")
        if not ip:
            continue
        m[ip] = {
            "pod": it["metadata"]["name"],
            "tier": it["metadata"].get("labels", {}).get("tier", "?"),
            "node": it["spec"].get("nodeName", "?"),
        }
    return m


def node_tiers() -> dict:
    """nodeName -> the node's tier label, set by the Karpenter NodePool.

    The node label is the stronger check: a pod carrying tier=batch that landed on an
    interactive node means the nodeSelector or the NodePool is wrong, and only the node
    label catches it.
    """
    out = sh("kubectl", "get", "nodes", "-o", "json")
    return {
        n["metadata"]["name"]: n["metadata"].get("labels", {}).get("tier", "?")
        for n in json.loads(out)["items"]
    }


def envoy_deployment() -> str:
    """The Envoy proxy Deployment fronting the inference gateway."""
    for line in sh("kubectl", "-n", ENVOY_NS, "get", "deploy", "-o", "name").splitlines():
        if "envoy-inference" in line:
            return line.strip()
    raise ProbeError(
        "no envoy-inference Deployment in namespace %s; without it no request can be "
        "attributed to a backend" % ENVOY_NS
    )


def envoy_traces() -> dict:
    """correlation id -> {upstream, code, objective, modelclass, cluster} from the gateway access log."""
    logs = sh("kubectl", "-n", ENVOY_NS, "logs", envoy_deployment(), "--tail=-1")
    traces = {}
    for line in logs.splitlines():
        mm = REQTRACE.search(line)
        if mm:
            traces[mm.group("cid")] = {
                "upstream": mm.group("upstream"),
                "code": mm.group("code"),
                "objective": mm.group("obj"),
                "modelclass": mm.group("cls") or "-",
                "cluster": mm.group("cluster") or "-",
            }
    return traces


def attribute(correlation_id: str, traces: dict, pods: dict, nodes: dict) -> dict:
    """Resolve one correlation id to {upstream, code, pod, pod_tier, node, node_tier, modelclass, rule}.

    Raises if the gateway never logged the id: an unattributable request cannot be used
    to argue that routing worked.
    """
    t = traces.get(correlation_id)
    if not t:
        raise ProbeError(
            "correlation id %s has no REQTRACE line in the Envoy access log, so the "
            "backend that served it is unknown" % correlation_id
        )
    ip = t["upstream"].split(":")[0]
    p = pods.get(ip)
    if not p:
        raise ProbeError(
            "upstream %s for correlation id %s does not match any pod selected by %r"
            % (t["upstream"], correlation_id, VLLM_SELECTOR)
        )
    return {
        "upstream": t["upstream"],
        "code": t["code"],
        "pod": p["pod"],
        "pod_tier": p["tier"],
        "node": p["node"],
        "node_tier": nodes.get(p["node"], "?"),
        "modelclass": t.get("modelclass", "-"),
        "rule": t.get("cluster", "-"),
    }


# IN-CLUSTER VANTAGE (2026-09-23). A driver running INSIDE the cluster reaches a pod's /metrics
# directly over HTTP and needs no Kubernetes API access at all, so the load-driver Job carries NO
# RBAC. Set POD_ENDPOINTS to a comma-separated list of ip:port and running_pods/pod_metrics use the
# HTTP path; unset, both behave exactly as before. This exists because the ramp's arrival pattern is
# its independent variable: a kubectl port-forward from a laptop would serialise arrivals and
# measure the tunnel instead of the router.
POD_ENDPOINTS = [e.strip() for e in os.environ.get("POD_ENDPOINTS", "").split(",") if e.strip()]


def pod_metrics(pod: str, counters) -> dict:
    """Scrape the named vLLM counters from one pod's /metrics endpoint.

    Single-sourced: lt8_direct.py, lt8_cell.py, pub_routing.py and spike_b_routing.py all
    used a verbatim copy of this, and every copy dropped the kubectl return code. A failed
    exec then produced empty output, every counter read 0.0, and the caller subtracted a
    start snapshot from an all-zero end snapshot to get a NEGATIVE delta, which it still
    wrote out as a prefix-cache hit rate. An aborted run that gets re-run is strictly better
    than a published number that is wrong, so this raises.
    """
    if ":" in pod:            # an ip:port endpoint: scrape it directly (in-cluster vantage)
        import urllib.request
        raw = urllib.request.urlopen(f"http://{pod}/metrics", timeout=10).read().decode()  # nosec B310 - fixed http:// URL to a pod address read from the cluster
    else:
        raw = sh("kubectl", "-n", NS, "exec", pod, "--", "python3", "-c",
                 "import urllib.request;"
                 "print(urllib.request.urlopen('http://localhost:8000/metrics',timeout=10)"
                 ".read().decode())")
    out = {c: 0.0 for c in counters}
    for line in raw.splitlines():
        if line.startswith("#") or not line.startswith("vllm:"):
            continue
        name = line.split("{")[0].split(" ")[0]
        if name in out:
            try:
                out[name] += float(line.rsplit(" ", 1)[1])
            except (ValueError, IndexError):
                pass
    if not raw.strip():
        raise ProbeError(
            "pod %s returned an empty /metrics body; every counter would read 0.0 and the "
            "resulting delta would be meaningless" % pod
        )
    return out


def running_pods(selector: str) -> list:
    """Pod names, or ip:port endpoints when POD_ENDPOINTS is set (in-cluster vantage)."""
    if POD_ENDPOINTS:
        return list(POD_ENDPOINTS)
    return _running_pods_via_kubectl(selector)


def _running_pods_via_kubectl(selector: str) -> list:
    """Names of Running pods matching a selector. Raises if the read fails."""
    out = sh("kubectl", "-n", NS, "get", "pod", "-l", selector,
             "--field-selector=status.phase=Running",
             "-o", "jsonpath={range .items[*]}{.metadata.name}{\"\\n\"}{end}")
    return [p for p in out.split("\n") if p.strip()]


def served_model(deployment: str) -> str:
    """The model a Deployment actually serves, read from its --model arg.

    Read live rather than hardcoded: a stale literal yields a 404 from vLLM, which reads
    as a broken route instead of a stale test. tests/smoke/test_routing_reachable.sh names
    this same hazard.
    """
    out = sh("kubectl", "-n", NS, "get", "deploy", deployment, "-o",
             "jsonpath={.spec.template.spec.containers[0].args}")
    for arg in json.loads(out):          # kubectl emits the args as a JSON array
        if arg.startswith("--model="):
            return arg.split("=", 1)[1]
    raise ProbeError("no --model arg on Deployment %s (args: %s)" % (deployment, out))


def ready_replicas(deployment: str) -> int:
    """Ready replica count for a Deployment. 0 means there is no capacity to test."""
    out = sh("kubectl", "-n", NS, "get", "deploy", deployment, "-o",
             "jsonpath={.status.readyReplicas}")
    return int(out.strip() or 0)
