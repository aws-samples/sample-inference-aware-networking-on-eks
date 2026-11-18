# Pre-registration: does the load balancer idle timeout race the gateway route timeout

Written 2026-08-03 BEFORE any request was issued. Nothing below is edited after execution; the
result lands in `RESULT.md` beside this file and in `ARCHITECTURE-REVIEW.md` R.5b.

## What is being tested, and why it can be tested at zero GPU

R.5b records a live defect: the external load balancer idle timeout and the gateway route timeout
were BOTH 600 seconds, and equal values race, so a non-streaming request running to the ceiling
could receive a load balancer error instead of the gateway's own 504. The fix raised L to 690.

The fix is currently REASONED, not observed. Nothing in the committed suite crosses the 600 second
ceiling (the longest recorded generation is 112 s), so R.5b derives an ordering and asserts it as
delivered, which is the shape this project distrusts.

The property is a STRICT ORDERING, and orderings are scale-free, so the constants shrink instead of
the workload growing. And the property is entirely NETWORK-LAYER: nothing about which timer fires
first involves inference. So a stub backend that simply sleeps replaces the GPU completely.

## Design

- A stub Deployment on the system nodes, no accelerator, that sleeps ~112 s before responding.
  112 s is chosen deliberately: it is the longest generation this project has actually measured
  (ARCHITECTURE-REVIEW.md Phase 1), so the test sits inside known territory.
- A test HTTPRoute with an EXPLICIT 600 s timeout mirroring production, so the route side of the
  race is the real value rather than a default.
- The load balancer idle timeout is temporarily lowered to 60 s, which INVERTS the ordering into
  violation. Restored to 690 and verified afterwards.

## The discriminator, and why status code alone cannot serve

BOTH sides of this race can emit 504. A Classic Load Balancer whose idle timeout expires returns
504; Envoy's route timeout also returns 504. So the status code is not evidence.

**PRIMARY DISCRIMINATOR: the Envoy access log.** The committed REQTRACE format already emits
`%RESPONSE_FLAGS%`, so no instrumentation is needed. Envoy sets an upstream-timeout flag (`UT`)
when IT terminates the request on the route timeout. A load balancer cutting the connection first
presents differently: a downstream-disconnect flag (`DC`), or no completed entry at all, because
Envoy never reached its own timeout. That signal is causal and server-side.

**CORROBORATION, not primary:** wall-clock timing and response provenance.

## The three outcomes, written before the first request

| # | Outcome | Access log signature | Timing | Meaning |
|---|---|---|---|---|
| A | The load balancer wins | `DC` flag, or NO completed entry for the request | ~60 s | THE RACE EXISTS. Inverting the ordering reproduces the defect R.5b describes. |
| B | The backend completes | `-` (no flag), 200 | ~112 s | The stub finished before either timer. The test failed to create the race and must be re-run with a longer sleep or a shorter idle timeout. |
| C | The route wins | `UT` flag, 504 | ~600 s | Ordering respected. Should NOT happen at idle 60, and would mean the annotation did not take. |

**Prediction, committed:** outcome A. Confidence high. The mechanism is simple and the margin is
large (60 s against 112 s against 600 s).

## Bracketing

Idle-timeout read-backs from the AWS API before and after, plus the load balancer NAME, against
the recorded identity `a6cce95249a4141ce8052325be937ddf`. Recording the identity is what makes an
annotation-driven Service churn detectable: if the name changes, the Service was re-provisioned and
the delivered-value reading from earlier today no longer describes the load balancer in the path.

## Boundary on what this proves, agreed in advance

Scale-freeness holds for the ORDERING, not for every mechanism in the path. At least one threshold
sits between this test and production: the target ENI connection-tracking default of 350 seconds.
So this run validates the ORDERING MECHANISM below 350 s and says NOTHING about the regime above
it, where the production pair 600 and 690 lives. The delivered-value read from the AWS API remains
the production check. This is evidence for the mechanism, not clearance for the values.
