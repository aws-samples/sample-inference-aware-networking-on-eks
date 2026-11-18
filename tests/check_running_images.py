#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
INV19's checker. EVERY RUNNING WORKLOAD THAT USES THIS PROJECT'S IMAGE RUNS THE COMMITTED PIN.

THE INCIDENT, quoted from WRITEUP-FACTPACK E.12 rather than remembered: "All three committed
manifests pinned the async-worker image :v8 ... while the live cluster ran :v10 for both
async-worker and submit-api. The manifests named an image two states behind what actually served."

WHY INV15 DOES NOT COVER THIS. INV15 asserts that the pins AGREE WITH EACH OTHER and that the tag is
commit-traceable. In the v8-versus-v10 incident THE PINS AGREED WITH EACH OTHER: all three said v8.
INV15's first assertion was green throughout the event it cites as its reason for existing, and
`tests/verify_invariants.sh` contains no `kubectl` call, so no INVARIANT has ever read a cluster.

CORRECTED 2026-08-06, and the correction is narrower and more damning than the first telling. An
earlier draft of this paragraph said the cure was never built. IT WAS BUILT.
tests/smoke/check_running_images.sh has existed since 4b361be (2026-08-04) with a committed
injection proof, it reads pod status, it fails closed, and its own header states this insight
correctly. What was never done is the WIRING: the script sat behind a Makefile target that belonged
to NO AGGREGATE, so it ran once at authorship and never again, while the commit title claimed
"INV15's cluster half". A GATE IN NO STANDING SET IS NOT A GATE. Full account in
evidence/correction-inv15-cluster-half-2026-08-06.txt; the script is now in `make test-cluster`.

So this file is not a first cure. It is a SECOND, DIFFERENT instrument, and its justifying incident
is the 2026-08-06 drift: the live relay running sha-1e40093ac21e while every committed manifest
pinned sha-f7a9a0759a93, with every offline gate green and the one cluster gate uninvoked.

THE LIST IS DERIVED, NEVER OWNED, and that is the design decision that matters. This check does not
carry a list of workloads to inspect. It enumerates EVERY workload in the cluster, keeps the ones
running this project's image, and checks those. A workload added tomorrow is inside the check on the
day it is created, because it was never outside it. A list the check owns is a list that rots; this
is the same move as `git ls-files` replacing an exclusion list in tests/check_model_facts.py.

    ./.venv/bin/python tests/check_running_images.py                     against the live cluster
    ./.venv/bin/python tests/check_running_images.py --from-json FILE    against a captured state

THE BOUNDARY OF THE REPLAY, and it is the reason the LIVE run is recurring and is NEVER replaced by
the offline one. A capture freezes an API SHAPE as well as a state. If a Kubernetes version moves a
field, renames a kind, or changes how a pod template nests, the EXTRACTION here breaks against the
live cluster while every fixture replays green, because the fixtures still carry the old shape. The
replay proves the LOGIC; only a live run proves the EXTRACTION still finds anything. So INV19's live
pass stays in the standing gate set and the fixtures are evidence about the logic alone.

THIS CHECK AND tests/smoke/check_running_images.sh ARE NOT REDUNDANT, and the difference is worth
one sentence each so nobody deletes the wrong one. That script reads POD STATUS in one namespace:
what was actually PULLED, which is the better authority for "what is serving right now". This one
reads WORKLOAD SPECS across every namespace and every kind: what is DECLARED, including a workload
parked at zero replicas that will serve the wrong image the moment it scales. Neither subsumes the
other.

The capture form is what makes a CLUSTER-GATED check injection-provable OFFLINE and FOREVER: the
drift state of 2026-08-06 is committed under tests/fixtures/ and replays on any machine, so the
fixture reproduces THE INCIDENT rather than a paraphrase of it.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# This project's own image. Third-party images (vLLM, the EPP, alpine) are out of scope: their
# versions are governed by versions.yaml and by INV-addon-pins, not by a build this repo performs.
OWN_IMAGE = "inference-async-worker"
PIN_RE = re.compile(
    r"[0-9]{12}\.dkr\.ecr\.[a-z0-9-]+\.amazonaws\.com/" + OWN_IMAGE + r":([A-Za-z0-9._-]+)"
)

MANIFEST_GLOBS = ("manifests/**/*.yaml", "async-worker/*.yaml")


def committed_pins() -> set[str]:
    """Every tag this project's manifests pin for its own image."""
    tags: set[str] = set()
    for pattern in MANIFEST_GLOBS:
        for f in ROOT.glob(pattern):
            tags |= set(PIN_RE.findall(f.read_text(errors="ignore")))
    return tags


def cluster_workloads() -> list[dict]:
    """Enumerate EVERY workload in the cluster and flatten it to one row per container.

    Kinds are enumerated explicitly rather than inferred, and `-A` means no namespace can hide a
    workload. CronJob nests its pod template one level deeper, which is the shape a naive reader
    misses and a naive check silently skips.
    """
    # Fixed kubectl argv: list form, no shell.
    out = subprocess.run(  # nosec B603
        ["kubectl", "get", "deploy,statefulset,daemonset,cronjob,job", "-A", "-o", "json"],
        capture_output=True, text=True, check=True,
    ).stdout
    return flatten(json.loads(out))


def finished_job(it: dict) -> bool:
    """A Job with a Complete or Failed condition will never start another pod.

    ITS IMAGE IS HISTORY, NOT A WORKLOAD. Found 2026-11-01: three sweeper Jobs that hit their deadline
    during a VPC CNI restart kept INV19 red after a correct re-pin, because failed-Job history stays
    until three newer failures replace it. The CronJob that spawns them is still checked, and so is
    any Job that is still running, so a parked or in-flight workload cannot hide here.
    """
    if it.get("kind") != "Job":
        return False
    conditions = (it.get("status") or {}).get("conditions") or []
    return any(c.get("type") in ("Complete", "Failed") and c.get("status") == "True" for c in conditions)


def flatten(doc: dict) -> list[dict]:
    rows: list[dict] = []
    for it in doc.get("items", []):
        if finished_job(it):
            continue
        md, spec = it["metadata"], it["spec"]
        tpl = spec.get("template") or spec.get("jobTemplate", {}).get("spec", {}).get("template") or {}
        pod = tpl.get("spec") or {}
        for c in list(pod.get("containers", [])) + list(pod.get("initContainers", [])):
            rows.append({"kind": it["kind"], "namespace": md["namespace"], "name": md["name"],
                         "container": c["name"], "image": c["image"]})
    return sorted(rows, key=lambda r: (r["namespace"], r["kind"], r["name"], r["container"]))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-json", dest="from_json",
                    help="a captured `kubectl get ... -o json` document, or the flattened row list "
                         "this checker itself emits with --capture")
    ap.add_argument("--committed-pin", dest="committed_pin",
                    help="judge a --from-json capture against THIS pin instead of today's manifests. "
                         "A captured state records what the cluster ran on its date; it must be judged "
                         "against the pin committed on that date, or every later pin move turns the "
                         "AGREED control red and the DRIFT control red for the wrong reason (2026-09-22)")
    ap.add_argument("--capture", action="store_true",
                    help="print the flattened row list and exit, for committing as a fixture")
    args = ap.parse_args()

    if args.from_json:
        doc = json.loads(Path(args.from_json).read_text())
        rows = doc if isinstance(doc, list) else flatten(doc)
    else:
        try:
            rows = cluster_workloads()
        except Exception as e:                                     # noqa: BLE001
            # PEND, NOT PASS. A check that cannot reach its subject has not checked it, and
            # reporting green here would be the reassuring-proxy failure this project keeps finding.
            print(f"PEND no cluster reachable ({type(e).__name__}), so running images were not "
                  f"read. This is NOT a pass.")
            return 2

    if args.capture:
        print(json.dumps(rows, indent=2))
        return 0

    pins = {args.committed_pin} if args.committed_pin else committed_pins()
    if len(pins) != 1:
        print(f"FAIL the manifests pin {len(pins)} different tags for {OWN_IMAGE} ({sorted(pins)}), "
              f"so there is no single committed answer to compare the cluster against. INV15 owns "
              f"that property; this check cannot proceed past it.")
        return 1
    want = pins.pop()

    ours = [r for r in rows if OWN_IMAGE in r["image"]]
    if not ours:
        print(f"PEND no running workload uses {OWN_IMAGE}. Nothing to compare; this is NOT a pass.")
        return 2

    bad = [r for r in ours if r["image"].rsplit(":", 1)[-1] != want]
    for r in bad:
        print(f"FAIL {r['kind']} {r['namespace']}/{r['name']} container {r['container']} runs "
              f"{r['image'].rsplit(':', 1)[-1]} while every committed manifest pins {want}. The "
              f"manifests describe an image that is not the one serving, which is E.12 exactly.")
    if bad:
        print(f"FAIL {len(bad)} of {len(ours)} running workloads using {OWN_IMAGE} are off the "
              f"committed pin.")
        return 1

    print(f"OK all {len(ours)} running workloads using {OWN_IMAGE} run the committed pin {want} "
          f"(enumerated from the cluster, so a new workload is inside this check by construction)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
