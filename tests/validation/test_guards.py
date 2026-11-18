# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Offline proof for the classifier guards (design v6.1 section 7).

No cluster, no accelerator, no credentials. Section 7 requires the kill switch and the maximum
upgrade rate to exist BEFORE any classifier does, so these are proven before there is anything to
guard, which is the point.

The two properties the design states, tested as properties rather than as happy paths:

  1. The kill switch flips UNDER LOAD without dropping requests. Withdrawal must be possible
     exactly when the system is in trouble, so it is exercised with concurrent traffic in flight
     and the count is asserted to be conserved.
  2. Each guard DEGRADES TO THE FLOOR, NEVER REJECTS. A guard that errors converts capacity
     protection into an availability incident, which is worse than the problem. Asserted by
     checking that every possible return value is a tier, across a saturating load.

Run:  ./.venv/bin/python tests/validation/test_guards.py
"""
import os
import sys
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "async-worker"))

from guards import Guards, KillSwitch, UpgradeRateLimiter  # noqa: E402

FLOOR, UP = "batch-standard", "interactive-standard"


def test_kill_switch_flips_under_load_without_dropping() -> None:
    """Property 1. Traffic is in flight, the switch is thrown mid-stream, nothing is lost."""
    ks = KillSwitch()
    # A generous bucket, so the RATE limiter cannot be what changes the outcome. This test must
    # isolate the kill switch, otherwise a pass could come from the wrong guard.
    g = Guards(ks, UpgradeRateLimiter(rate_per_sec=1e6, burst=1e6))
    results, lock = [], threading.Lock()
    N_THREADS, PER_THREAD = 8, 500

    def worker() -> None:
        local = [g.decide(FLOOR, UP) for _ in range(PER_THREAD)]
        with lock:
            results.extend(local)

    threads = [threading.Thread(target=worker) for _ in range(N_THREADS)]
    for t in threads:
        t.start()
    ks.engage()          # thrown WHILE the threads are running, which is the case that matters
    for t in threads:
        t.join()

    total = N_THREADS * PER_THREAD
    assert len(results) == total, f"requests were DROPPED: {len(results)} of {total} returned"
    assert set(results) <= {FLOOR, UP}, f"a non-tier value escaped: {set(results) - {FLOOR, UP}}"
    assert all(r is not None for r in results), "a request returned None instead of a tier"
    print(f"OK  1 kill switch: {total} concurrent decisions, none dropped, every value a tier")

    # After the switch is engaged, every subsequent decision must be the floor. Checked separately
    # from the racing window, because during the race either answer is legitimate.
    after = [g.decide(FLOOR, UP) for _ in range(100)]
    assert set(after) == {FLOOR}, f"engaged kill switch still upgraded: {set(after)}"
    print("OK  2 kill switch engaged: every decision falls to the floor")

    ks.release()
    assert g.decide(FLOOR, UP) == UP, "released kill switch did not restore classification"
    print("OK  3 kill switch released: classification restored, so withdrawal is reversible")


def test_rate_guard_degrades_to_floor_never_rejects() -> None:
    """Property 2. Saturate the bucket and assert the excess is FLOORED, not refused."""
    rl = UpgradeRateLimiter(rate_per_sec=0.0, burst=10)   # 10 upgrades, no refill
    t = 1000.0
    out = [rl.decide(FLOOR, UP, now=t) for _ in range(100)]

    assert set(out) <= {FLOOR, UP}, f"a non-tier value escaped: {set(out) - {FLOOR, UP}}"
    assert out.count(UP) == 10, f"burst not honoured: {out.count(UP)} upgrades admitted, expected 10"
    assert out.count(FLOOR) == 90, f"excess not floored: {out.count(FLOOR)} floored, expected 90"
    assert rl.upgrades_admitted == 10 and rl.upgrades_degraded == 90
    print("OK  4 rate guard: burst of 10 admitted, 90 excess DEGRADED TO FLOOR, zero rejections")

    # A request that was never an upgrade must be untouched: the guard must not become a general
    # rate limiter on traffic it has no opinion about.
    before = rl.upgrades_degraded
    assert rl.decide(FLOOR, FLOOR, now=t) == FLOOR
    assert rl.upgrades_degraded == before, "a non-upgrade consumed the guard's budget"
    print("OK  5 rate guard ignores non-upgrades: they neither consume tokens nor count as degraded")

    # Continuous refill, not fixed windows: a fixed-window implementation would admit two windows'
    # worth of upgrades across a boundary, which is the classic defect.
    rl2 = UpgradeRateLimiter(rate_per_sec=2.0, burst=2)
    t0 = 5000.0
    assert [rl2.decide(FLOOR, UP, now=t0) for _ in range(5)].count(UP) == 2, "burst wrong"
    assert rl2.decide(FLOOR, UP, now=t0 + 0.5) == UP, "refill did not accrue continuously"
    assert rl2.decide(FLOOR, UP, now=t0 + 0.5) == FLOOR, "refill over-credited at the same instant"
    print("OK  6 rate guard refills continuously, so a window boundary cannot double-admit")


def test_ordering_kill_switch_short_circuits() -> None:
    """The kill switch is checked FIRST: withdrawal must not depend on the limiter's state."""
    rl = UpgradeRateLimiter(rate_per_sec=0.0, burst=0)   # exhausted from the start
    g = Guards(KillSwitch(engaged=True), rl)
    assert g.decide(FLOOR, UP) == FLOOR
    assert rl.upgrades_degraded == 0, "the rate limiter ran even though the kill switch was engaged"
    print("OK  7 kill switch short-circuits: the limiter does no bookkeeping when withdrawal is on")


def test_aggregate_counter_makes_the_replication_error_visible() -> None:
    """Property 8. The limiter is PER INSTANCE, so the fleet ceiling is N times the local one.

    That is stated in the docstring, and a bound that only exists in prose is not a bound: nothing
    measures it, so nothing can say when N grew. This proves the counters an aggregate alert reads
    actually exhibit the overshoot, rather than proving the docstring can be read.

    The alert this pins down is fleet-wide, not per-pod:

        sum(rate(classifier_upgrades_admitted_total[5m])) > <pool headroom rate>

    A per-pod alert cannot fire here by construction. Every instance below is INSIDE its own budget;
    the violation exists only in the sum.
    """
    N_REPLICAS, PER_REPLICA_BURST = 4, 10
    replicas = [UpgradeRateLimiter(rate_per_sec=0.0, burst=PER_REPLICA_BURST) for _ in range(N_REPLICAS)]
    t = 2000.0
    for rl in replicas:
        for _ in range(50):                    # each replica saturated well past its own budget
            rl.decide(FLOOR, UP, now=t)

    per_replica = [rl.counters()["classifier_upgrades_admitted_total"] for rl in replicas]
    assert set(per_replica) == {PER_REPLICA_BURST}, f"a replica exceeded its LOCAL budget: {per_replica}"

    fleet = sum(per_replica)
    assert fleet == N_REPLICAS * PER_REPLICA_BURST, f"aggregate does not sum: {fleet}"
    assert fleet > PER_REPLICA_BURST, "the overshoot this property exists to expose did not occur"
    print(f"OK  8 aggregate: {N_REPLICAS} replicas each within their local {PER_REPLICA_BURST}, "
          f"fleet admitted {fleet}, which is {N_REPLICAS}x the intended ceiling and visible ONLY in the sum")

    # Degraded is exported too. Without it, "few upgrades admitted" cannot be told apart from
    # "little upgrade traffic arrived", and only one of those is a capacity signal.
    assert replicas[0].counters()["classifier_upgrades_degraded_total"] == 40
    print("OK  9 degraded exported alongside admitted, so low admitted can be told from low demand")

    # The kill switch is a GAUGE on the same read. Withdrawal and a genuine collapse in upgrade
    # traffic produce the same admitted series, and they call for opposite responses.
    ks = KillSwitch()
    g = Guards(ks, UpgradeRateLimiter(rate_per_sec=1e6, burst=1e6))
    assert g.counters()["classifier_kill_switch_engaged"] == 0
    ks.engage()
    assert g.counters()["classifier_kill_switch_engaged"] == 1
    print("OK 10 kill switch exported as a gauge, so withdrawal is distinguishable from a demand drop")

    # Monotonic: a counter that can decrease breaks rate() silently rather than loudly.
    before = replicas[0].counters()["classifier_upgrades_admitted_total"]
    replicas[0].decide(FLOOR, FLOOR, now=t)
    assert replicas[0].counters()["classifier_upgrades_admitted_total"] >= before
    print("OK 11 counters are monotonic, so rate() over them is meaningful")


def test_constraint_driven_placements_bypass_the_budget() -> None:
    """Property 12, amendment 0b A23. The guard bounds CHOICE, not physics.

    A context-bound placement is not an upgrade. Charging it against the budget would treat a
    requirement as discretionary spend and then refuse a request that was never optional, and it
    would do so precisely when the system is stressed, because an EXHAUSTED LIMITER IS THE STEADY
    STATE UNDER LOAD.
    """
    CONSTRAINT_FLOOR = UP           # physics forces the dearer class for this request
    rl = UpgradeRateLimiter(rate_per_sec=0.0, burst=0)   # exhausted from the first request
    g = Guards(KillSwitch(), rl)

    for _ in range(500):
        assert g.decide(CONSTRAINT_FLOOR, UP) == UP, "an exhausted limiter degraded a FORCED placement"
    assert rl.upgrades_admitted == 0 and rl.upgrades_degraded == 0, (
        f"a forced placement was COUNTED against the budget: admitted={rl.upgrades_admitted} "
        f"degraded={rl.upgrades_degraded}. It must bypass, not merely survive."
    )
    print("OK 12 500 constraint-driven placements through an EXHAUSTED limiter: all placed at the")
    print("      forced class, and NONE counted against the budget")

    # The kill switch must not withdraw physics either. Withdrawal removes the classifier's
    # discretion; it cannot remove a requirement.
    gk = Guards(KillSwitch(engaged=True), UpgradeRateLimiter(rate_per_sec=1e6, burst=1e6))
    assert gk.decide(CONSTRAINT_FLOOR, UP) == UP, "the kill switch withdrew a FORCED placement"
    print("OK 13 an engaged kill switch withdraws discretion, not a physical requirement")

    # And the discretionary case is unchanged, which is what keeps this an exemption rather than a
    # hole: when the floor is genuinely the cheaper class, the budget still binds.
    rl2 = UpgradeRateLimiter(rate_per_sec=0.0, burst=3)
    g2 = Guards(KillSwitch(), rl2)
    out = [g2.decide(FLOOR, UP) for _ in range(10)]
    assert out.count(UP) == 3 and out.count(FLOOR) == 7, out
    assert rl2.upgrades_admitted == 3 and rl2.upgrades_degraded == 7
    print("OK 14 discretionary upgrades are still bounded and still counted, so this is an")
    print("      exemption for forced placements rather than a hole in the budget")


def main() -> int:
    test_kill_switch_flips_under_load_without_dropping()
    test_rate_guard_degrades_to_floor_never_rejects()
    test_ordering_kill_switch_short_circuits()
    test_aggregate_counter_makes_the_replication_error_visible()
    test_constraint_driven_placements_bypass_the_budget()
    print("PASS: both section 7 guards exist, degrade to the floor, never reject, and export the")
    print("      counters a FLEET-WIDE alert needs, since the per-instance limiter cannot see it")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
