# Projecting the walkthrough's accelerator bill, with peak concurrency

    Written : 2026-08-07, before the run, per the standing authorization's item 4.1
    Ceiling : 20 GPU instance-hours. Over it is STOP CONDITION B, report instead of starting.

## Why peak concurrency is reported alongside the total

The instance-hour total answers "what does this cost". **Peak concurrent GPU count answers "what
does this need at once", and it is the number most likely to bind**, because `test-tier-isolation`
holds BOTH fleets simultaneously: it saturates the batch tier and asserts the interactive tier's
time to first token does not degrade, which is only meaningful with both tiers live. A projection
that reports only the total can pass the ceiling while requiring a simultaneous fleet the account
cannot provision, and the failure would arrive as a capacity error mid-run.

## The projection, at the reduced reader-size counts

| Phase | GPU instances held | Wall clock | Instance-hours |
|---|---|---|---|
| `make addons`, `observability-crds`, `dry-run` | 0 | ~10 min | 0 |
| `stage-1` (interactive tier up, model load, routing test) | 1 interactive | ~20 min | 0.33 |
| `stage-2` (async front door, CPU) | 1 interactive, held | ~10 min | 0.17 |
| `stage-3` (batch tier up) | 1 interactive + 1 batch | ~20 min | 0.67 |
| **`test-tier-isolation`** | **1 interactive + 1 batch, PEAK** | ~15 min | 0.50 |
| `observability-scalers` | 1 + 1, briefly | ~5 min | 0.17 |
| park and verify zero | 0 | ~5 min | 0 |

    PROJECTED TOTAL          : about 1.8 GPU instance-hours
    PEAK CONCURRENT GPU      : 2   (one interactive, one batch, during test-tier-isolation)
    CEILING                  : 20 instance-hours
    VERDICT                  : UNDER by an order of magnitude. Proceed without further permission.

## What the previous run cost, for calibration

The 2026-08-07 attempt at four interactive replicas provisioned **four** `g6e.xlarge` and reached
only stage-1. At reader-size counts the same prefix needs **one**. The overrun that followed cost
12.47 instance-hours on its own, which is most of this ceiling spent on a single stalled stage.

## The margin is deliberate

These are estimates with generous wall clocks. Even at triple the projection the run sits at about
5.4 instance-hours, comfortably inside 20. **The ceiling binds on peak concurrency long before it
binds on the total**, and peak concurrency here is 2.

## Item 3.2, checked rather than implemented: the premise assertion already exists

The rider asks that `test-tier-isolation` assert its premise so a vacuous pass is impossible.
**It already does, and adding a second assertion would be worse than leaving it alone.**

`tests/validation/test_tier_isolation.py`, in `preflight()`, which runs BEFORE the flood:

    ready = {t: cp.ready_replicas(d) for t, d in TIERS.items()}
    print(f"ready replicas: {ready}")
    empty = [t for t, n in ready.items() if n < 1]
    if empty:
        raise AssertionError(
            "FAIL: no capacity to test. Tiers with zero ready replicas: ..."
            "Tier isolation cannot be demonstrated against a scaled-to-zero cluster")

Its own docstring records why: the test once "reported PASS: interactive isolated from batch flood"
with a serving replica scaled to zero, and the fix was that "zero ready replicas is a hard failure,
not a fast pass". The ready counts are printed, so the premise is visible in the output rather than
only enforced.

**So the rider's requirement is met by existing code.** Verifying that and saying so is the correct
response; adding a duplicate check would put two guards on one property, which is the shape the
commit-gate consolidation removed one batch ago.
