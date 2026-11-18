# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Async inference worker with State-Backed Retry Suppression.

This is Layer 2 of the inference-aware EKS architecture: the durable front door
that llm-d does not provide. It is the headline contribution of the design, so
its correctness is load-bearing and is proven by tests/validation/test_idempotency.py.

The core guarantee: a duplicate submission of the same logical job never runs
inference twice on a GPU, even though Amazon SQS standard queues are at-least-once
and can redeliver a message. Deduplication happens with a DynamoDB conditional
write at claim time, not at enqueue time. Enqueue-time dedup alone is insufficient,
because a single enqueued message can still be delivered twice.

The claim, key, and state-transition primitives live in jobstore.py, shared with the
orphan sweeper (sweeper.py) so both use identical logic. This module adds the vLLM
call and the SQS receive/delete loop.

State machine per job:
    (absent) --claim--> processing --success--> complete
                              |
                              +----failure-----> failed (retryable after lease)

An orphan (processing, lease expired, no live worker, SQS message gone) is recovered
by the sweeper, which re-enqueues a message so a worker re-claims via the stale-claim
steal path. See DESIGN.md section 3.4.

Environment:
    SQS_QUEUE_URL          the inbound job queue
    DDB_TABLE              the idempotency and job-state table (default: inference-jobs)
    VLLM_ENDPOINT          the in-cluster vLLM service, e.g. http://vllm-batch:8000/v1
    CLAIM_TTL_SECONDS      how long a claim lease is valid before it is stealable
    RETENTION_SECONDS      DynamoDB item retention (decoupled from the lease)
"""

import json
import logging
import os
import time

import requests

import message_contract as mc
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from prometheus_client import Counter, start_http_server

import jobstore
from jobstore import (
    SQS_QUEUE_URL,
    ddb,
    job_key,
    mark,
    now_epoch,
    release,
    sqs,
    try_claim,
)


class InfraRejection(Exception):
    """The gateway rejected the request for an infrastructure reason (a 5xx from an Endpoint
    Picker outage, or a connection error), not a fault of the job itself. Handled distinctly
    from a genuine job failure: the job is not marked failed, and the worker backs off."""


class BackoffSignal(Exception):
    """Raised by handle() to tell the poll loop to back off after an infra rejection, without
    marking the job failed or deleting its message."""

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("async-worker")

# The worker submits each job to the SAME inference-aware gateway the synchronous path
# uses, carrying the tier header, so every request (sync or async) passes through the
# identical Endpoint-Picker routing path and the only difference is the queue in front.
# GATEWAY_ENDPOINT is the in-cluster Envoy gateway (ending in /v1). Calling a plain vLLM
# Service instead would round-robin across pods and bypass the Endpoint Picker.
GATEWAY_ENDPOINT = os.environ["GATEWAY_ENDPOINT"].rstrip("/")
INFERENCE_OBJECTIVE = os.environ.get("INFERENCE_OBJECTIVE", "batch")  # tier header value
# Optional extra routing header for measurement arms (LT-8): when PUB_ARM is set, the worker adds
# `x-pub-arm: <PUB_ARM>` alongside (never replacing) the tier objective header. `x-pub-arm: rr`
# selects the cache-blind batch round-robin route; unset or any other value uses the Endpoint Picker
# via the normal batch rule. This does NOT touch INFERENCE_OBJECTIVE, so INV9 stays true.
PUB_ARM = os.environ.get("PUB_ARM", "")

# One reused HTTP connection pool (keep-alive) for ALL inference calls, instead of a new TCP
# connection per request. A bare requests.post opens and closes a connection every call; across
# long-lived worker processes under sustained load that accumulates TIME_WAIT sockets and ephemeral
# port pressure, which shows up as intermittent connection-setup stalls (DEFECT-worker-latency.md,
# hypothesis H2). The pool is sized to the worker's in-flight concurrency (one poll loop per pod).
#
# Retry policy (the read-versus-connect distinction is the whole point, so it is spelled out):
#   connect=2  retry a CONNECT error. The request never reached the server, so a retry is safe. This
#              also recovers a stale keep-alive connection that the gateway closed on its idle timeout
#              (the gateway idle_timeout is 3600s, verified from config_dump), which reuse would
#              otherwise surface as a connect-side failure.
#   read=0     NEVER retry a READ error. The generation may already have started on a GPU, so a retried
#              POST could double-execute and break the exactly-once guarantee FROM INSIDE THE CLIENT,
#              which is the one thing this system must not do. A read error surfaces to run_inference as
#              a RequestException and becomes an InfraRejection (release + redeliver), handled once.
#   status=0   do NOT retry on 5xx. A gateway 5xx (an Endpoint Picker outage) must surface to us as an
#              InfraRejection, not be silently re-POSTed by the client (same double-execute risk).
# allowed_methods={POST}: POST is the only method the worker issues; it is retryable for CONNECT only
#              because read/status are 0.
_RETRY = Retry(total=2, connect=2, read=0, status=0, backoff_factor=0.2, allowed_methods=frozenset({"POST"}))
_ADAPTER = HTTPAdapter(pool_connections=4, pool_maxsize=16, max_retries=_RETRY)
_HTTP = requests.Session()
# Plain HTTP inside the cluster to the gateway ClusterIP, documented under README Security considerations, No TLS.
_HTTP.mount("http://", _ADAPTER)  # nosemgrep
_HTTP.mount("https://", _ADAPTER)
# The HTTP client timeout is DECOUPLED from the claim lease (CLAIM_TTL_SECONDS). Per the
# timeout-chain ordering rule (ARCHITECTURE-REVIEW.md R.5), it must exceed the gateway route
# timeout (600s), so the worker receives the gateway's verdict instead of giving up first, and
# stay below the claim lease, so the claim does not lapse while the worker is legitimately still
# waiting: route 600s < HTTP client 660s < claim lease 750s < SQS visibility 900s.
HTTP_TIMEOUT_SECONDS = int(os.environ.get("HTTP_TIMEOUT_SECONDS", "660"))
# Response header (if any) by which the gateway reports the selected upstream endpoint.
# The authoritative backend capture is the Envoy access log (correlation-id to upstream
# host); this header is an opportunistic in-band capture when the stack emits it.
TRACE_BACKEND_HEADER = os.environ.get("TRACE_BACKEND_HEADER", "x-gateway-destination-endpoint")
WAIT_SECONDS = int(os.environ.get("SQS_WAIT_SECONDS", "20"))  # long poll
# Default 1: handle() processes each received message sequentially, blocking up to
# HTTP_TIMEOUT_SECONDS, so a receive batch > 1 lets messages 2..N wait behind message 1 (in-batch
# head-of-line blocking) for long generations (DEFECT-worker-latency.md, hypothesis H1). This worker
# serves long, non-interactive work, so 1 is correct. Tradeoff: more receive calls (slightly higher
# SQS request volume and long-poll overhead) in exchange for no in-batch stalls. Raise only for
# short-job workloads.
BATCH_SIZE = int(os.environ.get("SQS_BATCH_SIZE", "1"))
METRICS_PORT = int(os.environ.get("METRICS_PORT", "9100"))
# On an infrastructure rejection (a batch Endpoint Picker outage returns a gateway 5xx), the
# worker must NOT consume the message. It leaves the message untouched, so the message keeps the
# queue's full VisibilityTimeout (900s, set in setup-async-infra.sh) before it redelivers. That
# is the correct bounded backoff: roughly maxReceiveCount x visibility to the DLQ, about 60 to 75
# minutes, long enough to ride out an Endpoint Picker outage.
#
# It deliberately does NOT call ChangeMessageVisibility. That API sets the ABSOLUTE remaining
# invisibility from now, so any value below the queue default would SHORTEN it and burn toward the
# DLQ faster, the opposite of a backoff. That was defect B.1: a 120s "backoff" against a 900s
# queue cut the DLQ horizon from about 60 to 75 minutes down to 8 to 10. The only backoff here is
# the poll loop sleeping WORKER_BACKOFF_SECONDS locally so it does not hammer the gateway.
WORKER_BACKOFF_SECONDS = int(os.environ.get("WORKER_BACKOFF_SECONDS", "5"))

# Scale-accurate GPU-run count. Incremented EXACTLY ONCE per real GPU invocation
# (a won claim, immediately before run_inference). The load-test idempotency
# scenario asserts the delta over a run equals N (one run per distinct job), so
# this must never be incremented on a duplicate/superseded path. Summed across all
# worker replicas by Prometheus: sum(worker_claims_total). The DURABLE equivalent
# that survives a pod kill is the DynamoDB claim_count attribute (see jobstore).
WORKER_CLAIMS = Counter(
    "worker_claims_total",
    "GPU inference invocations after a successful, still-owned claim",
)

# Infra rejections observed by this worker (a batch Endpoint Picker outage 5xx or an unreachable
# gateway). WORKER_CLAIMS increments at claim time, before the gateway call, so it counts an
# infra-rejected attempt that never reached a GPU. This counter is what you subtract to recover
# real GPU runs: sum(worker_claims_total) - sum(worker_infra_rejections_total). The DURABLE
# equivalents that survive a pod kill are the DynamoDB claim_count and infra_rejections attributes.
WORKER_INFRA_REJECTIONS = Counter(
    "worker_infra_rejections_total",
    "Claimed attempts rejected for an infrastructure reason (released, not run, not failed)",
)


def _safe_to_drop(key: str) -> bool:
    """Whether an SQS message can be deleted after a lost claim without stranding the
    job. True if the job is complete, or validly owned by a live worker (processing
    with a lease still in the future). False if the job is orphaned (processing with an
    expired lease: the owner died and this claim lost a race) or a retryable failure,
    so the message is kept for the next redelivery to re-claim."""
    item = jobstore.get_item(key)
    if not item:
        return False
    status = item.get("status", {}).get("S")
    if status == "complete":
        return True
    lease = int(item.get("lease_expires", {}).get("N", "0"))
    return status == "processing" and lease > now_epoch()


def run_inference(payload: dict, correlation_id: str, path: str | None = None,
                  capability: str | None = None) -> tuple[dict, str]:
    """Submit the job through the inference-aware gateway (Envoy plus the Endpoint
    Picker), carrying the tier objective header so the batch Endpoint Picker selects the
    backend by live GPU state, and the correlation-id header so the request is traceable
    across the gateway access log. Blocking by design: this worker serves async,
    non-interactive traffic, so holding the call here is correct. The SQS visibility
    timeout must exceed the max generation time. Returns (result, backend_endpoint), where
    backend_endpoint is the selected upstream if the gateway reports it in a response
    header (empty string otherwise; the Envoy access log is the authoritative source)."""
    try:
        resp = _HTTP.post(
            # THE ROUTE COMES FROM THE MESSAGE when the producer supplied one, so a promoted
            # /v1/completions request is re-submitted as /v1/completions. Defaulting to
            # /chat/completions is kept for messages from the submit API, which has always sent
            # chat-shaped payloads. Re-submitting a request in a DIFFERENT SHAPE than it arrived is
            # not a re-placement, it is a different request that happens to reach the same model.
            f"{GATEWAY_ENDPOINT.rstrip('/v1')}{path}" if path else f"{GATEWAY_ENDPOINT}/chat/completions",
            json=payload,
            headers={
                "x-gateway-inference-objective": INFERENCE_OBJECTIVE,
                "x-correlation-id": correlation_id,
                # THE CAPABILITY COORDINATE, CARRIED TO THE WIRE. Added 2026-09-21 and it closes a
                # real defect rather than adding a feature. promotion.promoted_pair() holds
                # capability FIXED and test_promotion asserts that as property 2 ("if it could
                # lower capability, a request that ran too long would silently receive a weaker
                # model"), but until now NOTHING carried it here: the worker sent bare `batch`, so
                # a promoted premium job matched the SLA-only route and was served by the STANDARD
                # pool while its promotion record said premium. The invariant was enforced one
                # layer above the wire.
                #
                # Harmless until now only because premium was unroutable and economy-on-batch is
                # unwired, so every promoted job WAS standard. Premium is the first class that
                # makes the gap observable.
                #
                # ADDITIVE, exactly as PUB_ARM is: it sits ALONGSIDE and never replaces
                # INFERENCE_OBJECTIVE, so INV9 (the worker routes through the gateway carrying the
                # batch objective) stays true by the same argument.
                **({"x-inference-model-class": capability} if capability else {}),
                # x-pub-arm added only when PUB_ARM is set (LT-8); absent otherwise.
                **({"x-pub-arm": PUB_ARM} if PUB_ARM else {}),
            },
            timeout=HTTP_TIMEOUT_SECONDS,
        )
    except requests.exceptions.RequestException as e:
        # Connection error or client timeout: the gateway is unreachable, not the job's fault.
        raise InfraRejection(f"gateway unreachable: {e}") from e
    if resp.status_code >= 500:
        # A gateway 5xx: an Endpoint Picker outage (FailClose) or a data-plane error. Not the
        # job's fault, and a genuinely long generation is never a 5xx (it is a 200 or a 504
        # route timeout, and the 504 is itself an infra condition, not a bad request).
        raise InfraRejection(f"gateway {resp.status_code}")
    resp.raise_for_status()  # a 4xx is a genuine bad request for this job (a real failure)
    return resp.json(), resp.headers.get(TRACE_BACKEND_HEADER, "")


def handle(message: dict) -> None:
    body = json.loads(message["Body"])
    client_id = body["client_id"]
    payload = body["payload"]
    key = job_key(client_id, payload)
    # Correlation ID for end-to-end tracing IS the deterministic job_id (= key). It is
    # present in the message, is the DynamoDB primary key, and is sent as a request header
    # to the gateway. submitted_at is the first trace timestamp (submit API stamped it).
    submitted_at = body.get("submitted_at", "")

    # Persist client_id + payload on the claim write so a message-less orphan can be
    # reconstructed by the sweeper.
    token = try_claim(key, client_id=client_id, payload=payload, routing=mc.routing_of(body))
    if token is None:
        # The claim did not win. Delete this SQS copy ONLY if the job is genuinely
        # done or validly owned by a live worker. If the job is ORPHANED (status
        # processing but the lease has expired, i.e. the owner died and this claim
        # merely lost a race), do NOT delete: leave the message so the next
        # redelivery re-claims it. Deleting an orphaned job's message would strand it
        # in `processing` until the sweeper re-enqueues it (the LT-6 durability gap).
        if _safe_to_drop(key):
            log.info("duplicate suppressed job_id=%s", key)
            _delete(message)
        else:
            log.warning("orphaned job_id=%s (lease expired, owner gone); leaving for redelivery", key)
        return

    log.info("claimed job_id=%s", key)
    try:
        # Count the actual GPU invocation EXACTLY ONCE, here, after a won claim and
        # immediately before the call. This is the scale-accurate GPU-run counter
        # the idempotency load test asserts against (delta == N for N jobs).
        WORKER_CLAIMS.inc()
        # `capability` is a SIBLING of payload on the message, like correlation_id and path, so
        # job_key(client_id, payload) is unchanged and the dedup hash (and therefore exactly-once)
        # is untouched. ABSENT MEANS STANDARD: every message written before this field existed
        # keeps its current routing exactly.
        result, backend = run_inference(payload, correlation_id=key, path=body.get(mc.PATH),
                                        capability=body.get(mc.CAPABILITY))
        # Persist the per-request trace on the terminal write (Phase B.3): the submission
        # and completion timestamps and the selected backend the gateway reported. The
        # backend pod, tier, node, and accelerator are resolved at trace-build time by
        # joining the correlation id against the Envoy access log (loadtest/build_trace.py).
        # Guarded by claim ownership: a superseded worker does not clobber the new owner.
        trace = {
            "result": json.dumps(result)[:300000],
            "submitted_at": str(submitted_at),
            "backend_endpoint": backend,
            "backend_selected_at": str(now_epoch()),
            "completed_at": str(now_epoch()),
        }
        if mark(key, "complete", token, trace):
            log.info("complete job_id=%s backend=%s", key, backend or "(via access log)")
        else:
            log.warning("completed but superseded job_id=%s (owner keeps state)", key)
    except InfraRejection as e:
        # NOT the job's fault (a batch Endpoint Picker outage returns a gateway 5xx, or the
        # gateway is unreachable). Do NOT mark the job failed: that would push an otherwise
        # healthy job to the DLQ after maxReceiveCount infra-rejected redeliveries. Instead
        # release the claim (so the job is re-claimable and the rejection is recorded durably in
        # infra_rejections), and leave the SQS message UNTOUCHED so it keeps the queue's full
        # visibility before redelivery (the correct long backoff; see the note at
        # WORKER_BACKOFF_SECONDS on why touching visibility here would shorten it). The poll loop
        # then sleeps WORKER_BACKOFF_SECONDS. This is the F1 non-consuming failure path
        # (ARCHITECTURE-REVIEW.md 2.2).
        release(key, token)
        WORKER_INFRA_REJECTIONS.inc()
        log.warning("infra rejection job_id=%s (not marked failed; claim released, message untouched): %s", key, e)
        raise BackoffSignal() from e
    except Exception as e:  # noqa: BLE001
        # A GENUINE job failure (a 4xx bad request, or a bad result). Retryable after the lease:
        # try_claim allows a re-claim once status=failed and the lease has passed. The mark is
        # ownership-guarded, so a superseded worker will not overwrite the new owner.
        mark(key, "failed", token, {"error": str(e)[:1000]})
        log.warning("failed job_id=%s error=%s", key, e)
        raise  # leave message on queue for SQS redelivery / DLQ
    _delete(message)  # terminal (complete or superseded-complete): drop this copy


def _delete(message: dict) -> None:
    sqs.delete_message(
        QueueUrl=SQS_QUEUE_URL,
        ReceiptHandle=message["ReceiptHandle"],
    )


def poll_loop() -> None:
    # Expose worker_claims_total for Prometheus (scraped via the worker PodMonitor).
    start_http_server(METRICS_PORT)
    log.info(
        "worker up queue=%s gateway=%s objective=%s metrics=:%d",
        SQS_QUEUE_URL, GATEWAY_ENDPOINT, INFERENCE_OBJECTIVE, METRICS_PORT,
    )
    while True:
        resp = sqs.receive_message(
            QueueUrl=SQS_QUEUE_URL,
            MaxNumberOfMessages=BATCH_SIZE,
            WaitTimeSeconds=WAIT_SECONDS,  # long poll, no busy spin
            AttributeNames=["ApproximateReceiveCount"],
        )
        for message in resp.get("Messages", []):
            try:
                handle(message)
            except BackoffSignal:
                # Infra rejection: the job was released and its message backed off in handle().
                # Sleep so the worker does not hammer the gateway during an outage.
                time.sleep(WORKER_BACKOFF_SECONDS)
            except Exception as e:  # noqa: BLE001
                # A genuine job failure, already marked failed in handle(); message stays for
                # redelivery until maxReceiveCount, then the DLQ.
                log.warning("handler error, leaving on queue: %s", e)


if __name__ == "__main__":
    poll_loop()
