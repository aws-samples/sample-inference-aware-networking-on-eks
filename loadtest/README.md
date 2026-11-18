# Load-test harness

Validates each correctness guarantee the architecture makes, on the real model at
realistic replica counts. Status: AUTHORED, pending a Gate 2 dry-run on the live
cluster (needs fresh credentials, the Hugging Face token for the target model, and
the scaled fleet). Nothing here has been executed yet; no results are recorded until
a real run produces them.

## Layout

- `workloads.py`   seeded, deterministic corpora (interactive-short, batch-long, prefix-heavy, reasoning-long).
- `locust_sync.py` sync path load generator; measures TTFT (streamed first token) and end-to-end.
- `locust_async.py` async submit path; drives N distinct jobs, each submitted D times to force duplicates.
- `faults.sh`      named fault actions: kill-workers, drain-batch-node, kill-epp, restore-epp.
- `analyze.py`     reads Prometheus (worker_claims_total, per-tier TTFT, prefix-cache hits) and DynamoDB (complete count) and prints pass/fail. Never invents a number.
- `client-nodegroup.yaml` dedicated c5.12xlarge client node group (keep load off the serving path).

## Method (applies to every scenario)

1. Seed every workload (LOADTEST_SEED, default 1337) and record it with the result.
2. Warm-up window discarded; measure only steady state; report percentiles (p50/p90/p99), not averages.
3. At least two repetitions; report the variance.
4. Record the topology with every result: model + revision, replica counts, node types and counts, region, and the versions.yaml at test time.
5. Client isolation: run Locust on the `loadgen` node group (or off-cluster). Log client CPU and network headroom per run to prove the client is not the bottleneck.
6. Do not weaken an assertion to make a scenario pass. A failed scenario is a finding.

## Scenario to gate map

| Scenario | Proves | Hard assertion |
|---|---|---|
| LT-1 | baseline latency/throughput on the real model | stable latency, error rate < 0.5% (non-fault) |
| LT-2 | EPP beats round-robin where it should | prefix-heavy: EPP TTFT p90 lower AND prefix-hit higher; interactive-short: no worse |
| LT-3 | idempotency under load | distinct-complete == N AND worker_claims_total delta == N exactly, zero lost |
| LT-4 | SLA-tier isolation | interactive p95 TTFT within target and within ~10-15% of LT-1 under a sustained batch flood; tiers on separate nodes |
| LT-5 | autoscaling | scalers fire, no silent zero, scale-up within the node-provisioning envelope, batch scales to zero |
| LT-6 | durability | zero jobs lost under worker kill + node drain; re-runs bounded by interrupted jobs only |
| LT-7 | graceful degradation | interactive EPP down -> clean 5xx; batch EPP down -> clean 5xx (both FailClose: the gateway does not honor FailOpen for InferencePool, DESIGN.md invariant 6); both recover |

## Running (once the cluster is scaled and warm)

```
# install client deps on the loadgen host / node group
pip install -r loadtest/requirements.txt

# LT-3 idempotency (the headline). Records claims delta and complete count.
make loadtest-lt3            # drives N distinct jobs x D duplicates, then analyze.py lt3

# LT-2 ran on 2026-07-21 on the pre-relay topology and is kept as a record (loadtest/run_lt2.sh
# stops on the current one). For the Endpoint Picker versus round-robin comparison today, use
# loadtest/run_exp3.sh: both arms enter through the same gateway from the in-cluster driver.
```

See the repo Makefile `loadtest-*` targets. Each target sets GATEWAY / SUBMIT_URL /
WORKLOAD / TIER / N_JOBS / DUP and points Locust at the `loadgen` node group.
