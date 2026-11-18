#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""LT-11 rho 0.80 topology-confirmation analysis.

Compares the Endpoint Picker cells measured on the ORIGINAL topology (2026-07-26, one g6.12xlarge,
files cell_{A,B}_rho0.8_r*.json) against the SAME cells re-measured on the CURRENT topology
(2026-07-28, four g6.xlarge, files cell_{A,B}_rho0.8_t2_r*.json), and recomputes the interaction test
at rho 0.80 entirely within the current topology (using the C and D cells, which were already measured
on it).
"""
import glob, json, statistics as st

OUT = "loadtest/results/lt11"


def m(pattern, key="prefix_cache_hit_rate"):
    xs = []
    for f in sorted(glob.glob(f"{OUT}/{pattern}")):
        with open(f) as fh:
            v = json.load(fh).get(key)
        if v is not None:
            xs.append(v)
    return (round(st.mean(xs), 4), [round(x, 4) for x in xs]) if xs else (None, [])


A_old, A_old_r = m("cell_A_rho0.8_r*.json")
B_old, B_old_r = m("cell_B_rho0.8_r*.json")
A_new, A_new_r = m("cell_A_rho0.8_t2_r*.json")
B_new, B_new_r = m("cell_B_rho0.8_t2_r*.json")
C, C_r = m("cell_C_rho0.8_r*.json")
D, D_r = m("cell_D_rho0.8_r*.json")

cA_old, _ = m("cell_A_rho0.8_r*.json", "max_concentration_fraction")
cA_new, _ = m("cell_A_rho0.8_t2_r*.json", "max_concentration_fraction")
cB_old, _ = m("cell_B_rho0.8_r*.json", "max_concentration_fraction")
cB_new, _ = m("cell_B_rho0.8_t2_r*.json", "max_concentration_fraction")

print("=== rho 0.80 hit rate, Endpoint Picker arms on both topologies ===")
print(f"  A epp grouped     old(1x g6.12xl) {A_old} {A_old_r}   new(4x g6.xl) {A_new} {A_new_r}   shift {round(A_new - A_old, 4):+}")
print(f"  B epp interleaved old(1x g6.12xl) {B_old} {B_old_r}   new(4x g6.xl) {B_new} {B_new_r}   shift {round(B_new - B_old, 4):+}")
print(f"  C rr  grouped     new only        {C} {C_r}")
print(f"  D rr  interleaved new only        {D} {D_r}")
print()
print("  max-concentration (routing distribution across all 52 groups):")
print(f"    A old {cA_old} -> new {cA_new}    B old {cB_old} -> new {cB_new}")
print()

print("=== interaction test at rho 0.80 ===")
ab_cross, cd = round(A_old - B_old, 4), round(C - D, 4)
ab_same = round(A_new - B_new, 4)
def sign(ab, cd):
    return "SUBSTITUTES" if ab < cd else ("COMPLEMENTS" if ab > cd else "equal")
print(f"  CROSS-RUN (as reported): (A-B)={ab_cross} vs (C-D)={cd}  margin {round(abs(ab_cross-cd),4)}  -> {sign(ab_cross, cd)}")
print(f"  SINGLE-TOPOLOGY (new):   (A-B)={ab_same} vs (C-D)={cd}  margin {round(abs(ab_same-cd),4)}  -> {sign(ab_same, cd)}")
print(f"  verdict agreement: {'CONFIRMED' if sign(ab_cross,cd)==sign(ab_same,cd) else 'CHANGED'}")
print()

print("=== implication for the cross-run COMPLEMENTS points (rho 1.0, 1.2, 1.6) ===")
print("  Worst-case correction: apply the measured B shift to (A-B) at each rho and re-check the sign.")
shift_B = B_new - B_old
rows = {"1.0": (0.9087, 0.1336, 0.6646, 0.1168),
        "1.2": (0.9047, 0.0557, 0.6668, 0.0280),
        "1.6": (0.9085, 0.0057, 0.6650, 0.0042)}
for rho, (a, b, c, d) in rows.items():
    ab = a - b
    cdv = c - d
    ab_adj = a - (b + shift_B)
    print(f"  rho {rho}: (A-B) {ab:.4f} -> adj {ab_adj:.4f}   (C-D) {cdv:.4f}   "
          f"reported {sign(ab, cdv)}, adjusted {sign(ab_adj, cdv)}, adj margin {abs(ab_adj-cdv):.4f}")
print()
print("  NOTE: this is a conservative worst case. It assumes the full B shift measured at rho 0.80")
print("  transfers unchanged to higher rho, where both interleaved arms are much closer to the floor")
print("  and therefore have far less room to move (B_old is 0.13, 0.06, 0.006 at rho 1.0, 1.2, 1.6).")
