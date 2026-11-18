#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""LT-11 summarizer. Builds the cliff curve from per-cell JSON (2-rep means), reports the
EPP-versus-round-robin gap at each admission mode, the interaction sign at each rho, and the
validity gates.

Label sets (select with the SUFFIX environment variable):
  SUFFIX=""    the original curve: cells A and B from 2026-07-26 (one g6.12xlarge) and cells C and D
               from the 2026-07-27 round-robin re-run (four g6.xlarge). CROSS-RUN for any comparison
               that mixes an Endpoint Picker arm with a round-robin arm.
  SUFFIX=_t3   the 2026-07-28 single-session run: all four arms measured in one session on one pinned
               topology (four g6.xlarge). SINGLE-TOPOLOGY, no cross-run comparison anywhere.

Cells A and B under the original label set are NOT interchangeable with the _t3 ones: the
inference-aware arms do not reproduce across sessions (measured shift A -0.0590, B +0.1454 at
rho 0.80), while the cache-blind arms do (within 0.003). That is why _t3 exists.
"""
import glob, json, os, re, statistics as st

OUT = "loadtest/results/lt11"
SUFFIX = os.environ.get("SUFFIX", "")
RHOS = tuple(os.environ.get("RHOS", "0.8 1.0 1.2 1.6").split())

# Expected per-cell gate values, from the LT-11 design: P=52 groups x R=12 requests = 624 requests,
# effective R 12.0, and N=4 replicas asserted at the start and the end of every cell.
EXPECT_OK, EXPECT_R, EXPECT_N = 624, 12.0, (4, 4)


def all_cell_files(suffix):
    """Every cell file for a label set, DISCOVERED from disk rather than from a hand-maintained rho list.

    This function exists because of a real defect: the gate summary used to iterate the RHOS display list,
    whose default is "0.8 1.0 1.2 1.6". When rho 0.90 was added to the t3 run, its 8 cells were silently
    outside the audited set, the summary printed ALL PASS over 32 of 40 cells, and a genuine 623/624
    exception in cell_C_rho0.9_t3_r1 was reported as "no exceptions". Discovering the files makes it
    impossible for a cell to be present on disk and absent from the audit.
    """
    files = sorted(glob.glob(f"{OUT}/cell_*_rho*{suffix}_r*.json"))
    if not suffix:
        # The unsuffixed set must not absorb the _t2 or _t3 sets.
        files = [f for f in files if not re.search(r"_t\d+_r\d+\.json$", f)]
    return files


def audit_gates(suffix):
    """Machine-checked gate line, regenerated from raw JSON. Reports, never asserts, cleanliness."""
    files = all_cell_files(suffix)
    exceptions, total = [], len(files)
    for f in files:
        with open(f) as fh:
            d = json.load(fh)
        ok, sent = d.get("requests_ok"), d.get("requests_sent")
        R, n = d.get("effective_R"), (d.get("n_start"), d.get("n_end"))
        why = []
        if ok != sent or ok != EXPECT_OK:
            why.append(f"ok={ok}/{sent}")
        if R != EXPECT_R:
            why.append(f"R={R}")
        if n != EXPECT_N:
            why.append(f"N={n}")
        if why:
            exceptions.append((os.path.basename(f)[:-5], " ".join(why)))
    clean = total - len(exceptions)
    detail = "none" if not exceptions else "; ".join(f"{name} {w}" for name, w in exceptions)
    line = (f"gates: {clean} of {total} cells at {EXPECT_OK}/{EXPECT_OK} R {EXPECT_R} N 4->4; "
            f"exceptions: {detail}")
    rhos_found = sorted({re.search(r"_rho([0-9.]+)_", f).group(1) for f in files}, key=float)
    return line, rhos_found, total


def cells(lbl, rho):
    pat = f"{OUT}/cell_{lbl}_rho{rho}{SUFFIX}_r*.json"
    loaded = []
    for f in sorted(glob.glob(pat)):
        with open(f) as fh:
            loaded.append(json.load(fh))
    return loaded


def mean(lbl, rho, key="prefix_cache_hit_rate"):
    xs = [d.get(key) for d in cells(lbl, rho) if d.get(key) is not None]
    return round(st.mean(xs), 4) if xs else None


def spread(lbl, rho, key="prefix_cache_hit_rate"):
    xs = [d.get(key) for d in cells(lbl, rho) if d.get(key) is not None]
    return round(max(xs) - min(xs), 4) if len(xs) > 1 else None


def sign(ab, cd):
    return "SUBSTITUTES" if ab < cd else ("COMPLEMENTS" if ab > cd else "equal")


label = SUFFIX if SUFFIX else "(original, cross-run)"
print(f"=== LT-11 cliff curve, label set {label}: prefix-cache hit rate (2-rep mean) ===")
print(f"{'rho':>5} | {'A epp grp':>9} | {'C rr grp':>8} | {'B epp intl':>10} | {'D rr intl':>9} || "
      f"{'A-C':>7} {'B-D':>7} | {'A-B':>6} {'C-D':>6} {'sign':>12}")
missing = []
for rho in RHOS:
    A, C, B, D = mean("A", rho), mean("C", rho), mean("B", rho), mean("D", rho)
    if None in (A, B, C, D):
        missing.append(rho)
        print(f"{rho:>5} | {str(A):>9} | {str(C):>8} | {str(B):>10} | {str(D):>9} || [Data needed]")
        continue
    print(f"{rho:>5} | {A:>9} | {C:>8} | {B:>10} | {D:>9} || "
          f"{round(A-C,4):>7} {round(B-D,4):>7} | {round(A-B,4):>6} {round(C-D,4):>6} "
          f"{sign(round(A-B,4), round(C-D,4)):>12}")
print(f"{'2.54':>5} | {0.9135:>9} | {0.6643:>8} | {0.0:>10} | {0.0:>9} || "
      f"{0.2492:>7} {0.0:>7} | {0.9135:>6} {0.6643:>6} {'degenerate':>12}   (LT-8 anchor, B=D=0, not re-run)")

print("\n=== crossover location (where the interaction sign changes) ===")
prev = None
for rho in RHOS:
    A, B, C, D = mean("A", rho), mean("B", rho), mean("C", rho), mean("D", rho)
    if None in (A, B, C, D):
        continue
    s = sign(round(A-B, 4), round(C-D, 4))
    delta = round((A-B) - (C-D), 4)
    print(f"  rho {rho}: (A-B)-(C-D) = {delta:+} -> {s}")
    if prev and prev[1] != s:
        print(f"  CROSSOVER between rho {prev[0]} and rho {rho}")
    prev = (rho, s)

print("\n=== rep spread (max minus min) ===")
for lbl in ("A", "B", "C", "D"):
    print(f"  {lbl}: " + "  ".join(f"rho{r}={spread(lbl, r)}" for r in RHOS))

print("\n=== validity gates, per displayed rho (R must be 12.0, ok 624, N 4->4, conc recorded) ===")
for lbl in ("A", "B", "C", "D"):
    for rho in RHOS:
        ds = cells(lbl, rho)
        if not ds:
            print(f"  {lbl} rho={rho}: [Data needed]")
            continue
        Rs = sorted({d.get("effective_R") for d in ds})
        oks = sorted({d.get("requests_ok") for d in ds})
        ns = sorted({(d.get("n_start"), d.get("n_end")) for d in ds})
        conc = [d.get("max_concentration_fraction") for d in ds]
        flag = "" if (Rs == [12.0] and ns == [(4, 4)] and oks == [624]) else "  <-- CHECK"
        print(f"  {lbl} rho={rho}: reps={len(ds)} R={Rs} ok={oks} N={ns} conc={conc}{flag}")

# AUTHORITATIVE gate line: audits every cell discovered on disk for this label set, independent of the
# RHOS display list above. Cite THIS line in reports; never hand-type a gate claim.
GATE_LINE, RHOS_FOUND, N_CELLS = audit_gates(SUFFIX)
print(f"\n{GATE_LINE}")
print(f"  audited {N_CELLS} cells across rho {' '.join(RHOS_FOUND)} (discovered from disk)")
not_shown = [r for r in RHOS_FOUND if r not in RHOS]
if not_shown:
    print(f"  WARNING: rho {' '.join(not_shown)} exist on disk but are NOT in the displayed RHOS list; "
          f"re-run with RHOS=\"{' '.join(RHOS_FOUND)}\" to see them in the tables above.")
if missing:
    print(f"MISSING rho points for this label set: {missing}")

print("\n=== latency within a rho point only (p50/p90 seconds, 2-rep mean) ===")
for rho in RHOS:
    row = [f"{lbl}={mean(lbl, rho, 'e2e_latency_p50_s')}/{mean(lbl, rho, 'e2e_latency_p90_s')}"
           for lbl in ("A", "C", "B", "D")]
    print(f"  rho={rho}: " + "  ".join(row))
