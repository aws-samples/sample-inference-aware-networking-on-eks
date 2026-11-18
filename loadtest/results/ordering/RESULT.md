# Result: the race is REAL and the ordering prevents it. Outcome A, as pre-registered.

Run 2026-08-03 at zero GPU, against the pre-registration in this directory, which was committed
before the first request and is unedited.

## Verdict

**Outcome A, predicted with high confidence and confirmed.** Inverting the ordering reproduces the
defect R.5b describes, and restoring it prevents the same request from failing. The two runs differ
in exactly one variable, the load balancer idle timeout.

| Run | Idle timeout | Route timeout | Stub | HTTP | Elapsed | Access log flags | Envoy `code` |
|---|---|---|---|---|---|---|---|
| Inverted | **60 s** | 600 s | 112 s | none, connection cut | 59 s | **`DC`** | `0` |
| Control | **690 s** | 600 s | 112 s | **200** | 113 s | **`-`** | `200` |

Raw, from the Envoy access log using the committed REQTRACE format:

    REQTRACE ... path=/ordering-stub code=0   upstream=10.0.0.1:8080 flags=DC dur=59371  resp_dur=-
    REQTRACE ... path=/ordering-stub code=200 upstream=10.0.0.1:8080 flags=-  dur=112001 resp_dur=112001

## Reading it, primary discriminator first

The access log is causal and server-side, which is why it was pre-registered as primary: both
sides of this race emit 504, so a status code could not have told them apart.

1. **`flags=DC`, downstream disconnect.** Envoy recorded that the CLIENT SIDE went away. Envoy did
   not terminate this request: it never reached its own route timeout, which is why `code=0` rather
   than 504 and why no `UT` flag appears. Something between the client and Envoy cut the
   connection, and the only thing there is the load balancer.
2. **`dur=59371` ms against an idle timeout of 60 s.** The cut lands 629 ms under the configured
   value, which is the load balancer's own timer, not a coincidence.
3. **Stronger than predicted:** the pre-registration expected a 504 from the load balancer. The
   client received NO HTTP RESPONSE AT ALL (curl exit with no status). So the failure is worse than
   the defect description assumed: it is not a misattributed 504, it is a bare connection cut with
   no error document, which a caller cannot distinguish from a network fault.
4. **The control closes it.** Same request, same stub, same route timeout, idle restored to 690:
   HTTP 200 at 113 s with no flags. The ordering is the only thing that changed.

## Bracketing, which is what makes the comparison trustworthy

Idle timeout read from the AWS API before (690), during (60), and after (690). Load balancer
identity `a6cce95249a4141ce8052325be937ddf` **unchanged across all three reads**, so the annotation
churn that would have invalidated the comparison did not occur. Recording that identity earlier is
what made this checkable.

## Boundary, agreed in advance and repeated because it will be quoted

Scale-freeness holds for the ORDERING, not for every mechanism in the path. The target ENI
connection-tracking default of 350 seconds sits BETWEEN this test and production. So this run
establishes the ORDERING MECHANISM below 350 s and says NOTHING about the regime above it, where
the production pair 600 and 690 actually lives. The delivered-value read from the AWS API remains
the production check. **This is evidence for the mechanism, not clearance for the values.**

## Cost

Zero accelerator time. One `python:3.12-slim` stub on the existing system nodes, deleted after.
