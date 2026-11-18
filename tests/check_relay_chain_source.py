# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
INV14's consumption check, done STRUCTURALLY instead of by grep adjacency.

WHY IT EXISTS. The first version of this check was two independent greps: "does the file contain
`configMapKeyRef`" and "does it contain `RELAY_UPSTREAM_TIMEOUT_SECONDS`". Both can be true while
the two have nothing to do with each other. That is exactly what happened: the relay gained a SECOND
ConfigMap reference (`gateway-endpoint`, for the discovered gateway address), and from then on the
injected defect (hardcoding the timeout as a literal `value:`) left both greps satisfied. The
efficacy harness caught it, which is the whole reason that harness exists.

WHAT IT ASSERTS. In the relay's pod spec, the environment variable RELAY_UPSTREAM_TIMEOUT_SECONDS is
sourced from `valueFrom.configMapKeyRef` naming the `chain-b-timeouts` ConfigMap and the matching
key, and NOT from a literal `value:`. A hardcoded timeout leaves the committed chain decorative: the
ordering would be true on paper and absent in the data plane, and nothing else here would notice.

Exit 0 when correct, 1 with a named reason otherwise.
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
RELAY_DIR = ROOT / "manifests" / "08-relay"
CONFIGMAP = "chain-b-timeouts"

# Both Chain B constants the relay must SOURCE rather than restate. Generalised 2026-08-04 when
# INV11 repeated INV14's original mistake: it checked for the STRING
# `PROMOTION_DEADLINE_SECONDS` anywhere in the file, and the env var's own `name:` line kept that
# grep satisfied after the `key:` was pointed elsewhere. The fixture caught it, which is the second
# time a text-adjacency check has been caught by the harness rather than by review.
VARS = ("RELAY_UPSTREAM_TIMEOUT_SECONDS", "PROMOTION_DEADLINE_SECONDS")
VAR = VARS[0]   # kept for callers that check only R


def pod_specs(doc: dict):
    """Yield every pod spec in a document, whatever workload kind wraps it."""
    if not isinstance(doc, dict):
        return
    spec = doc.get("spec") or {}
    tmpl = spec.get("template") or {}
    if isinstance(tmpl, dict) and tmpl.get("spec"):
        yield tmpl["spec"]


def _check_var(var: str) -> bool:
    found = []
    for path in sorted(RELAY_DIR.glob("*.yaml")):
        for doc in yaml.safe_load_all(path.read_text()):
            for pspec in pod_specs(doc):
                for c in pspec.get("containers") or []:
                    for env in c.get("env") or []:
                        if isinstance(env, dict) and env.get("name") == var:
                            found.append((path.name, c.get("name"), env))

    if not found:
        print(f"FAIL no container declares {var}; the relay would fall back to its in-code default")
        return False

    ok = True
    for fname, cname, env in found:
        if "value" in env:
            print(
                f"FAIL {fname}:{cname} HARDCODES {var}={env['value']!r} instead of sourcing it from "
                f"the {CONFIGMAP} ConfigMap. The committed chain would be decorative: correct on "
                f"paper, absent in the data plane."
            )
            ok = False
            continue
        ref = ((env.get("valueFrom") or {}).get("configMapKeyRef") or {})
        if ref.get("name") != CONFIGMAP:
            print(f"FAIL {fname}:{cname} sources {var} from ConfigMap {ref.get('name')!r}, expected {CONFIGMAP!r}")
            ok = False
        elif ref.get("key") != var:
            print(f"FAIL {fname}:{cname} sources {var} from key {ref.get('key')!r}, expected {var!r}")
            ok = False
        else:
            print(f"OK   {fname}:{cname} sources {var} from ConfigMap {CONFIGMAP} key {var}")
    return ok


def main() -> int:
    import sys as _s
    wanted = _s.argv[1:] or list(VARS)
    return 0 if all(_check_var(v) for v in wanted) else 1


if __name__ == "__main__":
    sys.exit(main())
