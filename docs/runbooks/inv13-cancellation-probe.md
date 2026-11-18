# Runbook: the INV13 cancellation probe

Status: **PROVEN AND DORMANT**, which are not in tension. Its passing path executed 2026-08-05
against a real generation on a `g6e.xlarge`, so `DEBT-PASSING-PATH` is CLEARED. It remains dormant
between runs by design, because the probe's value is repetition rather than continuity.

**The first execution found three defects in the probe itself**: it selected pods by
`app=<deployment-name>` when the serving pods carry `app=vllm-llama3` with the tier in a separate
label; it built its request body with a `sed` nested four levels deep inside `kubectl exec`, so the
body arrived malformed and vLLM rejected it instantly; and its model-id extraction used a greedy
`sed` that returned a permission id rather than a model. None of that code had ever run, because
every green reading the probe had ever produced was a SKIP. That is what the debt marker is for.

**A follow-up the first run identified.** Each sample costs a `kubectl exec` round trip, measured at
2,625 ms in the same session, so the probe's RESOLUTION is about 2.6 s and it cannot measure the
0.55 to 0.86 s interval 0b A15 recorded. The slot freed within the first sample, which is consistent
with A15 and is all this instrument can say. The WARN line therefore fires on every run as an
artefact of the instrument. A tightened timing needs in-pod measurement, which is a different probe.

## What it proves, in one paragraph

That closing the connection mid-generation frees the sequence slot. Decision D-1 rests on exactly
this: at the promotion deadline the relay abandons an in-flight generation by closing the
connection. If disconnect propagation breaks, **nothing fails**. The caller still gets a job id, the
replica keeps generating, the GPU stays occupied, and the tier isolation LT-4 verified degrades with
no error anywhere. That silence is the entire reason the probe exists.

## Why it is scheduled rather than always-on

The probe's value is **repetition, not continuity**. It catches regressions introduced by changes,
and changes arrive with commits rather than with the clock. Always-on serving to run a 20-second
check keeps one `g6e.xlarge` up for 730 hours a month. The scheduled cycle below keeps it up for
about 24.3 hours a month: thirty to one for identical coverage, at any price. In dollars, see the
Cost section. The 170-second model load (LT-5) is why one cycle takes minutes rather than seconds.

## Running it

```
bash tests/smoke/probe_cancellation.sh                    # dormant: SKIPs cleanly, exit 0
PROBE_REQUIRE=1 bash tests/smoke/probe_cancellation.sh    # scheduled: absence is a FAILURE, exit 1
```

`PROBE_REQUIRE=1` is the mode a scheduled cycle must use. On a schedule, "no serving replica" means
the bring-up failed, and that is a real result rather than a reason to skip.

Environment: `NS` (default `inference`), `DEPLOY` (default `vllm-llama3-interactive`),
`WARN_BOUND_S` (2), `HARD_BOUND_S` (5).

## Reading the result

| Output | Meaning | Action |
|---|---|---|
| `SKIP` | Nothing ran and **nothing was proven**. Exit 0 only so a dormant probe does not redden an unrelated suite. | None, unless you expected a replica. |
| `OK ... slot freed Ns` | The mechanism works. Measured 0.55 to 0.86 s on 2026-08-03, 4 of 4 cells. | None. |
| `WARN slower than ...` | Freed, but above the 2 s warn bound. | Note it in the run's record. Watch for a trend; one slow cycle is not a regression. |
| `FAIL ... did NOT free` | **Disconnect propagation is broken.** | Stop relying on D-1 until it is understood. See below. |
| `FAIL ... could not establish a generation` | The probe failed, not the mechanism. Reported as a failure precisely so it cannot be misread as a pass. | Check the model loaded and the pod is serving. |

The last row is the important design choice: a probe that cannot create the condition it tests
**must not report success**. Reporting "nothing was running, so nothing failed to cancel" as green is
the same defect class as a KEDA scaler whose empty vector becomes a plausible zero.

## If it fails

Four things can break disconnect propagation. In rough order of likelihood:

1. **The vLLM pin moved.** Highest-probability cause and the one already controlled: the
   version-coupled register (design v6.2 section 2.2.1) freezes the pin through Phase 3, and moving
   it re-runs section 0.2 plus the cancellation experiment by recorded rule. Check `versions.yaml`
   against the running image first, because it is quick and it is the usual suspect.
2. **The relay's HTTP client changed.** The relay must abandon by CLOSING THE CONNECTION. A client
   that switched to waiting out a timeout, or that got a retry added, does not produce a disconnect
   at all. See `async-worker/relay.py`, which retries nothing for a related reason.
3. **Envoy Gateway configuration changed.** Disconnects have to propagate through the proxy to
   reach vLLM. Read the running configuration from `config_dump`, not the manifest, because the
   question is what the data plane is doing.
4. **A keepalive or idle-timeout change** anywhere in Chain B is holding the connection open past
   the client's close. The committed constants are in
   `manifests/03-routing/chain-b-timeouts.yaml`.

## Finalized 2026-08-04 against the real routing topology

The runbook was written when no routes existed. Three things are now concrete rather than assumed,
and one of them changes what the probe must do.

**1. The probe must go THROUGH THE RELAY, not straight at a pod.** The relay is the sole external
entry (INV12). A probe that execs into a vLLM pod and
curls `localhost:8000` tests vLLM's disconnect handling, which is not the property D-1 rests on:
D-1 rests on the RELAY abandoning a generation by closing its connection, through the gateway. The
current script's in-pod form is the narrower test, kept because it isolates the engine, and a
second form through the relay is required before the result can be claimed for D-1.

**2. It needs a credential.** Give the probe its own caller id (`inv13-probe`) rather than reusing
a driver's, so a probe run is distinguishable from real traffic in `relay_requests_total`. Rotate
it on the same 90-day line as the rest.

**3. The pair it must send is `batch` + `standard`.** The interactive cell is what D-1's promotion
path abandons, but the probe should exercise the cell that will actually hold a long generation,
and interactive+economy coalesces (6.9.1) so it cannot be selected directly.

**Still DORMANT, and the first run still awaits authorization.** Nothing above changes the standing
deferral: the probe needs one `g6e` replica, that is the accelerator spend, and it has not been
authorized. What changed is that the probe is now specified against a topology that exists rather
than one that was planned, so the first authorized run has nothing left to design.

## Bounds, and the qualifier that travels with them

Warn at 2 s, fail at 5 s. **Both were derived in the concurrency-1, single-replica regime**, and
that qualifier is part of the number rather than a footnote to it. Under load the measurement
includes queueing time, so the bounds must be re-derived before the probe is pointed at a loaded
replica. Carrying them across unchanged would turn a latency measurement into a load measurement
without anyone deciding to.

## Cost, recorded as a named line

A bring-up-and-park cycle at roughly 4 per day, about 12 minutes each with the 170 s model load
dominating, keeps one `g6e.xlarge` up for 0.8 hours a day, about 24.3 hours a month (30.42 days).
At the us-west-2 Linux On-Demand price of 1.861 USD per hour (AWS public price list, published
2026-10-29), that is about 45 USD per month. Always-on serving would be 730 hours, about 1,359 USD
per month. Both figures cover the accelerator instance only: the cluster, its system nodes, and the
probe's own CPU cost the same either way. Recompute with your Region's price. It goes in the billing
criterion as a named line, the same way audit logging did, so a standing cost cannot become one
nobody revisits.
