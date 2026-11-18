# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
INV7, redesigned: assert Gateway API rule precedence, not file order.

WHY THE OLD CHECK HAD TO GO. Its second half compared `head -1` LINE NUMBERS of two pool names and
called the smaller one "first". That is a proxy for a precedence the file's own comment says is not
list order, so the check asserted something Gateway API does not promise. It passed for a reason
unrelated to the property.

WHAT GATEWAY API ACTUALLY DOES (spec, HTTPRouteRule matching): among rules that match, the winner
is chosen by, in order, the longest PathPrefix match, then the greatest number of HEADER matches,
then the greatest number of query-param matches. List order is the last resort within one
HTTPRoute, not the mechanism.

WHY THIS IS A HARD GATE FOR THE TWO-HEADER DESIGN. Design v6.2 routes on the PAIR (SLA header,
capability header). A fully specified pair rule outranks a partial rule ONLY because header-match
count breaks the tie after path length. So the two-header design's determinism rests on exactly the
property this file asserts, and the design says two-header routing must not ship until it does.

Run:  ./.venv/bin/python tests/check_httproute_precedence.py <httproute.yaml> [more...]
"""
import sys

import yaml


def rule_specificity(rule: dict) -> tuple:
    """(longest path prefix length, header match count, query match count), the spec's order."""
    best_path, best_hdr, best_q = -1, -1, -1
    for m in rule.get("matches", []) or []:
        p = m.get("path") or {}
        plen = len(p.get("value", "")) if p.get("type") in (None, "PathPrefix", "Exact") else 0
        h = len(m.get("headers", []) or [])
        q = len(m.get("queryParams", []) or [])
        if (plen, h, q) > (best_path, best_hdr, best_q):
            best_path, best_hdr, best_q = plen, h, q
    return (best_path, best_hdr, best_q)


# ---------------------------------------------------------------------------------------------
# OVERLAP, added 2026-08-04 when the two-header route set landed.
#
# WHY IT WAS NEEDED. Both checks below compared SPECIFICITY ALONE. With the four-rule two-header set
# that produced a false FAIL on a correct configuration: the interactive+standard rule and the
# batch+standard rule have identical specificity (3, 2, 0), so the tie check flagged them, and the
# batch-outranks-interactive check saw a tie and called it a misroute.
#
# NEITHER CAN HAPPEN. The two rules require DIFFERENT EXACT VALUES for the same header, and a single
# request has one value for `x-gateway-inference-objective`. No request can match both, so list
# order never decides anything between them and there is nothing to be ambiguous about.
#
# A specificity tie only matters when TWO RULES CAN BOTH MATCH ONE REQUEST. That is what overlap
# means, and testing it is what turns "these tuples are equal" into "these rules actually compete".
#
# THE ORIGINAL PROTECTION IS UNCHANGED AND IS RE-PROVEN BY INJECTION. The defect INV7 exists to
# catch is a header-only batch rule losing to an interactive rule with a longer path. Those two DO
# overlap (every /v1 request matches both), so the precedence requirement still binds on exactly the
# pair it was written for.
def _match_overlap(ma: dict, mb: dict) -> bool:
    """Can one request satisfy both of these match blocks?"""
    pa = (ma.get("path") or {}).get("value", "/")
    pb = (mb.get("path") or {}).get("value", "/")
    # PathPrefix: satisfiable together only if one prefix contains the other.
    if not (pa.startswith(pb) or pb.startswith(pa)):
        return False
    # Headers: a conflict is the SAME header name required at DIFFERENT exact values.
    ha = {h.get("name", "").lower(): h.get("value") for h in (ma.get("headers") or [])}
    hb = {h.get("name", "").lower(): h.get("value") for h in (mb.get("headers") or [])}
    for k, v in ha.items():
        if k in hb and hb[k] != v:
            return False
    return True


def rules_overlap(ra: dict, rb: dict) -> bool:
    """True when some request matches both rules, so precedence between them is load-bearing."""
    for ma in ra.get("matches", []) or []:
        for mb in rb.get("matches", []) or []:
            if _match_overlap(ma, mb):
                return True
    return False


def backends(rule: dict) -> list:
    return [b.get("name", "?") for b in (rule.get("backendRefs") or [])]


def check(paths: list) -> int:
    problems, checked = [], 0
    for path in paths:
        for doc in yaml.safe_load_all(open(path).read()):
            if not doc or doc.get("kind") != "HTTPRoute":
                continue
            checked += 1
            name = doc["metadata"]["name"]
            rules = doc.get("spec", {}).get("rules", []) or []
            scored = [(rule_specificity(r), backends(r), i) for i, r in enumerate(rules)]

            # 1. Two rules that CAN BOTH MATCH ONE REQUEST may not tie on specificity while
            #    pointing at different backends: the winner would be decided by list order, which
            #    is a property of the file rather than of the intent. Rules whose match sets are
            #    disjoint are exempt, because nothing ever chooses between them.
            for a_i in range(len(rules)):
                for b_i in range(a_i + 1, len(rules)):
                    if set(scored[a_i][1]) == set(scored[b_i][1]):
                        continue
                    if not rules_overlap(rules[a_i], rules[b_i]):
                        continue
                    if scored[a_i][0] == scored[b_i][0]:
                        problems.append(
                            f"{path} [{name}]: rules {a_i} and {b_i} CAN BOTH MATCH one request, "
                            f"have IDENTICAL specificity {scored[a_i][0]}, and point at different "
                            f"backends {scored[a_i][1]} vs {scored[b_i][1]}. Gateway API would fall "
                            f"back to list order, so this routes correctly only by accident.")

            # 2. Wherever a BATCH rule and an INTERACTIVE rule compete for the same request, the
            #    batch rule must strictly OUTRANK, and by the MECHANISM rather than by position:
            #    equal path length, more header matches. This is the defect that already happened
            #    once, a header-only batch rule losing to an interactive rule with a longer path.
            #    Only OVERLAPPING pairs are compared: the two-header interactive and batch rules
            #    require different values of the SLA header and can never compete.
            for b_i, (b_s, b_be, _) in enumerate(scored):
                if not any("batch" in x for x in b_be):
                    continue
                for i_i, (i_s, i_be, _) in enumerate(scored):
                    if not any("interactive" in x for x in i_be):
                        continue
                    if not rules_overlap(rules[b_i], rules[i_i]):
                        continue
                    if b_s <= i_s:
                        problems.append(
                            f"{path} [{name}]: batch rule {b_i} {b_s} does not outrank the "
                            f"COMPETING interactive rule {i_i} {i_s}. Batch traffic would serve on "
                            f"the interactive tier.")
                    elif b_s[0] != i_s[0]:
                        problems.append(
                            f"{path} [{name}]: batch rule {b_i} outranks competing interactive rule "
                            f"{i_i} by PATH LENGTH ({b_s[0]} vs {i_s[0]}), not by header count. "
                            f"That works, but it is not the mechanism the two-header design depends "
                            f"on; the paths should tie so the header match is what discriminates.")

    if problems:
        print("FAIL  HTTPRoute precedence")
        for p in problems:
            print(f"  {p}")
        return 1
    print(f"OK    HTTPRoute precedence: {checked} route(s). No COMPETING rules tie on specificity "
          f"across different backends, and every batch rule outranks the interactive rules it "
          f"actually competes with, by HEADER COUNT at equal path length.")
    return 0


if __name__ == "__main__":
    raise SystemExit(check(sys.argv[1:] or ["manifests/03-routing/httproute.yaml"]))
