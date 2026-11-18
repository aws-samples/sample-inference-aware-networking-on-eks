# The driver's vantage moved in-cluster (2026-09-23)

**Stated difference, recorded rather than buried** (the maintainer's condition): the ramp's load driver now
runs INSIDE the cluster, where the Llama series drove from a laptop against the gateway's external
address. **Absolute latencies therefore shift slightly** between the two series.

Harmless for this experiment, and why:

1. These are **new-series numbers** under the comparability rule; no Qwen3 figure is presented as a
   continuation of the Llama series.
2. The ramp's metric is the **hit-rate curve against rho**, computed from the serving replicas' own
   `vllm:prefix_cache_*` counters, which are indifferent to where the client sits.
3. e2e latency is recorded per cell as a secondary number and is comparable **within** this series.

## Why it had to move

The ramp's independent variable is ARRIVAL ORDER (grouped versus interleaved admission). A laptop
driver behind a `kubectl port-forward` serialises arrivals through one tunnel, so it would measure
the tunnel rather than the router. Today's rebuilt cluster also exposes the gateway as a ClusterIP
only, so a laptop driver could not reach it at all: 48 of 48 warm-up requests timed out and the
ramp's own preflight gate stopped the run before it measured anything. That gate working is why
there is no contaminated data to discard.

## What the driver is, and what it deliberately is not

`loadtest/incluster-driver.yaml`: one ephemeral Pod, labelled
`inference-aware-eks/ephemeral: "true"`, deleted after the run, like the promotion-stub precedent.

**It holds no RBAC and no ServiceAccount token** (`automountServiceAccountToken: false`). It needs
only two plain-HTTP things from inside the cluster: the gateway by DNS, and each replica's
`/metrics`. The replica addresses arrive as `POD_ENDPOINTS` from the orchestrator, which already has
kubectl on the operator's machine. **A driver that cannot call the Kubernetes API cannot perturb
what it measures**, which is stronger than the minimal-role condition asked for.

`cluster_probe` gained one branch rather than a copy: with `POD_ENDPOINTS` set it scrapes those
endpoints over HTTP; unset, it uses the original `kubectl exec` path unchanged. Four scrapers share
that function, and the comment above it records why they were single-sourced in the first place.
