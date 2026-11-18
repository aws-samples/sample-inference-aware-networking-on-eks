#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# D-7: THE RELAY'S ADDED FIRST-BYTE COST. Zero GPU by construction.
#
# WHY THIS IS THE MEASUREMENT THAT MATTERS NEXT. P's floor is now measured, and it made the relay's
# BENEFIT computable and NARROW: promotion touches only generations past about 5,640 output tokens,
# the top quarter of an 8,192-token context. The relay's COST is the opposite shape: it is paid on
# EVERY interactive request, universally. D-7's first-byte overhead has been UNVERIFIED since v5, so
# both sides of that trade can now be written down and only one of them has a number.
#
# WHY IT NEEDS NO ACCELERATOR. The quantity D-7 names is the RELAY'S ADDED HOP, not anything about
# generation. A paired measurement isolates exactly that: the same request, through the relay and
# directly against the gateway, same route, same load balancer, same backend. Whatever the backend
# costs cancels in the difference. So a stub backend is not a compromise here, it is the correct
# instrument: it removes the term that would otherwise dominate and vary.
#
# THE CAVEAT, RECORDED RATHER THAN DISCOVERED LATER. No model serving occurs, by construction. If
# the relay's presence changed queueing behaviour at a busy engine, or interacted with connection
# reuse under real generation load, this would not see it. What this measures is the HOP, and the
# hop is what D-7 asks about.
#
# ON THE BACKEND CHOICE, from the 2026-08-05 run: the REAL gateway with EMPTY POOLS answers 503
# immediately and is a BETTER instrument than the hanging stub, because the path under test is then
# the genuine Envoy route including the ext_proc Endpoint Picker callout rather than a substitute
# for it. Either backend works; the real one measures more of the real path.
#
# INTERLEAVED, NOT BATCHED. Samples alternate direct/relayed rather than running all of one then all
# of the other, because anything drifting during the run (a noisy neighbour, a scaling event, a load
# balancer rebalance) would otherwise land entirely on one arm and be read as the effect.
#
# REPORTED AS A DISTRIBUTION, NOT A MEAN. A mean hides the tail, and the tail is where an added hop
# hurts. The denominator it will be judged against is the 0.4632 s WARM BASELINE.
set -uo pipefail
NS="${NS:-inference}"
N="${N:-40}"                  # paired samples; each contributes one direct and one relayed
WARM_BASELINE_S="0.4632"      # the denominator D-7's acceptance question is asked against

command -v kubectl >/dev/null 2>&1 || { echo "FAIL kubectl not on PATH"; exit 1; }
kubectl version >/dev/null 2>&1 || { echo "FAIL cannot reach the cluster; this measurement needs it (no GPU, but a cluster)"; exit 1; }

GW=$(kubectl -n "$NS" get configmap gateway-endpoint -o jsonpath='{.data.base}' 2>/dev/null)
[ -n "$GW" ] || { echo "FAIL gateway-endpoint ConfigMap absent; run setup-async-infra.sh"; exit 1; }

RPOD=$(kubectl -n "$NS" get pods -l app=inference-relay \
  -o jsonpath='{range .items[?(@.status.phase=="Running")]}{.metadata.name}{"\n"}{end}' | head -1)
[ -n "$RPOD" ] || { echo "FAIL no running relay pod"; exit 1; }


echo "=== D-7: relay added first-byte cost, paired and interleaved, ZERO GPU ==="
echo "  samples          : $N pairs (one direct, one relayed each)"
echo "  gateway          : $GW"
echo "  measured from    : inside $RPOD, so the client hop is identical for both arms"
echo "  warm baseline    : ${WARM_BASELINE_S}s (the denominator, not a measurement made here)"
echo

kubectl -n "$NS" exec "$RPOD" -- python -c "
import json, statistics, time, urllib.error, urllib.request

GW = '$GW'
KEY = '$KEY'
N = $N
BASE = float('$WARM_BASELINE_S')
BODY = json.dumps({'model': 'stub', 'prompt': 'x', 'max_tokens': 1}).encode()

def first_byte(url, headers):
    r = urllib.request.Request(url, data=BODY, headers=headers)
    t = time.perf_counter()
    try:
        resp = urllib.request.urlopen(r, timeout=30)
        resp.read(1)
    except urllib.error.HTTPError as e:
        e.read(1)          # a 4xx/5xx still exercised the whole hop, which is what is timed
    except Exception:
        return None
    return time.perf_counter() - t

direct, relayed = [], []
for i in range(N):
    # INTERLEAVED. Order also alternates within the pair so a systematic first-of-pair effect
    # (connection setup, cache warmth) does not attach to one arm.
    if i % 2 == 0:
        d = first_byte(GW + '/v1/completions', {'content-type': 'application/json',
                                                'x-gateway-inference-objective': 'batch'})
        rl = first_byte('http://localhost:8080/v1/completions',
                        {'content-type': 'application/json', 'x-caller-id': 'd7-overhead',
                         'x-gateway-inference-objective': 'batch'})
    else:
        rl = first_byte('http://localhost:8080/v1/completions',
                        {'content-type': 'application/json', 'x-caller-id': 'd7-overhead',
                         'x-gateway-inference-objective': 'batch'})
        d = first_byte(GW + '/v1/completions', {'content-type': 'application/json',
                                                'x-gateway-inference-objective': 'batch'})
    if d is not None and rl is not None:
        direct.append(d); relayed.append(rl)

if len(direct) < 5:
    print('  FAIL too few usable pairs (%d). Not reporting a distribution from this.' % len(direct))
    raise SystemExit(1)

def pct(xs, p):
    xs = sorted(xs); k = max(0, min(len(xs) - 1, int(round(p / 100.0 * (len(xs) - 1)))))
    return xs[k]

# PAIRED deltas, not a difference of aggregates. Pairing is what removes drift common to both arms.
deltas = [r - d for r, d in zip(relayed, direct)]

print('  usable pairs     : %d' % len(direct))
print()
print('  %-10s %10s %10s %10s %10s' % ('arm', 'p50 ms', 'p90 ms', 'p99 ms', 'max ms'))
for name, xs in (('direct', direct), ('relayed', relayed)):
    print('  %-10s %10.2f %10.2f %10.2f %10.2f' % (name, pct(xs,50)*1000, pct(xs,90)*1000,
                                                   pct(xs,99)*1000, max(xs)*1000))
print('  %-10s %10.2f %10.2f %10.2f %10.2f' % ('DELTA', pct(deltas,50)*1000, pct(deltas,90)*1000,
                                               pct(deltas,99)*1000, max(deltas)*1000))
print()
d50 = pct(deltas, 50); d90 = pct(deltas, 90)
print('  THE D-7 QUANTITY: added first-byte cost, p50 %.1f ms, p90 %.1f ms' % (d50*1000, d90*1000))
print('  Against the %.4f s warm baseline: p50 is %.2f%%, p90 is %.2f%% of it.'
      % (BASE, 100*d50/BASE, 100*d90/BASE))
print()
print('  CAVEAT: whatever backend answered, NO MODEL SERVING OCCURRED, so this is the HOP cost,')
print('  which is what D-7 asks about. Run 2026-08-05 against the REAL gateway with empty pools')
print('  (immediate 503), which is a better instrument than a stub because the path under test is')
print('  the genuine Envoy route including the ext_proc Endpoint Picker callout. It does not')
print('  capture any effect the relay has on queueing at a BUSY engine, and it is concurrency 1.')
" 2>&1
