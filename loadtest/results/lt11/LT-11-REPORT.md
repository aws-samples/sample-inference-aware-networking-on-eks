# LT-11: the cliff sweep (prefix-cache hit rate versus overflow ratio)

AMENDMENT 3 (2026-07-28, gate-exception correction; read before Amendment 2). Amendment 2 as originally
written claimed the t3 run had "all gates passed with no exceptions" and, further down, "32 of 32 cells at
624/624 ... R 12.0 everywhere ... no exceptions", and contrasted that with the 2026-07-27 re-run's single
623/624 cell. THAT CLAIM WAS WRONG and is corrected here rather than silently revised (standard 6).

1/ THE TRUTH, regenerated from raw JSON by `SUFFIX=_t3 ./.venv/bin/python loadtest/lt11_summarize.py`:

       gates: 39 of 40 cells at 624/624 R 12.0 N 4->4; exceptions: cell_C_rho0.9_t3_r1 ok=623/624 R=11.98

   So the t3 run is 40 cells with ONE exception, not 32 cells with none. The exempt cell is
   `cell_C_rho0.9_t3_r1`: 623 of 624 requests completed, one client-side short request, effective R 11.98
   rather than 12.0.
2/ THE FINDING IS UNAFFECTED, for the same reasons recorded for the 2026-07-27 `cell_D_rho1.6_r2`
   exception. Rep 2 of the same cell is 624/624 at 0.663, the two-rep mean 0.6635 stands, and C is the
   flat cache-blind arm whose value is 0.6611 to 0.6662 across the whole sweep, so a single missing
   request cannot move it. The rho 0.90 SUBSTITUTES verdict and every number in the curve stand unchanged.
   No hit rate, margin, sign, or crossover in this report is altered by this correction.
3/ CELL COUNT RECONCILED. The run is 40 cells: four arms times five rho (0.80, 0.90, 1.00, 1.20, 1.60)
   times two reps. Amendment 2's "32 cells" plus "8 further cells" split described the two launches
   (the initial four rho, then the added rho 0.90) and obscured which set the exempt cell fell in. It fell
   in the rho 0.90 set.
4/ ROOT CAUSE, because it is the same class of defect this experiment exists to hunt. The gate claim was
   hand-typed from a summary table that could not see the cell. `lt11_summarize.py` iterated its RHOS
   display list, whose default is "0.8 1.0 1.2 1.6", so when rho 0.90 was added its 8 cells sat outside
   the audited set and the per-rho table printed clean over 32 of 40 cells. A data-versus-prose divergence
   was therefore structurally possible, exactly as the round-robin route fallthrough was structurally
   possible before a preflight gate existed.
5/ SYSTEMIC FIX, so a typed gate claim cannot diverge from the data again. `lt11_summarize.py` now carries
   an `audit_gates` function that DISCOVERS every cell file for a label set from disk, independent of the
   RHOS display list, checks requests_ok against requests_sent, effective R against 12.0 and N against
   4->4, and prints the authoritative one-line gate summary quoted in item 1. It also emits a WARNING when
   cells exist on disk at a rho that the displayed tables omit, which is the precise condition that hid
   this exception. Verified to reproduce `cell_C_rho0.9_t3_r1` for the `_t3` set, `cell_D_rho1.6_r2` for
   the unsuffixed 2026-07-27 set, and "exceptions: none" for the clean `_t2` set. Reports must cite that
   regenerated line; do not hand-type a gate claim.

AMENDMENT 2 (2026-07-28, t3 single-session curve): the cross-run caveat is now CLOSED AT EVERY rho. All
four arms were re-measured in ONE session on ONE pinned topology (four g6.xlarge, one NVIDIA L4 per node,
`node.kubernetes.io/instance-type` pinned so Karpenter could not substitute), 40 cells across five rho,
two reps each. Gate status is the machine-generated line in Amendment 3 item 1 (39 of 40 clean, one named
exception), NOT the "no exceptions" wording originally used here. Every verdict below is now MEASURED
single-topology rather than bounded.
Amendment 1 and the original Verdict 4 text are left unedited (standard 6). Script:
`loadtest/run_lt11_t3_single_session.sh`. Tables: `SUFFIX=_t3 ./.venv/bin/python loadtest/lt11_summarize.py`.
Portability analysis: `loadtest/lt11_t3_portability.py`. Cells: `cell_{A,B,C,D}_rho{...}_t3_r{1,2}.json`.

1/ THE SINGLE-TOPOLOGY CURVE (2-rep means, prefix-cache hit rate).

| rho | A epp grouped | C rr grouped | B epp interleaved | D rr interleaved | (A-B) | (C-D) | (A-B)-(C-D) | sign |
|---|---|---|---|---|---|---|---|---|
| 0.80 | 0.8658 | 0.6611 | 0.6245 | 0.2341 | 0.2413 | 0.4270 | -0.1857 | SUBSTITUTES |
| 0.90 | 0.8284 | 0.6635 | 0.4390 | 0.1825 | 0.3894 | 0.4810 | -0.0916 | SUBSTITUTES |
| 1.00 | 0.8298 | 0.6641 | 0.2541 | 0.1358 | 0.5757 | 0.5283 | +0.0474 | COMPLEMENTS |
| 1.20 | 0.8684 | 0.6648 | 0.0702 | 0.0273 | 0.7982 | 0.6375 | +0.1607 | COMPLEMENTS |
| 1.60 | 0.8327 | 0.6662 | 0.0063 | 0.0042 | 0.8264 | 0.6620 | +0.1644 | COMPLEMENTS |
| 2.54 | 0.9135 | 0.6643 | 0.000 | 0.000 | n/a | n/a | n/a | degenerate (LT-8 anchor, not re-run) |

rho 0.90 was added as the optional crossover-locating point and is part of the same session and topology
(8 further cells, bringing the run to 40). Corrected 2026-07-28: the rho 0.90 set contains the run's one
gate exception, `cell_C_rho0.9_t3_r1` at 623/624 with R 11.98, which does not affect the value shown
because rep 2 is 624/624 at 0.663. See Amendment 3.

2/ ALL SIGNS AGREE WITH THE CROSS-RUN REPORT. Substitutes at rho 0.80 and 0.90, complements at 1.00, 1.20
   and 1.60. The flip is therefore real and not an artifact of stitching two sessions together, which was
   the open question. THE CROSSOVER IS LOCATED between rho 0.90 and 1.00, narrowed from the previous
   "between 0.80 and 1.00": the interaction term (A-B)-(C-D) runs -0.1857, -0.0916, +0.0474, +0.1607,
   +0.1644 across rho 0.80, 0.90, 1.00, 1.20, 1.60, a monotone progression that changes sign once. Linear
   interpolation between the two bracketing points puts the crossover at rho 0.97 (arithmetic on measured
   points, not a fitted model). That is essentially AT the cache boundary, where the per-replica working
   set equals per-replica cache capacity, which is the natural place for the relationship to inverit and
   makes the result easy to state: below the boundary the two mechanisms substitute, above it they
   complement. It also explains item 3: the rho 1.00 margin is thin because rho 1.00 sits only 0.03 above
   the crossover.
3/ ONE MARGIN CAME BACK THINNER THAN THE PUBLISHED WORST CASE, and per the run's own rule that is the
   measurement, not a problem to tune away. Margins |(A-B) - (C-D)|, cross-run versus single-topology
   versus the worst-case bound published in Amendment 1:
   - rho 0.80: 0.0519 -> 0.1857. Substitutes is stronger than the cross-run estimate.
   - rho 1.00: 0.2273 -> 0.0474, against a published worst case of 0.0819. BELOW the bound. The sign
     holds, but this verdict is thin, and it is thin for a coherent reason: rho 1.00 sits just above the
     interpolated crossover at 0.96, so a small margin there is exactly what the curve predicts. Note
     also that B's within-session rep spread at rho 1.00 is 0.0458, comparable to the margin itself, so
     treat rho 1.00 as "complements, near the crossover" rather than as a confident separation.
   - rho 1.20: 0.2102 -> 0.1607, against a published worst case of 0.0648. Above the bound.
   - rho 1.60: 0.2420 -> 0.1644, against a published worst case of 0.0966. Above the bound.
   So the conservative correction in Amendment 1 was too pessimistic at rho 1.20 and 1.60 and not
   pessimistic enough at rho 1.00.
4/ THE PORTABILITY ASYMMETRY IS NOW MEASURED ACROSS THE WHOLE CURVE, not inferred from one point. Maximum
   absolute session-to-session shift per arm: C (round-robin grouped) 0.0020, D (round-robin interleaved)
   0.0190, A (Endpoint Picker grouped) 0.0789, B (Endpoint Picker interleaved) 0.1205. The cache-blind
   arms reproduce, the inference-aware arms do not, by roughly an order of magnitude. B's shift also
   shrinks as rho rises (+0.1080, +0.1205, +0.0145, +0.0006 at rho 0.80, 1.00, 1.20, 1.60) because both
   arms approach the floor and have less room to move, which is why Amendment 1's flat worst-case
   correction over-penalized the high-rho points.
5/ WHAT THIS CHANGES FOR THE WRITE-UP. Every interaction verdict may now be quoted as measured on one
   topology. Do NOT quote the rho 1.00 margin as a confident separation; state it as complements close to
   the crossover. The rho 2.54 anchor stays degenerate and unmeasurable by construction.
6/ GATES (corrected 2026-07-28 by Amendment 3; cite the regenerated line, not a typed claim). The
   authoritative summary from `SUFFIX=_t3 ./.venv/bin/python loadtest/lt11_summarize.py` is:

       gates: 39 of 40 cells at 624/624 R 12.0 N 4->4; exceptions: cell_C_rho0.9_t3_r1 ok=623/624 R=11.98

   So 39 of 40 cells are clean, with one named exception (one client-side short request; rep 2 of that
   cell is 624/624 at 0.663, the mean 0.6635 stands, and the arm is flat cache-blind, so the finding is
   unaffected). N was asserted 4 at start and end of every cell. In addition: C re-derived at 30736 before
   any cell ran, exactly four g6.xlarge nodes asserted, and both preflight gates passed, round-robin
   spread (hit 0.6611 at concentration 0.271) and Endpoint Picker separation (hit 0.8248, separation from
   round-robin 0.1637 above the 0.15 threshold). The Endpoint Picker separation gate is new in this run
   and is the guard that proves the two arms reach different backends, which the round-robin gate alone
   cannot establish.
7/ ONE OBSERVATION WORTH RECORDING. Cell A's within-session rep spread reached 0.0872 at rho 1.20 and
   0.0800 at rho 0.80, with per-rep max concentration swinging between 0.423 and 1.000. So the Endpoint
   Picker's group-to-replica assignment is unstable even between two reps in the same session, not only
   between sessions. That is the same mechanism identified in Amendment 1, observed at finer grain, and
   it is the dominant noise source in the grouped Endpoint Picker arm.

AMENDMENT (2026-07-28, rho 0.80 topology confirmation): the cross-run caveat in Verdict 4 has been
closed at rho 0.80 by re-measuring the Endpoint Picker arms on the CURRENT topology. Verdict 4's original
text is left unedited below (standard 6); read it together with this amendment. Script:
`loadtest/run_lt11_topology_confirm.sh`. Analysis: `loadtest/lt11_confirm_analyze.py`. Cells:
`cell_{A,B}_rho0.8_t2_r{1,2}.json` (t2 = second topology; the original A and B cells are untouched).

1/ THE FRAGILE VERDICT IS CONFIRMED, AND MORE STRONGLY. Measured on one topology (4x g6.xlarge, the same
   topology as C and D): A 0.8447, B 0.6619, so (A-B) = 0.1828 against (C-D) = 0.4391. SUBSTITUTES at
   rho 0.80, margin 0.2563 rather than the cross-run 0.0519. The sign is unchanged and it is no longer
   marginal, so the interaction result at rho 0.80 can now be quoted.
2/ THE RUN-TO-RUN SHIFT IS LARGE, AND THIS CORRECTS AN EARLIER UNDERSTATEMENT. The report previously
   bounded the topology effect at about 0.003, which was the bound for the GROUPED ROUND-ROBIN arm only
   (new C versus LT-8 C) and does not transfer to the Endpoint Picker arms. Measured on the Endpoint
   Picker at rho 0.80: A shifted -0.0590 (0.9037 to 0.8447) and B shifted +0.1454 (0.5165 to 0.6619).
   Two distinct causes, separable in the data:
   - A: the Endpoint Picker's group-to-replica assignment changed between sessions. Max concentration
     across the 52 groups went from 0.298 (near-uniform spread of groups over 4 replicas) to 0.689 (most
     groups piled onto one replica), which lowers the grouped hit rate because one replica's cache holds
     fewer of the 52 prefixes. This is a routing-distribution difference, not a cache-size difference.
   - B: routing was unchanged (concentration 0.2605 versus 0.2625, both effectively uniform), so the
     +0.1454 is a genuine timing effect. On four separate g6.xlarge nodes each replica owns its host CPU
     and PCIe rather than sharing one, which changes request pacing, temporal overlap and therefore reuse
     distance in the thrash regime.
3/ THE COMPLEMENTS VERDICTS SURVIVE, WITH THINNER MARGINS THAN ORIGINALLY CLAIMED. Applying the measured
   B shift to (A-B) as a conservative worst case: rho 1.0 margin 0.2273 becomes 0.0819; rho 1.2 0.2102
   becomes 0.0648; rho 1.6 0.2420 becomes 0.0966. The sign is COMPLEMENTS in every case, so the verdicts
   hold, but they are not the comfortable 0.21 to 0.24 the report implied. The correction is conservative:
   it assumes the full rho 0.80 shift transfers unchanged to higher rho, where B_old is already 0.13,
   0.06 and 0.006 and therefore has far less room to move.
4/ THE REUSABLE LESSON, and it is worth carrying into the write-up. The cache-blind arm is highly
   reproducible across sessions and topologies (C to within 0.003); the inference-aware arm is not (up to
   0.145 on the interleaved cell). That asymmetry is expected, because the Endpoint Picker makes
   state-dependent placement decisions while round-robin is stateless. CONSEQUENCE: any
   EPP-versus-round-robin comparison must be measured with both arms in the SAME session, which is
   exactly the matched, gateway-equalized discipline the publication runs already use. Cross-session
   comparison of an Endpoint Picker arm is not safe at this operating point.
5/ SCOPE OF THIS CONFIRMATION. rho 0.80 only, 4 cells, 2 reps, all gates passed (C re-derived at 30736;
   624/624 requests in all four cells; effective R 12.0; N 4 asserted; Endpoint Picker preflight
   confirmed co-location at hit 0.8452, concentration 0.875). The rho 1.0, 1.2 and 1.6 interaction points
   remain cross-run and are now bounded by item 3 rather than measured directly.

STATUS: RUN COMPLETE (round-robin arms re-run 2026-07-27, see the repair note below). Cluster
inference-aware-eks. The PRE-REGISTRATION below was committed before the run and is preserved
unedited (standard 3). Measured values are filled in from the per-cell JSON in this directory.

## REPAIR NOTE (2026-07-27): the first pass measured the Endpoint Picker in all four cells

The first LT-11 pass (2026-07-26) ran with the round-robin route and Service ABSENT from the cluster.
Consequence, confirmed on the live cluster: the `x-pub-arm: rr` header had no matching route, so the
round-robin requests fell through to the canonical batch rule (InferencePool plus Endpoint Picker),
exactly as `loadtest/lt8-batch-route.yaml` documents in its own header comment. Cells C and D therefore
measured the Endpoint Picker, not round-robin, and the run silently contained no round-robin data.

1/ Evidence of the fault. The live cluster had no `lt8-batch-rr-route` HTTPRoute and no
   `vllm-llama3-batch-rr` Service (both are labelled MEASUREMENT ONLY, delete at teardown, and were
   removed during the publication teardown between LT-8 and LT-11; neither `run_lt11.sh` nor the LT-8
   scripts re-applied them). The first-pass C cells read 0.902 to 0.908 with uneven per-replica
   concentration 0.269 to 0.356, statistically indistinguishable from the A (Endpoint Picker) cells and
   irreconcilable with LT-8's C = 0.6643 at uniform 0.25.
2/ How it was caught. The first-pass C values contradicted the LT-8 anchor on the same cell definition.
   The scaffold's own curve print places the reused LT-8 anchor (C = 0.6643) on the same line as the
   swept C values (about 0.90), so filling in this report would have exposed the discontinuity. The
   report was never filled in, so nobody saw it.
3/ Repair. The Service and route were re-applied (unchanged manifests), and a PREFLIGHT GATE was added
   that must pass before any cell runs: a small round-robin grouped probe must SPREAD across replicas
   (max concentration below 0.5) and land near 1 - N/R = 0.667. Measured preflight: hit 0.6618, max
   concentration 0.271, 48/48 requests ok. PASS, so the round-robin arm provably reached the
   cache-blind Service rather than the Endpoint Picker.
4/ What was re-run. Only the round-robin cells (C grouped, D interleaved), 4 rho points, 2 reps, 16
   runs, driver and parameters unchanged. Cells A and B (Endpoint Picker) from the first pass are VALID
   and are reused. The 16 superseded C and D files are preserved unmodified in
   `superseded_rr_fallthrough/` and must not be used.
5/ Script hardening. `loadtest/run_lt11_rr_rerun.sh` carries the apply plus preflight gate.
   `run_lt11.sh` now applies both objects idempotently and runs the same gate, so this cannot recur.

TOPOLOGY DEVIATION (stated, not smoothed). The re-run landed on 4x g6.xlarge (one NVIDIA L4 per node,
one replica per node) because Karpenter provisioned per-pod; the first pass and LT-8 used one
g6.12xlarge (4x L4 on one node). Per-replica GPU is identical (one L4, 24 GB) and C was re-derived on
the new topology and equals 30736 (num_gpu_blocks 1921 x 16), so the per-replica cache measurement is
comparable. The empirical bound on the topology effect: C measured on the new topology (0.6625 to
0.6668) matches LT-8's C on the old topology (0.6643) to within 0.003. Consequence to respect: the
grouped comparison A versus C and the interleaved comparison B versus D are CROSS-RUN (A and B from
2026-07-26 on one node, C and D from 2026-07-27 on four nodes). See the caveat under Verdict 4.

## Purpose

LT-8 measured the interleaved cells (B, D) at exactly 0.0000 at an overflow ratio rho = 2.54. That one
point makes the interaction test degenerate (with B = D = 0, `(A-B) > (C-D)` reduces to `A > C`) and it
leaves the most interesting quantity in the project a single point. LT-11 converts that point into a
curve by sweeping rho, so the blog can report where cache-aware routing starts to matter, not just how
much it wins once it does. Mapping the cliff is the primary goal; the complements-versus-substitutes
question is a secondary output that becomes answerable only if some interleaved cell is non-zero.

## The variable (verified against the repository)

rho = (P/N) * L / C. With P=52, N=4, C=30736 (num_gpu_blocks 1921 x 16, from LT-8/LT-10), L=5999
reproduces rho = 2.5373 ~ 2.54 (the LT-8 point), which confirms the formula before use. The sweep holds
P=52, R=12, total_requests=624, N=4, C fixed, and moves L alone, so rho moves alone and every model
prediction stays constant. L = rho * C * N / P = rho * 2364.3. PREFIX_REPS = round((L - 7) / 28) from
LT-8's measured tokenization (7-token preamble plus 28 tokens per repetition); the actual L and rho are
verified live per point (a salt on the prefix shifts the preamble by a few tokens).

| rho target | L target | PREFIX_REPS | L actual (7+28*reps) | rho actual (pre-run) | source |
|---|---|---|---|---|---|
| 0.8 | 1891 | 67 | 1883 | 0.796 | new |
| 1.0 | 2364 | 84 | 2359 | 0.998 | new (the boundary) |
| 1.2 | 2837 | 101 | 2835 | 1.199 | new |
| 1.6 | 3783 | 135 | 3787 | 1.602 | new |
| 2.54 | 5999 | 214 | 5999 | 2.537 | LT-8, reused (not re-run) |

## PRE-REGISTRATION (committed before running; do not edit afterwards)

1/ Grouped cells (A EPP, C round-robin) are predicted FLAT across the whole sweep, because grouping puts
   one prefix in flight at a time and a single prefix of length L fits comfortably in C at every rho
   here. Predicted: A = 1 - 1/R = 0.917 and C = 1 - N/R = 0.667 at every rho. Anchored at rho 2.54 by
   LT-8 (0.9135, 0.6643).
2/ Interleaved cells (B EPP, D round-robin) are the open question: a CLIFF that is near zero at rho 2.54
   (anchored by LT-8: B = D = 0.000) and rises as rho falls toward and below 1. The SHAPE of that rise
   is pre-registered as two equally interesting outcomes, and the report commits to NEITHER:
   - STEP: hit rate collapses to near zero as soon as rho passes 1; rho = 1 is a hard operational
     boundary. This confirms textbook LRU cyclic thrashing (reuse distance exceeds the cache) in a
     production-shaped stack.
   - RAMP: there is a usable degraded region above rho = 1, softened by partial block reuse, concurrent
     in-flight requests, and non-uniform arrival interleaving; the post can quote how much headroom a
     reader has.
   NOT named Belady's anomaly (that is a FIFO more-frames-more-faults phenomenon, not this).
3/ Interaction (complements vs substitutes) is SECONDARY and evaluable only at a rho where both B and D
   are non-zero. No direction is pre-committed anywhere. If the sign differs across rho, that locates
   where the relationship inverts and is the strongest possible result; it is a bonus, not the goal.

## Operating-point / control assertions (must equal LT-8)

1/ Same driver lt8_direct.py (direct at the gateway; NOT the queue path, which is LT-10): CONFIRMED,
   `loadtest/lt8_direct.py` unchanged, invoked by `run_lt11_rr_rerun.sh`.
2/ Batch tier one g6.12xlarge, N=4 asserted from the live count at start and end of every cell:
   PARTIAL. N=4 asserted at start and end of all 16 cells (n_start = n_end = 4 in every JSON). The
   instance shape DEVIATED to 4x g6.xlarge; see the topology deviation note above.
3/ KEDA suspended on the batch ScaledObject: CONFIRMED, paused-replicas=4 for the whole session.
4/ Cache reset between cells via unique salted prefixes: CONFIRMED, salt `lt11-<cell>-rho<rho>-r<rep>`
   unique per cell and rep.
5/ P=52, R=12, total=624, max_tokens=64, model Llama-3.1-8B, temp 0 identical to LT-8: CONFIRMED for
   the re-run cells (P=52, R=12, 624 requests, max_tokens 64, conc 16, temperature 0).
6/ C re-derived on the pinned instance and equals 30736 (else STOP): CONFIRMED, num_gpu_blocks=1921,
   C=30736, gate asserted in-script before any cell ran.

Measured rho per point (live tokenization, from `rho_map.txt`): L = 1891, 2367, 2843, 3795 giving
rho = 0.7998, 1.0011, 1.2025, 1.6051.

## The curve (measured)

Hit rate versus rho, per arm, 2 clean reps each (mean). LT-8's 2.54 point reused.

| rho | B EPP interleaved | D RR interleaved | A EPP grouped | C RR grouped |
|---|---|---|---|---|
| 0.80 | 0.5165 | 0.2234 | 0.9037 | 0.6625 |
| 1.00 | 0.1336 | 0.1168 | 0.9087 | 0.6646 |
| 1.20 | 0.0557 | 0.0280 | 0.9047 | 0.6668 |
| 1.60 | 0.0057 | 0.0042 | 0.9085 | 0.6650 |
| 2.54 | 0.000 (LT-8) | 0.000 (LT-8) | 0.9135 (LT-8) | 0.6643 (LT-8) |

Derived quantities (same rows):

| rho | A-C (routing gain, grouped) | B-D (routing gain, interleaved) | A-B | C-D | interaction sign |
|---|---|---|---|---|---|
| 0.80 | 0.2412 | 0.2931 | 0.3872 | 0.4391 | SUBSTITUTES |
| 1.00 | 0.2441 | 0.0168 | 0.7751 | 0.5478 | COMPLEMENTS |
| 1.20 | 0.2379 | 0.0277 | 0.8490 | 0.6388 | COMPLEMENTS |
| 1.60 | 0.2435 | 0.0015 | 0.9028 | 0.6608 | COMPLEMENTS |
| 2.54 | 0.2492 | 0.0000 | 0.9135 | 0.6643 | COMPLEMENTS (degenerate, B=D=0) |

Rep spread (max minus min, 2 reps): A at most 0.0063, C at most 0.0017, B at most 0.0292, D at most
0.0298. The grouped arms are extremely reproducible; the interleaved arms are noisier, as expected for a
timing-sensitive thrash regime, and still far tighter than the effects being reported.

## Verdicts

1/ Step versus ramp (one sentence): RAMP. The interleaved arms decline monotonically and continuously
   with no discontinuity at rho = 1 (Endpoint Picker 0.5165, 0.1336, 0.0557, 0.0057, 0.000 across
   rho 0.80, 1.00, 1.20, 1.60, 2.54), so there is a usable degraded region below the boundary rather
   than a cliff edge at it. Operationally: most of the collapse happens between rho 0.8 and 1.2, the
   band above 1.2 is effectively dead (below 0.06), and there is real headroom only below about 1.0.
2/ Did the grouped arms hold flat? YES, both, and this is the strongest confirmation in the run. C
   (round-robin grouped) sat at 0.6625 to 0.6668 across the whole sweep against a predicted 1 - N/R =
   0.667, a maximum deviation of 0.0045, and it reconciles with LT-8's independent 0.6643. A (Endpoint
   Picker grouped) sat at 0.9037 to 0.9135 against a predicted 1 - 1/R = 0.917, running about 0.010 to
   0.013 low but flat within 0.010 across five rho points. Grouping is immune to the cliff: the
   admission pattern, not the overflow ratio, decides whether the cache works.
3/ Are the interleaved cells non-zero anywhere (does the interaction become answerable)? YES at all four
   swept points (B 0.0057 to 0.5165, D 0.0042 to 0.2234), so unlike LT-8 the interaction test is
   non-degenerate here. LT-8's rho 2.54 is now identified as the collapse anchor of this curve, the one
   point where both arms are floored and the test is meaningless.
4/ Interaction sign at each rho where both non-zero (no pre-committed direction): THE SIGN FLIPS.
   SUBSTITUTES at rho 0.80 (A-B 0.3872 below C-D 0.4391) and COMPLEMENTS at rho 1.00, 1.20 and 1.60
   (0.7751 above 0.5478; 0.8490 above 0.6388; 0.9028 above 0.6608). The crossover lies between rho 0.80
   and 1.00, that is, at the cache boundary. Reading: below the boundary the cache is large enough that
   grouping alone recovers most of the benefit and round-robin gains MORE from grouping than the
   Endpoint Picker does (they substitute); at and above the boundary only the Endpoint Picker plus
   grouping keeps the working set resident, so the two reinforce each other (they complement).
   CAVEAT, load-bearing: this comparison is CROSS-RUN (A and B from 2026-07-26 on one g6.12xlarge, C and
   D from 2026-07-27 on four g6.xlarge). The topology effect is empirically bounded at about 0.003 for
   the grouped round-robin arm (C new versus LT-8 C old), but no equivalent old-topology D exists to
   bound it for the interleaved arms, and the interleaved arms are the timing-sensitive ones. The sign
   flip should therefore be treated as INDICATIVE, not settled, until A and B at rho 0.80 and 1.00 are
   re-run on a single topology alongside C and D. That is a bounded follow-up: 8 cells, about 45 minutes
   of GPU plus about 20 minutes of provisioning, no new infrastructure. Recommended before the sign flip
   is quoted in the blog.

## Validity gates

1/ requests_ok == requests_sent every cell: 15 of 16 re-run cells PASS at 624/624. ONE EXCEPTION,
   reported not smoothed: cell D rho1.6 rep2 recorded 623/624 (one request failed) giving effective
   R = 11.98 rather than 12.0. Both D rho1.6 reps returned an identical hit rate of 0.0042, so the
   finding at that point is unaffected. All 8 reused A and B cells were 624/624 (confirmed previously by
   the LT-8 and LT-11 stall audit standard).
2/ N stable at 4 throughout: PASS, n_start = n_end = 4 in all 16 re-run cells and all 8 reused cells.
3/ Effective R measured at 12 at every point (confirms the sweep moved ONE variable): PASS at 12.0 in
   every cell except D rho1.6 rep2 at 11.98 (see gate 1). R held constant across the sweep, so rho moved
   alone as designed.
4/ Measured rho per point (from actual L and derived C, not the target): 0.7998, 1.0011, 1.2025, 1.6051
   (L = 1891, 2367, 2843, 3795; C = 30736). Every point within 0.006 of its target.
5/ Round-robin arm provably cache-blind (new gate, added by the repair): PASS, preflight hit 0.6618 at
   max concentration 0.271, and every C and D cell shows uniform per-replica concentration 0.253 to
   0.271 (a co-locating Endpoint Picker arm reads 0.27 to 0.37 with a much higher hit rate).

## Latency (within a rho point only)

Changing L changes prefill cost, so absolute latency is NOT comparable across rho points. Hit rate is.
End-to-end p50/p90 seconds, 2-rep mean. Note the same cross-run caveat as Verdict 4: A and B are from
the 2026-07-26 topology, C and D from 2026-07-27.

| rho | A EPP grouped | C RR grouped | B EPP interleaved | D RR interleaved |
|---|---|---|---|---|
| 0.80 | 5.21 / 5.33 | 5.00 / 5.08 | 5.23 / 6.48 | 6.29 / 7.35 |
| 1.00 | 5.46 / 5.51 | 5.16 / 5.23 | 7.30 / 9.41 | 7.28 / 8.27 |
| 1.20 | 5.75 / 5.83 | 5.47 / 5.57 | 8.21 / 10.08 | 8.14 / 9.83 |
| 1.60 | 5.97 / 6.31 | 5.85 / 6.01 | 9.30 / 12.00 | 9.50 / 11.63 |

The grouped arms stay near 5 to 6 seconds at every rho while the interleaved arms climb from about 5 to
6 seconds up to 9 to 12 seconds, so the hit-rate collapse shows up as latency, and admission grouping
protects latency as well as hit rate.

## What this run establishes, and what it does not

Establishes:
1/ The routing benefit under grouped admission is real, large and rho-INVARIANT: A - C is 0.2379 to
   0.2492 at every one of five overflow ratios, independently reproducing LT-8's 0.2492.
2/ The cliff is a ramp, with the collapse concentrated between rho 0.8 and 1.2.
3/ Grouped admission is immune to the cliff; interleaved admission is destroyed by it.
4/ LT-8's rho 2.54 is the floored anchor of that ramp, which is why its interaction test was degenerate.

Does not establish:
1/ The interaction sign flip, at publication strength. It is measured, but cross-run (Verdict 4 caveat).
2/ Anything about the queue path. LT-11 is direct-driven by design; LT-10 owns the shipped-path claim.
3/ Anything cross-accelerator. This is NVIDIA L4 only.

## Files

1/ Per-cell JSON: `cell_{A,B}_rho{0.8,1.0,1.2,1.6}_r{1,2}.json` (Endpoint Picker, 2026-07-26, reused)
   and `cell_{C,D}_rho{0.8,1.0,1.2,1.6}_r{1,2}.json` (round-robin, 2026-07-27 re-run).
2/ Superseded, do not use: `superseded_rr_fallthrough/` (the 16 original C and D files that measured the
   Endpoint Picker by route fallthrough).
3/ Repair gate evidence: `preflight_rr.json`. Live rho derivation: `rho_map.txt`. Run logs: `run_log.txt`
   (first pass) and the re-run terminal transcript summarized in this report.
4/ Re-run script: `loadtest/run_lt11_rr_rerun.sh`. Summarizer: `loadtest/lt11_summarize.py` (regenerates
   every table above from the JSON).
