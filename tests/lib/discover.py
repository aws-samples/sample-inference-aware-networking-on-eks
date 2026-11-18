# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Address discovery for Python callers. The sibling of tests/lib/discover.sh.

WHY BOTH EXIST, and the reason is a mistake made in the batch that wrote them. The shell library
closed the "configured, not discovered" class for shell callers on 2026-08-08, and the very next
stage failed with:

    GATEWAY = os.environ["GATEWAY"]
    KeyError: 'GATEWAY'

The class fix had been applied to the shell family and its PYTHON SIBLING WAS MISSED, because the
grep that enumerated the class searched for the shell form `${VAR:?}` only. A class fix that
searches for one spelling of the class finds one spelling of the class.

So the enumeration that matters is: every consumer, in every language, found by searching for the
NAME rather than for a syntax.

    grep -rnE 'os\\.environ\\["GATEWAY"\\]|os\\.environ\\.get\\("GATEWAY"|\\$\\{GATEWAY' tests/ loadtest/

THE OVERRIDE ANNOUNCES ITSELF here too, for the same reason as in the shell version: an override is
the exact shape of the defect this closes, and a stale one that quietly wins is indistinguishable
from discovery working.
"""
from __future__ import annotations

import os
import subprocess
import sys

NS = "envoy-gateway-system"
LABEL = "gateway.envoyproxy.io/owning-gateway-name=inference-gateway"


def _kubectl(*args: str) -> str:
    try:
        # Fixed kubectl argv from this module's callers: list form, no shell.
        return subprocess.run(["kubectl", *args], capture_output=True, text=True,  # nosec B603
                              timeout=30).stdout.strip()
    except Exception:                                            # noqa: BLE001
        return ""


def _discover_from_cluster() -> str:
    svc = _kubectl("-n", NS, "get", "svc", "-l", LABEL,
                   "-o", "jsonpath={.items[0].metadata.name}")
    if svc:
        return f"http://{svc}.{NS}.svc.cluster.local"
    # Fallback: the ConfigMap the async worker already reads, so the test and the worker agree
    # about the address rather than discovering it two different ways.
    base = _kubectl("-n", "inference", "get", "cm", "gateway-endpoint",
                    "-o", "jsonpath={.data.base}")
    return base or ""


def discover_gateway() -> str:
    env = os.environ.get("GATEWAY", "").strip()
    if env:
        would = _discover_from_cluster()
        print("  NOTE GATEWAY is set and OVERRIDES discovery.", file=sys.stderr)
        print(f"       override : {env}", file=sys.stderr)
        print(f"       discovery would have returned: {would or '<nothing discoverable>'}",
              file=sys.stderr)
        if would and would != env:
            print("       THESE DISAGREE. If the override is stale, this line is the only thing "
                  "that says so.", file=sys.stderr)
        return env.rstrip("/")
    return _discover_from_cluster().rstrip("/")


def require_gateway() -> str:
    g = discover_gateway()
    if not g:
        raise SystemExit(
            "FAIL could not discover the inference gateway address, and GATEWAY is unset.\n"
            f"     Discovery looks for a Service labelled {LABEL} in {NS},\n"
            "     then falls back to the gateway-endpoint ConfigMap in the inference namespace.\n"
            "     Override with GATEWAY=http://host:port to point at something else on purpose."
        )
    return g


# REACHABILITY, a different question from discovery, and the 2026-08-08 run proved it twice.
#
# Discovery returns the correct address and an off-cluster caller still cannot use it: the gateway
# Service is ClusterIP, so `...svc.cluster.local` resolves inside the cluster and nowhere else. The
# shell library learned this first (curl exit 6); this is the same fix for Python callers, added in
# the same hour rather than one stage later, which is the whole lesson of this batch applied.
def reachable_gateway(port: int = 18081):
    """Return (url, cleanup). Port-forwards when the discovered address is in-cluster."""
    import socket
    import time

    g = require_gateway()
    if ".svc" not in g:
        return g, (lambda: None)

    svc = g.split("//", 1)[1].split(".", 1)[0]
    print(f"  in-cluster address detected; port-forwarding svc/{svc} -> 127.0.0.1:{port}",
          file=sys.stderr)
    # Fixed kubectl argv; the Service name is read from the cluster: list form, no shell.
    pf = subprocess.Popen(["kubectl", "-n", NS, "port-forward", f"svc/{svc}", f"{port}:80"],  # nosec B603
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # POLL, do not sleep. A fixed sleep encodes a guess about another process's readiness and loses
    # exactly when the cluster is busy, which is when a reader is most likely to be running this.
    for _ in range(40):
        with socket.socket() as s:
            s.settimeout(1)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                break
        # Deliberate fixed wait in a measurement or test script: poll interval while the port-forward comes up.
        time.sleep(1)  # nosemgrep
    else:
        pf.terminate()
        raise SystemExit(f"FAIL the port-forward to svc/{svc} never began listening on {port}.")
    return f"http://127.0.0.1:{port}", pf.terminate
