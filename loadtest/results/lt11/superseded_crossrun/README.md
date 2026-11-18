# Superseded for the interaction curve: cross-run Endpoint Picker cells

These Endpoint Picker cells are SUPERSEDED for the interaction (substitutes versus complements) curve by
the 2026-07-28 single-session run, whose cells carry the `_t3` label. Use the `_t3` cells for anything that
compares an Endpoint Picker arm against a round-robin arm.

Why they are superseded: they cannot be combined with the round-robin cells without crossing a session
boundary, and the inference-aware arms do not reproduce across sessions. Measured maximum
session-to-session shift: A 0.0789 and B 0.1205, against C 0.0020 and D 0.0190 for the cache-blind arms
(`loadtest/lt11_t3_portability.py`). Any interaction margin built from an A or B cell here plus a C or D
cell from another session carries that shift as unquantified error.

Contents:
1/ `cell_{A,B}_rho{0.8,1.0,1.2,1.6}_r{1,2}.json`, the 2026-07-26 pass on one g6.12xlarge (4x NVIDIA L4 on
   one node).
2/ `cell_{A,B}_rho0.8_t2_r{1,2}.json`, the 2026-07-28 rho 0.80 confirmation on four g6.xlarge, which
   established the portability asymmetry and confirmed the rho 0.80 sign before the full curve was run.

These are COPIES, not moves, and the originals remain in the parent directory on purpose. The earlier
published tables and their dated amendments cite the original label set, and
`lt11_summarize.py` with `SUFFIX=""` must stay able to regenerate them; moving the files would make a
published report unverifiable, which standard 6 forbids. This directory is therefore an explicit
supersession marker rather than a relocation.

Still valid, and NOT superseded, in these files:
1/ The 2026-07-26 A and B cells remain the Endpoint Picker ramp measured on that topology, and the ramp
   verdict was always internally single-topology within each arm.
2/ The t2 cells remain the direct measurement of the portability asymmetry, which is the reason the `_t3`
   run exists.
