# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Chain B's committed constants: strictly ordered, and AGREEING WITH THE FILES THAT ACTUALLY APPLY.

This is NOT INV14. INV14 asserts Chain B from the committed RELAY manifest, and the relay does not
exist, so INV14 stays REGISTERED-NOT-IMPLEMENTED. What is checkable today is narrower and worth
having on its own: the ConfigMap introduced with this test restates two values whose authoritative
copies live elsewhere (the HTTPRoute's route timeout and the load balancer idle-timeout annotation),
and a restatement that nothing cross-checks is how two sources of truth begin. So the restatements
are cross-checked from the day they are written rather than from the day the relay lands.

The ordering it asserts, strict at every step:

    P  <  600 (route)  <  630 (relay upstream)  <  690 (exposure idle)

Equal values are a FAILURE, not a pass. That is the R.5 defect exactly: Chain A once had two equal
values, they raced, and a redelivered worker could double-run a job the first was still writing.

Offline. No cluster, no credentials.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CFG = ROOT / "manifests" / "03-routing" / "chain-b-timeouts.yaml"
ROUTE = ROOT / "manifests" / "03-routing" / "httproute.yaml"
# L LIVES WHEREVER THE CUTOVER PUT IT, and this test follows rather than hardcodes. Before the
# Phase 2 cutover the external annotation sits on the gateway's envoyService; after it, on the
# relay's exposure. Those are exactly the two topologies INV12 recognises, so this reads the same
# two and fails if it finds neither.
#
# This was found the honest way: the cutover moved the annotation and this test went RED because it
# was still looking at the gateway file. That is the cross-check doing its job, since the
# alternative was a restatement quietly describing a value nothing applied any more.
GATEWAY_EXPOSURE = ROOT / "manifests" / "03-routing" / "envoyproxy-accesslog.yaml"
RELAY_DIR = ROOT / "manifests" / "08-relay"


def exposure_sources() -> list:
    """Every committed file that could carry the external idle-timeout annotation, relay first."""
    return sorted(RELAY_DIR.glob("*.yaml")) + [GATEWAY_EXPOSURE]

failures: list[str] = []


def check(ok: bool, msg: str) -> None:
    if ok:
        print(f"OK   {msg}")
    else:
        print(f"FAIL {msg}")
        failures.append(msg)


def load_data(path: Path) -> dict[str, str]:
    """Read the ConfigMap's `data:` block without a YAML dependency.

    Deliberately literal: the block is flat string-to-string, and parsing it by hand keeps this
    test runnable on a fresh clone with no packages installed, which is the property `test-offline`
    promises a reader.
    """
    out: dict[str, str] = {}
    in_data = False
    for line in path.read_text().splitlines():
        if re.match(r"^data:\s*$", line):
            in_data = True
            continue
        if in_data:
            if line and not line.startswith((" ", "\t")):
                break
            m = re.match(r'^\s+([A-Z_]+):\s*"(.*)"\s*$', line)
            if m:
                out[m.group(1)] = m.group(2)
    return out


def main() -> int:
    d = load_data(CFG)
    required = [
        "ROUTE_TIMEOUT_SECONDS",
        "RELAY_UPSTREAM_TIMEOUT_SECONDS",
        "EXPOSURE_IDLE_TIMEOUT_SECONDS",
        "PROMOTION_DEADLINE_CEILING_SECONDS",
    ]
    missing = [k for k in required if k not in d]
    check(not missing, f"every Chain B constant is present (missing: {missing or 'none'})")
    if missing:
        return 1

    route = int(d["ROUTE_TIMEOUT_SECONDS"])
    relay = int(d["RELAY_UPSTREAM_TIMEOUT_SECONDS"])
    exposure = int(d["EXPOSURE_IDLE_TIMEOUT_SECONDS"])

    # 1. STRICT ordering. `<` and not `<=`, because equal values race.
    check(route < relay, f"route ({route}) < relay upstream ({relay}), strictly")
    check(relay < exposure, f"relay upstream ({relay}) < exposure idle ({exposure}), strictly")

    # 2. The restatements must AGREE with the files that actually apply. This is the whole reason
    #    the test exists alongside the ConfigMap rather than after it.
    route_txt = ROUTE.read_text()
    # The value is a quoted Gateway API duration ("600s"), so the quotes are part of the syntax
    # rather than optional. Matching without them found nothing and the cross-check said so, which
    # is the check earning its place on its first run: it was written against an assumed format and
    # the actual file corrected it.
    applied_routes = {int(x) for x in re.findall(r'request:\s*"?(\d+)s"?', route_txt)}
    check(
        route in applied_routes,
        f"ROUTE_TIMEOUT_SECONDS ({route}) appears in the HTTPRoute's applied timeouts {sorted(applied_routes) or '(none found)'}",
    )

    # WHERE L LIVES MOVED ON 2026-08-07, when the relay migrated from a Classic Load Balancer to a
    # Network Load Balancer provisioned by the AWS Load Balancer Controller.
    #
    #   Classic : aws-load-balancer-connection-idle-timeout: "690"
    #   NLB     : ...listener-attributes.TCP-80: tcp.idle_timeout.seconds=690
    #
    # THIS TEST IS THE SECOND CONSUMER OF THAT ANNOTATION and INV14 was the first. INV14 was
    # retaught and went green, and the suite still refused the commit because this one had not
    # been. That is the offline suite earning its keep: fixing the invariant that shouted was not
    # the same as finding everything that read the value, and only running everything found the
    # rest.
    #
    # Both forms are recognised so an unmigrated manifest still reports honestly, and BOTH PRESENT
    # is a failure below, because on an NLB the Classic annotation is inert and an inert value that
    # reads correctly beside a live one is the reassuring-proxy family.
    found = []
    for path in exposure_sources():
        if not path.exists():
            continue
        txt = path.read_text()
        for v in re.findall(r'tcp\.idle_timeout\.seconds=(\d+)', txt):
            found.append((path.name + " (NLB listener attribute)", int(v)))
        for v in re.findall(r'aws-load-balancer-connection-idle-timeout:\s*"(\d+)"', txt):
            found.append((path.name + " (Classic annotation)", int(v)))
    check(bool(found), f"an external idle-timeout is declared, in either the NLB listener-attribute "
                       f"form or the Classic annotation form, in one of "
                       f"{[p.name for p in exposure_sources()]}")
    if found:
        # Exactly one annotation must exist. Two would mean two external doors, which is the
        # half-finished cutover INV12 fails on, and this would otherwise silently agree with
        # whichever one it happened to read first.
        check(len(found) == 1, f"exactly ONE external idle-timeout declaration exists (found {found}). "
                               f"Two would mean either two external doors, or a Classic annotation "
                               f"left beside an NLB listener attribute where it is inert.")
        where, applied = found[0]
        check(
            applied == exposure,
            f"EXPOSURE_IDLE_TIMEOUT_SECONDS ({exposure}) equals the annotation actually applied in {where} ({applied})",
        )

    # 3. P. Unset is legal and is the recorded state: it is a product decision, not a derivation.
    #    Set is legal only strictly under the ceiling. A promise that outlives the machinery
    #    enforcing it is not enforceable, which is why this is the one constraint the file can make.
    p = d.get("PROMOTION_DEADLINE_SECONDS", "").strip()
    ceiling = int(d["PROMOTION_DEADLINE_CEILING_SECONDS"])
    check(ceiling <= route, f"the promotion ceiling ({ceiling}) is at or under the route timeout ({route})")
    if p == "":
        print("OK   PROMOTION_DEADLINE_SECONDS is unset, which is the recorded state: P is a product")
        print("     decision and no value is inferable from the other three")
    else:
        check(int(p) < ceiling, f"P ({p}) is strictly under its ceiling ({ceiling})")

    if failures:
        print(f"\nCHAIN B FAILURES: {len(failures)}")
        return 1
    print("\nPASS: Chain B is strictly ordered and its restatements agree with the applied files")
    return 0


if __name__ == "__main__":
    sys.exit(main())
