#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
THE REQTRACE PARSER AGREES WITH THE LIVE ACCESS-LOG FORMAT. Offline, no cluster.

WHY THIS EXISTS. cluster_probe.REQTRACE's own comment said "if that format changes this regex stops
matching and callers fail their assertions, which is the intended behaviour". The format DID
change (2026-08-04, modelclass= and cluster= added for the two-header routing), the regex DID stop
matching, and nothing failed for seven weeks, because the only callers are cluster-gated tests
nobody ran in that window. A loud failure that only fires on a GPU cluster is a quiet failure
everywhere else. Found 2026-09-22 on the first live premium request.

SO THE SAMPLE LINE IS DERIVED FROM THE MANIFEST, never written by hand. The format string is read
out of manifests/03-routing/envoyproxy-accesslog.yaml and each %OPERATOR(...)% is substituted with
a known token. A hand-written sample would be a second copy of the format, free to go stale
exactly the way the regex did.

THE ARCHIVE IS THE REGRESSION CONTROL. loadtest/results/ is immutable and holds old-format lines
that build_trace.py and phased_verdict.py must still replay. The pattern must parse those too,
with identical extraction to the pattern it replaced.
"""
from __future__ import annotations

import os
import re
import sys

import yaml

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
sys.path.insert(0, os.path.join(ROOT, "loadtest"))
import cluster_probe as cp  # noqa: E402

MANIFEST = os.path.join(ROOT, "manifests/03-routing/envoyproxy-accesslog.yaml")
ARCHIVE = os.path.join(ROOT, "loadtest/results/lt10/capture/envoy-access.log")

# One known token per operator, so extraction can be asserted field by field.
TOKENS = {
    "%REQ(X-CORRELATION-ID)%": "cid-fixture-1",
    "%REQ(X-GATEWAY-INFERENCE-OBJECTIVE)%": "interactive",
    "%REQ(X-INFERENCE-MODEL-CLASS)%": "premium",
    "%UPSTREAM_CLUSTER%": "httproute/inference/llm-route/rule/2",
    "%REQ(:METHOD)%": "POST",
    "%REQ(X-ENVOY-ORIGINAL-PATH?:PATH)%": "/v1/chat/completions",
    "%RESPONSE_CODE%": "200",
    "%UPSTREAM_HOST%": "10.0.0.7:8000",
    "%RESPONSE_FLAGS%": "-",
    "%DURATION%": "253",
    "%RESPONSE_DURATION%": "252",
    "%RESP(X-ENVOY-UPSTREAM-SERVICE-TIME)%": "-",
}

# The pattern this replaced, kept ONLY as the archive's regression reference.
PREVIOUS = re.compile(
    r"REQTRACE cid=(?P<cid>\S+) objective=(?P<obj>\S+) method=\S+ path=\S+ "
    r"code=(?P<code>\S+) upstream=(?P<upstream>\S+)"
)


def live_format() -> str:
    for doc in yaml.safe_load_all(open(MANIFEST)):
        if not doc or doc.get("kind") != "EnvoyProxy":
            continue
        for s in doc["spec"]["telemetry"]["accessLog"]["settings"]:
            text = s.get("format", {}).get("text", "")
            if "REQTRACE" in text:
                return text.strip()
    raise AssertionError(f"no REQTRACE format found in {MANIFEST}")


def main() -> int:
    fmt = live_format()
    line = fmt
    for op, tok in TOKENS.items():
        line = line.replace(op, tok)
    leftover = re.findall(r"%[A-Z_]+(?:\([^)]*\))?%", line)
    assert not leftover, (
        f"the live format uses operators this test has no token for: {leftover}. Add them to "
        "TOKENS; an unrendered operator means the sample no longer matches what Envoy emits.")

    m = cp.REQTRACE.search(line)
    assert m, f"cluster_probe.REQTRACE does NOT parse the live format.\n  format: {fmt}\n  line:   {line}"
    got = {k: m.group(k) for k in ("cid", "obj", "cls", "cluster", "code", "upstream")}
    want = {"cid": "cid-fixture-1", "obj": "interactive", "cls": "premium",
            "cluster": "httproute/inference/llm-route/rule/2", "code": "200", "upstream": "10.0.0.7:8000"}
    assert got == want, f"live-format extraction wrong: got {got}, want {want}"
    print("OK  1 the shared parser reads the LIVE format rendered from the manifest, every field")

    # The negative control: the previous pattern must FAIL on the live line, or this test could not
    # have caught the defect it exists for.
    assert not PREVIOUS.search(line), "control broken: the previous pattern parses the live line"
    print("OK  2 control: the pattern it replaced does NOT parse the live format (the defect, reproduced)")

    lines = open(ARCHIVE, errors="replace").read().splitlines()
    old = [PREVIOUS.search(ln) for ln in lines]
    new = [cp.REQTRACE.search(ln) for ln in lines]
    n_old, n_new = sum(1 for x in old if x), sum(1 for x in new if x)
    assert n_old > 0, "the archive regression control has no REQTRACE lines, so it proves nothing"
    assert n_old == n_new, f"archive regression: previous pattern matched {n_old}, shared matches {n_new}"
    for a, b in zip(old, new):
        if a:
            assert (a["cid"], a["obj"], a["code"], a["upstream"]) == (b["cid"], b["obj"], b["code"], b["upstream"])
    print(f"OK  3 archive regression: {n_new} immutable old-format lines parse with identical extraction")

    # One definition: the trace builder and the verdict script must use this object, not a copy.
    import build_trace, phased_verdict  # noqa: E401,E402
    assert build_trace.REQTRACE is cp.REQTRACE and phased_verdict.REQTRACE is cp.REQTRACE, (
        "a loadtest script defines its own REQTRACE again; three copies went stale together once")
    print("OK  4 build_trace.py and phased_verdict.py share cluster_probe's single definition")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
