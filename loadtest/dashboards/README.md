# Grafana access and load-test dashboards

How to open the dashboards, find each load-test result by its time range, and view the committed
raw series. Grafana is reached by port-forward, not a public endpoint.

## Access

Grafana runs in the cluster (kube-prometheus-stack). Reach it locally with a port-forward:

```
kubectl -n monitoring port-forward svc/kps-grafana 3000:80
```

Then open http://localhost:3000. Retrieve the admin credentials from the Kubernetes secret (not
stored in this repo):

```
kubectl -n monitoring get secret kps-grafana -o jsonpath='{.data.admin-user}'     | base64 -d ; echo
kubectl -n monitoring get secret kps-grafana -o jsonpath='{.data.admin-password}' | base64 -d ; echo
```

Do not expose Grafana with a public LoadBalancer or Ingress on this account. The port-forward is
the intended access path.

## Dashboards

All four are provisioned into Grafana as ConfigMaps (label `grafana_dashboard=1`, namespace
`monitoring`) and use the Prometheus datasource by uid `prometheus`, which is this cluster's
default Grafana datasource uid, so they render on a fresh import with no datasource remap. Every
panel was verified to resolve series against the running Prometheus (`verify_dashboards.py`).

| Dashboard | uid | Local link | Panels rendering |
|---|---|---|---|
| Inference-Aware EKS: Proof | `inference-aware-proof` | http://localhost:3000/d/inference-aware-proof | 11/11 |
| Inference-Aware EKS: Request Journey | `inference-request-journey` | http://localhost:3000/d/inference-request-journey | 3/3 |
| Inference-Aware EKS: vLLM serving | `inference-vllm` | http://localhost:3000/d/inference-vllm | 6/6 |
| Inference-Aware EKS: DCGM GPU | `inference-dcgm-gpu` | http://localhost:3000/d/inference-dcgm-gpu | 6/6 |
| Inference-Aware EKS: Request Flow | `inference-request-flow` | http://localhost:3000/d/inference-request-flow | 13/13 timeseries (3 stat panels use `$__range` and render only in Grafana) |

Source and provisioning: `proof.json` / `proof-dashboard-configmap.json` (built by
`build_proof_dashboard.py`); `request-journey.json` / `request-journey-configmap.json`; `vllm.json`
/ `vllm-configmap.json`; `dcgm-gpu.json` / `dcgm-gpu-configmap.json` (the last three built by
`build_dashboards.py`); `request-flow.json` / `request-flow-configmap.json` (built by
`build_request_flow.py`). Re-apply a ConfigMap after regenerating:
`kubectl apply -f loadtest/dashboards/<name>-configmap.json`.

### Request Flow dashboard (the two paths on one timeline)

The Request Flow dashboard shows both request paths on one shared time axis so the difference is
visible, not asserted. Four rows, each with a one-line "expect to see":

- Asynchronous path (long requests through the durable SQS queue to the batch tier): submissions
  per second rise, SQS depth builds to a peak and drains to zero, worker claims per second track
  the drain, batch tier requests running and completions per second follow.
- Synchronous path (short requests straight through the gateway to the interactive tier):
  interactive request rate and requests running rise, interactive TTFT p95 stays flat (about 76 ms)
  throughout, and the "Async-path metrics stay zero for synchronous traffic" panel shows the
  async submissions, SQS depth, and worker claims sitting at zero except during the async burst.
  That panel is the differentiation: the async instruments do not move for sync load.
- Reconciliation over the selected window: five stat panels (submitted, claimed, batch completions,
  queue visible now, queue in flight now). The three counter stats use `increase(...[$__range])`,
  which Grafana substitutes with the panel's time range; they render only in Grafana, not in a
  direct Prometheus query.
- GPU utilization by tier: batch tier average, interactive tier average, and per-GPU (legend
  `{{hostname}} gpu{{gpu}} {{exported_pod}}`), so it is clear which fleet does the work in each lane.

The queue-depth series here is the REAL queue state, not the vLLM waiting-requests proxy the
Request Journey dashboard uses. It comes from a direct SQS exporter (`async-worker/sqs_exporter.py`,
Deployment `sqs-exporter`) that polls `GetQueueAttributes` every 15 seconds under the async-worker
service account (read-only, `GetQueueAttributes` only) and exposes two gauges:
`sqs_queue_messages_visible` (waiting) and `sqs_queue_messages_in_flight` (being processed), scraped
by the `sqs-exporter` PodMonitor. Because the poll interval is 15 seconds and SQS
`ApproximateNumberOfMessages` is eventually consistent, the plotted depth can lag a sudden burst by
up to about a poll interval plus the SQS convergence time; it is exact at steady state. The
authoritative per-run depth trend is the committed CSV `loadtest/results/request_flow_depth.csv`.

Reconciliation identities (proven by `loadtest/verify_reconciliation.py` over the Phase D window):

- Exact (from durable DynamoDB state, which survives worker scale-down):
  - distinct submitted == distinct completed.
  - sum(claim_count) == distinct + re-claims of interrupted jobs.
  - synchronous traffic contributes zero to the async path (SQS depth, worker-claims rate, and
    submissions rate are all zero during the synchronous-only lead-in).
  - batch completions (raw counter delta, end minus start) == durable claims.
  - interactive completions + batch completions == total completions.
  - final SQS depth == 0.
- Approximate (tolerance and cause stated, never widened to force a pass):
  - the instant mid-burst SQS visible depth lags submitted minus completed minus in-flight, because
    SQS `ApproximateNumberOfMessages` is eventually consistent; the identity holds exactly at steady
    state (net queue change over the window is zero).
  - `increase(worker_claims_total)` is a per-pod Prometheus counter and under-counts when KEDA
    scales workers down mid-window (terminated pods lose their counters); it is a documented
    cross-check, not the authoritative claim count. The authoritative count is DynamoDB `claim_count`.

## Retention

Prometheus retention is 10 days (`kubectl -n monitoring get prometheus -o
jsonpath='{.items[0].spec.retention}'`). Every load-test scenario ran within the last three days,
so all scenario data is queryable in Prometheus. Nothing has aged out. A dashboard opened at the
right time range shows that scenario's live historical data. The metrics per tier
(`vllm:*{tier=...}`) exist only for the intervals the tier's pods were running; the DCGM GPU
metrics exist only while GPU nodes were up. Both are present for the run windows below.

## Scenario to dashboard to time range (one-click preset links)

Each link opens the mapped dashboard with the scenario's time range already set (the `from` and
`to` are epoch milliseconds; the port-forward must be running). Click, then read the "expect to
see" column so a wrong panel is obvious. All times UTC.

| Scenario | Preset link | Expect to see |
|---|---|---|
| LT-2 routing, EPP vs round-robin (HEADLINE) | http://localhost:3000/d/inference-vllm?from=1784615220000&to=1784621220000 | Prefix-cache hit rate for interactive reaching about 98% during the EPP arm and dropping to about 55% during the round-robin arm (committed: 97.9% vs 54.7%). |
| LT-2 routing (Proof view) | http://localhost:3000/d/inference-aware-proof?from=1784615220000&to=1784621220000 | Same prefix-cache swing, plus interactive TTFT rising when the hit rate falls. |
| LT-3 idempotency (batch async) | http://localhost:3000/d/inference-aware-proof?from=1784617620000&to=1784619420000 | worker_claims_total (real GPU runs) increasing in a single step per distinct job, and batch running > 0. |
| LT-4 isolation / LT-5 autoscaling / LT-7 failure | http://localhost:3000/d/inference-aware-proof?from=1784638020000&to=1784644020000 | Interactive TTFT staying low (about 40 to 76 ms) while batch is under load; queue depth and ready replicas rising then falling during autoscaling. |
| Phase C queue build and drain | http://localhost:3000/d/inference-request-journey?from=1784702640000&to=1784703120000 | Queue depth rising then draining to zero. See the queue-depth note below. |
| Phase D traced routing (Request Journey) | http://localhost:3000/d/inference-request-journey?from=1784703120000&to=1784704200000 | Batch and interactive request rates both present, interactive TTFT flat. |
| Phase D traced routing (vLLM) | http://localhost:3000/d/inference-vllm?from=1784703120000&to=1784704200000 | Per-tier requests running, tokens/sec, and completed requests for both tiers. |
| LT-6 durability, clean 300/300 (vLLM) | http://localhost:3000/d/inference-vllm?from=1784712840000&to=1784713920000 | Batch requests running spiking to about 14, tokens/sec active, completed requests draining to zero. |
| LT-6 durability (GPU) | http://localhost:3000/d/inference-dcgm-gpu?from=1784712840000&to=1784713920000 | Two of the four GPUs at 100% (aggregate about 50%), about 40 GB framebuffer held, temperature and power rising under load. |
| Request Flow, both paths at once (differentiation + reconciliation) | http://localhost:3000/d/inference-request-flow?from=1784734638000&to=1784734938000 | Async lane: SQS depth building to a peak (about 195) and draining to zero, submissions and worker claims tracking it, batch running capped at 4. Sync lane: interactive rate and running present, interactive TTFT p95 flat at about 76 ms, and the "async-path metrics stay zero for synchronous traffic" panel at zero except during the async burst. GPU-by-tier: both fleets working. Run tag `flow-1784734625`. |

The LT-1 through LT-7 boundaries are bracketed from the Prometheus vLLM request-rate series for
2026-07-21 (those were synchronous runs without an epoch run tag). The authoritative measured
numbers for every scenario are in `LOAD-TEST-RESULTS.md`; the links above are for viewing the
activity. Phase C, Phase D, and LT-6 have exact epoch run tags (`phasec-1784702782`,
`phased-1784703280`, `lt6-1784712970`). Verified against live Prometheus: the LT-2 window renders
the prefix-cache hit rate at 0.979 (EPP) and about 0.55 (round-robin), matching the committed
97.9% and 54.7%.

Queue-depth note (Phase C): the Request Journey "Queue depth" panel's SQS-depth series
(`keda_scaler_metrics_value`) shows no data on this build, because the KEDA operator metrics are
not scraped into Prometheus (adding a ServiceMonitor is an infrastructure change, out of scope for
a dashboards-only pass). The panel instead shows the in-cluster vLLM batch waiting queue as a live
proxy (peak about 35). The authoritative SQS queue depth for Phase C (build to 57, drain to 0) is
the committed file `loadtest/results/phasec_queue_depth.csv`. Open that CSV to see the true depth.

Non-scenario activity: a burst on 2026-07-22 from about 10:10 to 10:25 UTC (about 4 concurrent
requests, 0.8 requests/sec, batch tier) is NOT a load-test scenario. It is the dashboard
verification and populate run (40 jobs with client_id `grafana-live`, confirmed in DynamoDB) used
to confirm the live panels render. Ignore it when reading scenario evidence.

## Live in Prometheus versus committed as files

- Live in Prometheus (within retention, viewable in Grafana at the ranges above): all scenarios.
- Committed raw series (the durable record, independent of retention):
  - `loadtest/results/phasec_queue_depth.csv` (Phase C SQS queue depth, sampled from SQS).
  - `loadtest/results/request_journey_timeseries.{csv,json}` (per-tier request rate and interactive
    TTFT for the Phase C and Phase D window).
  - `loadtest/results/phased_trace.{md,json}` (Phase D per-request trace: correlation id, backend
    pod, tier, node, accelerator, including the interrupted-and-recovered row).
  - `loadtest/results/request_flow_depth.csv` (Request Flow combined-demo SQS depth trend: elapsed
    seconds, visible, in-flight, complete; the authoritative queue-depth series for the run, peak
    visible 195, drained to 0).

When a scenario eventually ages out of the 10-day Prometheus retention, view it from these
committed files (open the CSV, or the JSON series) rather than the dashboards. The trace table
(`phased_trace.md`) renders directly as Markdown.
