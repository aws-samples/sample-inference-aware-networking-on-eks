#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""LT-11 t3 portability check: how far does each arm move between sessions?

Compares the 2026-07-28 single-session run (_t3, four g6.xlarge) against the earlier cells:
  A and B from the 2026-07-26 pass (one g6.12xlarge)
  C and D from the 2026-07-27 round-robin re-run (four g6.xlarge)

This is the direct test of the claim that the cache-blind arms port across sessions and the
inference-aware arms do not. It also re-states each interaction margin, single-topology versus
cross-run, against the worst-case bound that was published before this run.
"""
import glob, json, re, statistics as st

OUT = "loadtest/results/lt11"
# Only these rho have an earlier-session counterpart to compare against, so the portability table is
# restricted to them. rho 0.90 exists only in the t3 run.
RHOS = ("0.8", "1.0", "1.2", "1.6")


def curve_rhos():
    """Every rho present in the t3 set, DISCOVERED from disk.

    Hardcoding a rho list here was the same defect that hid a gate exception in the summarizer: rho 0.90
    was added mid-session and a fixed list silently excluded it, so the crossover was reported bracketed
    between 0.80 and 1.00 when the measured data brackets it between 0.90 and 1.00.
    """
    files = glob.glob(f"{OUT}/cell_A_rho*_t3_r*.json")
    return sorted({re.search(r"_rho([0-9.]+)_", f).group(1) for f in files}, key=float)
# worst-case bounds published in the 2026-07-28 amendment, before this run measured them
PUBLISHED_WORST_CASE = {"1.0": 0.0819, "1.2": 0.0648, "1.6": 0.0966}


def mean(lbl, rho, suffix=""):
    xs = []
    for f in sorted(glob.glob(f"{OUT}/cell_{lbl}_rho{rho}{suffix}_r*.json")):
        with open(f) as fh:
            v = json.load(fh).get("prefix_cache_hit_rate")
        if v is not None:
            xs.append(v)
    return round(st.mean(xs), 4) if xs else None


print("=== per-arm session portability (t3 minus earlier) ===")
print(f"{'arm':>22} | " + " | ".join(f"rho {r:>4}" for r in RHOS) + " |  max |abs shift|")
for lbl, desc, base in (("C", "rr grouped   (07-27)", ""),
                        ("D", "rr interleaved(07-27)", ""),
                        ("A", "epp grouped  (07-26)", ""),
                        ("B", "epp interleav(07-26)", "")):
    shifts = []
    for r in RHOS:
        old, new = mean(lbl, r, base), mean(lbl, r, "_t3")
        shifts.append(None if None in (old, new) else round(new - old, 4))
    mx = max(abs(s) for s in shifts if s is not None)
    print(f"{lbl+' '+desc:>22} | " + " | ".join(f"{s:+8.4f}" for s in shifts) + f" | {mx:.4f}")
print()
print("  Reading: the cache-blind arms (C, D) reproduce; the inference-aware arms (A, B) do not.")
print("  B's shift shrinks as rho rises because both arms approach the floor and have less room to move.")
print()

print("=== interaction margin: |(A-B) - (C-D)| ===")
print(f"{'rho':>5} | {'cross-run':>10} | {'single-topology':>15} | {'published worst case':>20} | verdict")
for r in RHOS:
    A_x, B_x = mean("A", r), mean("B", r)
    C_, D_ = mean("C", r), mean("D", r)
    A_s, B_s = mean("A", r, "_t3"), mean("B", r, "_t3")
    C_s, D_s = mean("C", r, "_t3"), mean("D", r, "_t3")
    if None in (A_x, B_x, C_, D_, A_s, B_s, C_s, D_s):
        print(f"{r:>5} | [Data needed]")
        continue
    d_x = (A_x - B_x) - (C_ - D_)
    d_s = (A_s - B_s) - (C_s - D_s)
    sign_x = "SUBSTITUTES" if d_x < 0 else "COMPLEMENTS"
    sign_s = "SUBSTITUTES" if d_s < 0 else "COMPLEMENTS"
    wc = PUBLISHED_WORST_CASE.get(r)
    wc_s = f"{wc:.4f}" if wc else "n/a (substitutes)"
    note = "sign AGREES" if sign_x == sign_s else "SIGN CHANGED"
    thin = ""
    if wc and abs(d_s) < wc:
        thin = f"  <-- measured margin BELOW the published worst case ({abs(d_s):.4f} < {wc:.4f})"
    print(f"{r:>5} | {abs(d_x):>10.4f} | {abs(d_s):>15.4f} | {wc_s:>20} | {sign_s}, {note}{thin}")
print()

CURVE = curve_rhos()
print(f"=== crossover, from the single-topology curve (rho discovered from disk: {' '.join(CURVE)}) ===")
pts = []
for r in CURVE:
    A_s, B_s, C_s, D_s = (mean(x, r, "_t3") for x in ("A", "B", "C", "D"))
    if None not in (A_s, B_s, C_s, D_s):
        pts.append((float(r), (A_s - B_s) - (C_s - D_s)))
for (r1, d1), (r2, d2) in zip(pts, pts[1:]):
    if (d1 < 0) != (d2 < 0):
        x = r1 + (0 - d1) * (r2 - r1) / (d2 - d1)
        print(f"  sign changes between rho {r1} ({d1:+.4f}) and rho {r2} ({d2:+.4f});"
              f" linear interpolation puts it at rho {x:.2f}")
print("  Measured points only; the interpolation is arithmetic, not a fitted model.")
