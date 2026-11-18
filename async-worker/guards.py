# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Classifier guards: the kill switch and the maximum upgrade rate.

Design v6.1 section 7 requires BOTH to exist BEFORE any classifier does, and the ordering is
deliberate rather than tidy: an unguarded upgrade-only classifier is a mechanism for violating this
project's own headline tier-isolation result from the inside. The interactive pool scales 4 to 6
replicas, is on-demand only and pinned to one instance family, so total elastic headroom is TWO
replicas; scale-up takes 170 s dominated by model load; and a classifier shifts traffic in
milliseconds. Guards first, classifier second.

This module is shared by the two placements design v6.2 recognises: the interactive front-door
relay (Phase 2) and the submit-API sidecar. It has NO web-framework and NO AWS dependency so both
can import it, and so it is testable with no cluster.

THE PROPERTY THAT GOVERNS BOTH GUARDS: they DEGRADE TO THE FLOOR, they never REJECT.

A guard that returns an error converts a capacity-protection mechanism into an availability
incident, which is strictly worse than the problem it solves. Under upgrade-only semantics the
declared or defaulted tier is always a safe placement (decision D-5: the system may raise a tier
and never lower one), so "guard fired" and "serve at the floor" are the same action. That is why
neither guard has a rejection path at all: not as a policy that could be configured differently,
but structurally, because the functions return a tier rather than a verdict.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field


@dataclass
class KillSwitch:
    """Disables classification in one change, at any time, including under load.

    Withdrawal must ALWAYS be possible, including when the pool is already saturated, which is
    exactly when someone will reach for it. So the check is a plain read of a boolean with no lock,
    no I/O and no allocation on the hot path: it cannot itself fail, block, or add latency when the
    system is already in trouble.

    When engaged, `decide` returns the caller's floor unchanged. Requests are NOT dropped and NOT
    errored; the system simply stops making its own placement decisions and honours what the caller
    declared. That is the definition of "falls back to shadow" in section 7.
    """

    engaged: bool = False

    def engage(self) -> None:
        self.engaged = True

    def release(self) -> None:
        self.engaged = False

    def decide(self, floor: str, proposed: str) -> str:
        """Return the tier to route to. Never raises, never returns a rejection."""
        return floor if self.engaged else proposed


@dataclass
class UpgradeRateLimiter:
    """A token bucket bounding how much traffic the classifier may point at a scarcer pool.

    Nothing else bounds it. The classifier can shift traffic in milliseconds; the pool it shifts
    onto has two replicas of headroom and a 170 s scale-up. Without a cap, a misclassifying
    classifier saturates the interactive pool and the callers already using it pay for it in
    latency, which is someone else's SLO, not the upgrader's money.

    LOCAL AND THEREFORE APPROXIMATE UNDER REPLICATION, stated rather than hidden: N instances each
    holding their own bucket admit up to N times the per-instance rate. Set the per-instance budget
    from the pool's headroom divided by the expected instance count, and alert on the aggregate.
    Exact enforcement needs a shared counter, which is the same deferred item the reservation guard
    carries.

    THE APPROXIMATION IS THEREFORE MADE OBSERVABLE RATHER THAN LEFT AS PROSE. `counters()` exports
    the two cumulative totals this instance has accumulated, which is what a scrape reads and what
    the aggregate alert sums across replicas. An error bound that only exists in a docstring is not
    an error bound: nothing measures it, so nothing can tell you when N grew and the effective
    ceiling grew with it. Fleet-wide admitted rate is the quantity the alert watches, and it is the
    quantity a per-instance limiter structurally cannot see.

    Refill is continuous rather than per-window, so a burst at a window boundary cannot admit two
    windows' worth of upgrades, which is the classic fixed-window defect.
    """

    rate_per_sec: float
    burst: float
    _tokens: float = field(default=0.0, init=False)
    _last: float = field(default_factory=time.monotonic, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)
    upgrades_admitted: int = field(default=0, init=False)
    upgrades_degraded: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        self._tokens = float(self.burst)

    def decide(self, floor: str, proposed: str, now: float | None = None) -> str:
        """Return the tier to route to.

        An upgrade costs one token. With no token available the request is served AT THE FLOOR,
        which is a correct placement under upgrade-only semantics, not a degraded one. A request
        that was not an upgrade in the first place costs nothing and is never affected: the guard
        must not become a general rate limiter on traffic it has no opinion about.
        """
        if proposed == floor:
            # NOT AN UPGRADE, SO NOT COUNTED AND NO TOKEN SPENT (amendment 0b A23). This branch is
            # what makes constraint-driven placements bypass the budget: the caller passes the
            # CONSTRAINT FLOOR as `floor`, so a request physics forces to `standard` arrives here
            # with proposed == floor and never touches the bucket.
            #
            # The guard bounds what the CLASSIFIER CHOOSES, not what physics requires. Charging a
            # forced placement against the budget would treat a requirement as discretionary spend
            # and refuse a request that was never optional, and it would do so precisely under load,
            # when an exhausted limiter is the steady state.
            #
            # No new exposure: a caller could always reach the standard pool for free by declaring
            # it, so the budget never bounded caller-reachable placements, only classifier
            # discretion.
            return floor
        t = time.monotonic() if now is None else now
        with self._lock:
            elapsed = max(0.0, t - self._last)
            self._last = t
            self._tokens = min(self.burst, self._tokens + elapsed * self.rate_per_sec)
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                self.upgrades_admitted += 1
                return proposed
            self.upgrades_degraded += 1
            return floor

    def counters(self) -> dict[str, int]:
        """Cumulative totals for export. Monotonic, so a scrape can rate() them.

        Read under the lock: the two fields are updated together on the hot path, and a scrape that
        caught them mid-update would export a pair that never actually held. The cost is a lock
        acquisition per scrape, which is once per scrape interval rather than once per request.
        """
        with self._lock:
            return {
                "classifier_upgrades_admitted_total": self.upgrades_admitted,
                "classifier_upgrades_degraded_total": self.upgrades_degraded,
            }


@dataclass
class Guards:
    """The pair, applied in the order section 7 implies.

    The kill switch is checked FIRST and short-circuits: when someone is withdrawing the classifier
    under load, the rate limiter's bookkeeping is irrelevant and should not run at all.
    """

    kill_switch: KillSwitch
    rate_limiter: UpgradeRateLimiter

    def decide(self, floor: str, proposed: str, now: float | None = None) -> str:
        if self.kill_switch.engaged:
            return floor
        return self.rate_limiter.decide(floor, proposed, now=now)

    def counters(self) -> dict[str, int]:
        """Everything a scrape needs, including whether the kill switch is currently engaged.

        The kill switch is exported as a gauge because "upgrades fell to zero" and "someone
        withdrew the classifier" look identical in the admitted-rate series alone, and they call for
        opposite responses. Distinguishing them at read time costs one integer.
        """
        c = self.rate_limiter.counters()
        c["classifier_kill_switch_engaged"] = 1 if self.kill_switch.engaged else 0
        return c
