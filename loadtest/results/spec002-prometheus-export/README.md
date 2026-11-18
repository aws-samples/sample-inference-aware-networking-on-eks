# Prometheus export for the Qwen3 measurement window

Captured 2026-09-25, immediately BEFORE cluster teardown, because Prometheus in this cluster has
10-day retention and no persistent volume, so the raw series would have been destroyed with the
cluster. The committed cell JSONs carry the aggregates; this is the underlying time series, so the
blog's figures can be redrawn from data rather than from a panel definition.

Window: **2026-09-24T07:30Z to 2026-09-25T10:10Z**, covering experiment 3 (four regimes), experiment
4 (overflow ramp), the queue path, the Neuron recovery cells and the dense rho sweep.

| Series | Series count | Points | What it supports |
|---|---|---|---|
| `vllm_prefix_cache_hit_rate` | 22 | 9,273 | the regime and ramp hit rates, per replica |
| `vllm_running_requests` | 22 | 9,179 | slot occupancy, the cancellation and promotion claims |
| `vllm_waiting_requests` | 22 | 9,179 | queue-side admission pressure |
| `sqs_queue_messages_visible` | 1 | 1,601 | queue depth during the queue-path run |
| `sqs_queue_messages_in_flight` | 1 | 1,601 | in-flight jobs, the exactly-once arithmetic |

Each series is written twice: `.json` (raw Prometheus `query_range` response, including labels) and
`.csv` (flat `timestamp_utc,labels,value`). `MANIFEST.json` records the exact PromQL for each.

## NOT captured, and it does not exist anywhere

**GPU utilisation for the Qwen3 series.** The DCGM exporter has **zero** metrics in this Prometheus:
`__name__` contains no `DCGM` series at all, so utilisation was never scraped during the Qwen3 work.
The Llama publication run has `loadtest/results/publication/nvidia_gpu_util.txt`; there is no Qwen3
equivalent and none can be produced now.

This is recorded as a **gap in the measurement, not a loss at teardown**. Any blog statement about
Qwen3 accelerator utilisation would have no data behind it and must not be made.

Neuron metrics are likewise absent (`neuron` matches no series), consistent with the Neuron arm
having been closed as characterization.
