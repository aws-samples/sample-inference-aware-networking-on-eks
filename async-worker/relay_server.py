# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
The interactive front-door relay, as a server.

WHAT IT IS FOR. After the Phase 2 cutover the relay is the SOLE externally reachable interactive
entry, and the gateway data plane is cluster-internal (INV12). That is not a packaging preference:
while the gateway is directly addressable, a client can reach the interactive default rule
unclassified and bypass classification, the reservation guard and the upgrade-rate cap, with no
failure and no alert. Closing that bypass requires exactly one door, which is this.

WHAT IT DOES, and deliberately nothing more:
  1. Classifies on free signals, in process (relay.classify).
  2. Applies the section 7 guards to the result (guards.Guards.decide).
  3. Sets the objective header and forwards to the gateway's ClusterIP.
  4. Streams the response back.

WHAT IT DOES NOT DO. It does not pick a replica: the Endpoint Picker does that from live GPU state
inside whichever pool the relay names. It does not retry (see relay.make_relay_session, and the
reasoning there is the reason this module has no retry logic of its own either). It does not
authenticate callers. This sample leaves authentication to the deployer: put your organization's
identity layer in front of the relay before any use, and keep the network controls in
manifests/08-relay/relay.yaml either way. The caller is the UNAUTHENTICATED `x-caller-id` header,
used only to keep different callers' promoted jobs apart; it is a label, not an identity.

THE STANDING PROPERTY: A RELAY FAILURE MUST NOT BECOME A CLASSIFICATION FAILURE, AND NEITHER MUST
BECOME AN OUTAGE. Classification already fails open to the floor. The guards already degrade to the
floor. So every path through this handler that can go wrong before the upstream call ends with a
request being forwarded at the floor rather than with an error returned to the caller. The only
errors this server originates are ones where forwarding is impossible, plus the RESOURCE BOUNDS
below, which protect the relay itself rather than judge the request.

RESOURCE BOUNDS (added in release candidate 7, deep security scan finding M1). Without them one
oversized request from any host allowed to reach the relay could exhaust the pod's memory. Each
bound applies before the bytes it bounds are held, and each refusal names itself in
`x-relay-reason` (where a reply is sent) and in `relay_requests_bounded_total{reason}`:
  - connections per replica: RELAY_MAX_CONNECTIONS (503 from the accept loop);
  - request line and headers: RELAY_HEADER_BUDGET_BYTES and RELAY_HEADER_DEADLINE_SECONDS (the
    connection is closed), and a malformed header block (400);
  - body size, framing and read deadline: http_bounds.read_body (413, 400, 411, 408);
  - requests being classified or forwarded: RELAY_MAX_IN_FLIGHT (503 with Retry-After), taken
    only after the body has arrived, so a slow upload cannot hold one;
  - client socket timeout per read or write: RELAY_CLIENT_SOCKET_TIMEOUT_SECONDS.
They bound memory per replica, not callers: one host that can reach the relay can still occupy
its connection slots. Per-caller rate limits belong in the identity layer the deployer adds in
front of the relay.
"""
import json
import re
import os
import sys
import threading
import time

import requests
from http.server import BaseHTTPRequestHandler

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, generate_latest

from guards import Guards, KillSwitch, UpgradeRateLimiter
import http_bounds
import promotion as promo
import message_contract as mc
from relay import (
    CAPABILITY_HEADER,
    SLA_HEADER,
    classify,
    make_relay_session,
)

# Chain B's R, sourced from the chain-b-timeouts ConfigMap rather than restated. The default here is
# a fallback for local runs only; the Deployment always supplies it, and the value it supplies is
# the one the committed chain is checked against.
UPSTREAM_TIMEOUT_SECONDS = int(os.environ.get("RELAY_UPSTREAM_TIMEOUT_SECONDS", "630"))

# P, THE PROMOTION DEADLINE, sourced from the same committed ConfigMap as R rather than restated.
# INV11 asserts P < route timeout and that the relay reads it from that ConfigMap; a hardcoded
# value here would satisfy the ordering on paper while the actor enforced something else.
# Priced from A10's four-term cost in docs/specs/PRICING-promotion-deadline-P.md.
PROMOTION_DEADLINE_SECONDS = int(os.environ.get("PROMOTION_DEADLINE_SECONDS", "120"))
# Promotion is OFF unless the async front door is configured. The actor pre-registers through the
# jobstore and re-places on SQS, so without those it can hand out a job id nobody can redeem, which
# is worse than not promoting: the caller is told to collect an answer that will never exist.
PROMOTION_ENABLED = bool(os.environ.get("SQS_QUEUE_URL")) and os.environ.get("PROMOTION_ENABLED", "1") != "0"
GATEWAY_ENDPOINT = os.environ.get("GATEWAY_ENDPOINT", "http://inference-gateway.inference.svc.cluster.local")
LISTEN_PORT = int(os.environ.get("RELAY_PORT", "8080"))

# RESOURCE BOUNDS, see the module docstring. Worst case of the bounded inputs per replica, against
# the 512 Mi limit in manifests/08-relay/relay.yaml: 64 connections x (64 KiB of headers + up to
# 2 MiB while a 1 MiB body is read and joined) is about 132 MiB; 4 concurrent parses x about 25 MiB
# (the worst JSON shape measured for a 1 MiB body) is about 100 MiB; and 32 in-flight raw bodies
# held across the upstream call are 32 MiB; 4 concurrent promotion enqueues decode and copy a body
# a few times each, up to about 16 MiB each for non-ASCII text, about 64 MiB. About 330 MiB above
# the process baseline in total.
# Upstream responses are buffered whole and are bounded by the model server's own limits
# (max_tokens within the context window), not here.
# No committed load-test driver targets the relay (they drive the gateway and the submit API), and
# the relay's own published measurement, tests/validation/measure_relay_overhead.sh, runs at
# concurrency 1, so no published number depends on a higher bound. Raise this together with the
# memory limit if your traffic needs more per replica.
MAX_BODY_BYTES = int(os.environ.get("RELAY_MAX_BODY_BYTES", str(http_bounds.DEFAULT_MAX_BODY_BYTES)))
BODY_READ_DEADLINE_SECONDS = float(os.environ.get(
    "RELAY_BODY_READ_DEADLINE_SECONDS", str(http_bounds.DEFAULT_BODY_READ_DEADLINE_SECONDS)))
CLIENT_SOCKET_TIMEOUT_SECONDS = float(os.environ.get("RELAY_CLIENT_SOCKET_TIMEOUT_SECONDS", "60"))
MAX_IN_FLIGHT = int(os.environ.get("RELAY_MAX_IN_FLIGHT", "32"))
_IN_FLIGHT = threading.BoundedSemaphore(MAX_IN_FLIGHT)
MAX_CONNECTIONS = int(os.environ.get("RELAY_MAX_CONNECTIONS", str(http_bounds.DEFAULT_MAX_CONNECTIONS)))
# PARSING IS THE MEMORY-HEAVY PHASE. A parsed 1 MiB JSON body can retain about 24 MiB (an array of
# empty objects, measured 2026-11-07), so parsing and classification run under a small slot count,
# and the parsed body is released before the upstream call, which holds only the raw bytes.
MAX_CONCURRENT_PARSES = int(os.environ.get("RELAY_MAX_CONCURRENT_PARSES", "4"))
_PARSE_SLOTS = threading.BoundedSemaphore(MAX_CONCURRENT_PARSES)
# PROMOTION ENQUEUES are the other place a whole body is copied (into the queue message and the SQS
# client's request). Every promotable request can time out at once, so they also wait for a slot.
MAX_CONCURRENT_ENQUEUES = int(os.environ.get("RELAY_MAX_CONCURRENT_ENQUEUES", "4"))
_ENQUEUE_SLOTS = threading.BoundedSemaphore(MAX_CONCURRENT_ENQUEUES)
# The wait for an enqueue slot, and the SQS client's own limits, are BOUNDED: a slow or unreachable
# SQS endpoint must not queue every promotion behind it for minutes, each request holding its
# in-flight slot. A promotion that cannot be enqueued in time gets 504, like any enqueue failure.
ENQUEUE_WAIT_SECONDS = float(os.environ.get("RELAY_ENQUEUE_WAIT_SECONDS", "10"))
HEADER_BUDGET_BYTES = int(os.environ.get("RELAY_HEADER_BUDGET_BYTES", str(http_bounds.DEFAULT_HEADER_BUDGET_BYTES)))
HEADER_DEADLINE_SECONDS = float(os.environ.get(
    "RELAY_HEADER_DEADLINE_SECONDS", str(http_bounds.DEFAULT_HEADER_DEADLINE_SECONDS)))

# The caller, as the client states it. UNAUTHENTICATED: anyone can send any value, so it separates
# callers' promoted jobs (job identity is client id plus body) and is echoed back, and nothing more.
CALLER_HEADER = "x-caller-id"
CALLER_LABEL = "unauthenticated"
_CALLER_RE = re.compile(r"[A-Za-z0-9._-]{1,64}")


def caller_from(value):
    """A well-formed caller id, or "anonymous". Bounded so it cannot carry header or log injection."""
    value = (value or "").strip()
    return value if _CALLER_RE.fullmatch(value) else "anonymous"

# Per-instance guard budget. The limiter is LOCAL, so N replicas admit up to N times this; that
# error is bounded by setting the budget from the pool's headroom divided by the expected replica
# count, and it is made VISIBLE by the counters below, which a fleet-wide alert sums. See
# guards.UpgradeRateLimiter and guards property 8.
UPGRADE_RATE_PER_SEC = float(os.environ.get("UPGRADE_RATE_PER_SEC", "2.0"))
UPGRADE_BURST = float(os.environ.get("UPGRADE_BURST", "10"))

CORRELATION_HEADER = "x-correlation-id"

# Labelled on BOTH axes, never on a fused token. A metric keyed by "batch-standard" cannot answer
# "how much interactive traffic is there" without string surgery, which is the same collapse the
# wire format was corrected for.
# THE caller LABEL IS A FIXED VALUE. The caller header is unauthenticated, so letting it name a
# series would let any client create unbounded label values. Deployments that add an identity
# layer can label by the authenticated principal instead.
_requests = Counter("relay_requests_total", "Requests handled by the relay",
                    ["caller", "sla", "capability", "reason"])
_promotions = Counter("relay_promotions_total", "Requests promoted to the async path",
                      ["caller", "from_capability", "streaming"])
_promo_skipped = Counter("relay_promotion_skipped_total", "Eligible-window requests not promoted",
                         ["reason"])
_refusals = Counter("relay_refusals_total", "Requests refused before routing", ["reason"])
_upstream_errors = Counter("relay_upstream_errors_total", "Upstream call failures", ["kind"])
_bounded = Counter("relay_requests_bounded_total", "Requests refused by a resource bound before classification",
                   ["reason"])
_admitted = Counter("classifier_upgrades_admitted_total", "Upgrades admitted by the rate guard")
_degraded = Counter("classifier_upgrades_degraded_total", "Upgrades degraded to the floor")
_kill = Gauge("classifier_kill_switch_engaged", "1 when classification is withdrawn")

_KILL_SWITCH = KillSwitch(engaged=os.environ.get("CLASSIFIER_KILL_SWITCH", "0") == "1")
_GUARDS = Guards(_KILL_SWITCH, UpgradeRateLimiter(rate_per_sec=UPGRADE_RATE_PER_SEC, burst=UPGRADE_BURST))
_SESSION, _TIMEOUT = make_relay_session(UPSTREAM_TIMEOUT_SECONDS)
_lock = threading.Lock()


def _sync_guard_metrics() -> None:
    """Mirror the guards' own counters onto the Prometheus ones.

    The guards keep their totals internally so they stay importable with no metrics dependency,
    which is what lets them be tested with no cluster. This is the one place the two representations
    meet, and it is a `_value.set` rather than an increment precisely so a missed scrape cannot make
    the exported total drift from the authority inside the guard.
    """
    c = _GUARDS.counters()
    _admitted._value.set(c["classifier_upgrades_admitted_total"])
    _degraded._value.set(c["classifier_upgrades_degraded_total"])
    _kill.set(c["classifier_kill_switch_engaged"])


def _enqueue_promoted(job_id, raw, rec, decision, capability, caller, path="/v1/completions",
                      raw_is_json=None) -> bool:
    """Re-place a promoted request on the ASYNC path. Returns False if it could not be placed.

    RETURNING FALSE MATTERS AS MUCH AS SUCCEEDING. If the job cannot be enqueued, the caller must
    NOT receive a job id: an id for a job that will never run tells them to collect an answer that
    does not exist, which is strictly worse than the timeout they would otherwise have seen. The
    caller of this function converts a False into a 504.

    The message carries the PROMOTED PAIR, so the worker selects its pool from the same two headers
    the synchronous path used, with the capability coordinate held fixed per section 6.9.5.

    THE CALLER'S BODY IS SPLICED IN VERBATIM as `payload`, never parsed again (release candidate 7).
    Parsing it here, outside the parse slots, let every request that timed out at once re-parse its
    body together: about 640 MiB for 32 adversarial 1 MiB bodies. The worker parses the message, so
    it reads the same payload either way. `raw_is_json` is the parse phase's verdict: an unparseable
    body is not enqueued (the caller gets 504), as before. None means unknown, and the body is then
    validated once under the parse slots.
    """
    try:
        import boto3

        queue_url = os.environ.get("SQS_QUEUE_URL")
        if not queue_url:
            return False
        if raw_is_json is False:
            return False
        msg = {
            "job_key": job_id,
            "client_id": caller,
            # THE ROUTE THE REQUEST ARRIVED ON. Without it the worker defaults to
            # /chat/completions, so a promoted /v1/completions request is re-submitted as a CHAT
            # request and the gateway returns 400. Found 2026-08-05 by phase3b's deliverable 1: the
            # promotion path and the worker had never been exercised together, so the shape
            # mismatch could not surface. A promoted job must be re-placed as THE SAME REQUEST it
            # was, not as a differently-shaped one that happens to reach the same model.
            mc.PATH: path,
            mc.SLA: rec["promoted_to_sla"],
            mc.CAPABILITY: rec["promoted_to_capability"],
            "promotion": rec,
        }
        head = json.dumps(msg)
        if not _ENQUEUE_SLOTS.acquire(timeout=ENQUEUE_WAIT_SECONDS):
            print(f"promotion enqueue failed for {job_id}: no enqueue slot within {ENQUEUE_WAIT_SECONDS}s", flush=True)
            return False
        try:
            # DECODED INSIDE THE SLOT, so requests waiting for one hold only their raw bytes, and
            # DECODED EXACTLY AS json.loads DECODED IT (json.detect_encoding, surrogatepass), so the
            # text spliced below is the JSON the parse phase accepted: a UTF-8 BOM is dropped, and
            # UTF-16 or UTF-32 JSON is carried as its characters, not as bytes that happen to be
            # valid UTF-8. A character SQS cannot carry makes the send fail, and the caller gets 504.
            if isinstance(raw, str):
                text = raw
            else:
                text = raw.decode(json.detect_encoding(raw), "surrogatepass") if raw else ""
            text = text or "{}"
            if raw_is_json is None:
                with _PARSE_SLOTS:
                    json.loads(text)   # validity only; raises into the handler below
            from botocore.config import Config
            sqs = boto3.client("sqs", config=Config(connect_timeout=5, read_timeout=10,
                                                    retries={"max_attempts": 3, "mode": "standard"}))
            sqs.send_message(QueueUrl=queue_url, MessageBody=head[:-1] + ', "payload": ' + text + "}")
        finally:
            _ENQUEUE_SLOTS.release()
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"promotion enqueue failed for {job_id}: {type(exc).__name__}: {exc}", flush=True)
        return False


class Handler(http_bounds.BoundedRequestMixin, BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    # Applied to the client socket by StreamRequestHandler.setup: bounds each read and write. The
    # wait for a request, including the next one on an idle keep-alive connection, is bounded by
    # the shorter header deadline. The upstream call has its own timeout (Chain B's R) and does not
    # touch this socket while it waits.
    timeout = CLIENT_SOCKET_TIMEOUT_SECONDS
    header_budget_bytes = HEADER_BUDGET_BYTES
    header_deadline_seconds = HEADER_DEADLINE_SECONDS
    max_body_bytes = MAX_BODY_BYTES

    def count_bounded(self, reason: str) -> None:
        _bounded.labels(reason=reason).inc()

    def log_message(self, fmt, *args):  # noqa: A003
        # Access logging is the gateway's job and its format is the authoritative per-request record
        # (REQTRACE, joined by correlation id). A second unstructured log here would be noise that
        # looks like evidence.
        pass

    def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
        self.send_response(code)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/healthz":
            self._send(200, b'{"ok":true}')
        elif path == "/metrics":
            _sync_guard_metrics()
            self._send(200, generate_latest(), CONTENT_TYPE_LATEST)
        else:
            self._send(404, b'{"error":"not found"}')

    def _refuse_bounded(self, code: int, reason: str, retry_after: int | None = None) -> None:
        """Refuse on a resource bound and close the connection gracefully: an unread body may
        still be in it, and a plain close would reach the client as a reset, not as this status."""
        _bounded.labels(reason=reason).inc()
        self.close_connection = True
        body = json.dumps({"error": "request refused by a relay resource bound", "reason": reason}).encode()
        try:
            self.send_response(code)
            self.send_header("content-type", "application/json")
            self.send_header("x-relay-reason", f"bounded:{reason}")
            if retry_after is not None:
                self.send_header("retry-after", str(retry_after))
            self.send_header("connection", "close")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            pass   # the client is already gone; nothing left to tell it
        self.linger_close()

    def do_POST(self) -> None:  # noqa: N802
        if not self.path.startswith("/v1"):
            self.close_connection = True   # the body is unread, so the connection cannot be reused
            self._send(404, b'{"error":"not found"}')
            self.linger_close()
            return

        caller = caller_from(self.headers.get(CALLER_HEADER))

        try:
            raw = http_bounds.read_body(self.rfile, self.headers, MAX_BODY_BYTES, BODY_READ_DEADLINE_SECONDS,
                                        sock=self.connection)
        except http_bounds.BodyRejected as rej:
            self._refuse_bounded(rej.status, rej.reason)
            return

        # THE IN-FLIGHT BOUND is taken only once the body has arrived, so a slow or idle upload holds
        # a connection slot (bounded by the read deadline) but never an in-flight slot. Non-blocking:
        # a full relay answers 503 at once rather than queueing callers behind requests that may hold
        # their slot for the full upstream timeout.
        if not _IN_FLIGHT.acquire(blocking=False):
            self._refuse_bounded(503, "in-flight-cap", retry_after=1)
            return
        try:
            self._serve(raw, caller)
        finally:
            _IN_FLIGHT.release()

    def _serve(self, raw: bytes, caller: str) -> None:

        # THE PARSE PHASE RUNS UNDER _PARSE_SLOTS (see MAX_CONCURRENT_PARSES) and ends by releasing the
        # parsed body, so its memory is bounded by the slot count rather than by the in-flight count.
        with _PARSE_SLOTS:
            # PARSING IS PART OF CLASSIFICATION, SO IT FAILS THE SAME WAY. An unparseable body is not
            # rejected here: it is forwarded at the floor, and the upstream decides whether it is valid.
            # The relay is not a validation layer, and turning it into one would mean a classifier bug
            # could reject traffic the engine would have served.
            try:
                body = json.loads(raw) if raw else {}
                raw_is_json = True
                # ONLY A JSON OBJECT IS PROMOTABLE. The job id is job_key(caller, body) with `body`
                # replaced by {} below for any other JSON value, while the worker hashes the payload
                # it is given, so an array or scalar body would get an id the worker never claims.
                body_is_object = isinstance(body, dict)
                if not isinstance(body, dict):
                    body = {}
            except Exception:  # noqa: BLE001
                body = {}
                raw_is_json = False
                body_is_object = False

            headers = {k.lower(): v for k, v in self.headers.items()}
            decision = classify(body, headers)

            # THE REFUSAL PATH, and it comes BEFORE the guards on purpose. A guard's job is to bound how
            # much traffic the classifier may move; it has no opinion on a request that physics cannot
            # serve, and running it here would let a rate limit turn a deterministic refusal into a
            # different answer depending on load.
            if decision.refused:
                _refusals.labels(reason=decision.reason).inc()
                payload = json.dumps({
                    "error": "infeasible_request",
                    "reason": decision.reason,
                    "detail": decision.refusal,
                    "required_context_tokens": decision.required_context_tokens,
                }).encode()
                self.send_response(400)
                self.send_header("content-type", "application/json")
                self.send_header("x-relay-sla", decision.sla)
                self.send_header("x-relay-capability", decision.capability)
                self.send_header("x-relay-reason", decision.reason)
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return

            # The guards act on the CAPABILITY axis, which is the one the classifier moves. The SLA class
            # comes from the caller and the relay never raises it, so there is nothing to bound there.
            #
            # THE FLOOR IS THE CONSTRAINT FLOOR, NOT `economy`. Clarification 0b A22 puts deterministic
            # constraints above preferences, and a guard is a preference. With a blanket economy floor an
            # exhausted rate limiter would degrade a CONTEXT-BOUND request into a class that physically
            # cannot hold it, producing the backend failure A22 refuses to produce for a caller's pin.
            # A guard may withdraw a DISCRETIONARY upgrade and may never go below what physics requires.
            capability = _GUARDS.decide(decision.min_feasible, decision.capability)
            # `capability` may differ from `decision.capability` when a guard fired. Both are recorded:
            # the reason explains what the classifier wanted, the label records what was served, and the
            # divergence is exactly what the upgrade-rate alert watches for.
            _requests.labels(caller=CALLER_LABEL, sla=decision.sla, capability=capability, reason=decision.reason).inc()

            upstream = dict(headers)
            upstream.pop("host", None)
            upstream.pop("content-length", None)
            # TWO HEADERS, NEVER A FUSED TOKEN (D-3). The objective header carries the SLA class alone,
            # because the HTTPRoute matches it EXACTLY against `batch`; a combined value would fall
            # through to the catch-all interactive rule and land batch traffic on the interactive pool.
            upstream[SLA_HEADER] = decision.sla
            upstream[CAPABILITY_HEADER] = capability
            upstream.setdefault("content-type", "application/json")

            # ---- THE PROMOTION CLOCK ---------------------------------------------------------------
            # The upstream call is given P seconds rather than R when the request is promotable. That IS
            # the abandonment mechanism: requests' timeout closes the connection, and per 0b A15 a
            # connection close is what frees the sequence slot. There is no abort endpoint call and no
            # acknowledgment to wait for.
            #
            # P < R < L is Chain B, asserted statically by INV11 and INV14, so a promotion deadline can
            # never outlive the machinery enforcing it.
            pdec = promo.decide(body, headers)
            promotable = PROMOTION_ENABLED and decision.sla == "interactive" and pdec.eligible and body_is_object
            call_timeout = PROMOTION_DEADLINE_SECONDS if promotable else _TIMEOUT
            job_id = None
            if promotable:
                # PRE-REGISTER ON ARRIVAL, not at the deadline. The id must exist before it can be handed
                # out, and it uses the SAME job_key the async front door uses so a promoted job and a
                # directly submitted duplicate collapse to one identity under the conditional claim.
                try:
                    import jobkey
                    job_id = jobkey.job_key(caller, body)
                except Exception as exc:  # noqa: BLE001
                    # Pre-registration failed, so there is no id to hand out. Fall back to serving the
                    # request normally at the full timeout: a promotion that cannot name its job would
                    # tell the caller to collect an answer that does not exist.
                    _promo_skipped.labels(reason=f"preregistration-failed:{type(exc).__name__}").inc()
                    promotable, call_timeout = False, _TIMEOUT
            elif PROMOTION_ENABLED and decision.sla == "interactive":
                _promo_skipped.labels(reason=pdec.reason).inc()
            body = None   # released: nothing below uses the parsed body, only `raw`

        started = time.monotonic()
        try:
            r = _SESSION.post(
                f"{GATEWAY_ENDPOINT}{self.path}",
                data=raw,
                headers=upstream,
                timeout=call_timeout,
                stream=True,
            )
        except requests.exceptions.Timeout:
            # THE DEADLINE FIRED. requests has already closed the connection, which per 0b A15 is
            # what frees the sequence slot upstream; the slot-release latency (0.55 to 0.86 s,
            # CONCURRENCY-1 DERIVED, so a floor rather than a bound under load) is term 4 of the
            # promotion cost and is not waited on here.
            if not promotable or job_id is None:
                raise
            elapsed = time.monotonic() - started
            rec = promo.promotion_record(job_id, decision.sla, capability, caller, elapsed)
            enqueued = _enqueue_promoted(job_id, raw, rec, decision, capability, caller, path=self.path,
                                         raw_is_json=raw_is_json)
            if not enqueued:
                # Could not re-place. Do NOT hand out a job id for a job that will never run.
                _promo_skipped.labels(reason="enqueue-failed").inc()
                body_b = json.dumps({"error": "upstream timeout", "promoted": False}).encode()
                self.send_response(504)
                self.send_header("content-type", "application/json")
                self.send_header("x-relay-reason", "promotion-enqueue-failed")
                self.send_header("content-length", str(len(body_b)))
                self.end_headers()
                self.wfile.write(body_b)
                return
            _promotions.labels(caller=CALLER_LABEL, from_capability=capability,
                               streaming=str(pdec.streaming).lower()).inc()
            if pdec.streaming:
                # Opted-in stream: terminal named event, then the stream ends.
                payload = promo.promotion_sse_event(job_id).encode()
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
            else:
                payload = json.dumps(promo.promotion_body(job_id)).encode()
                self.send_response(202)
                self.send_header("content-type", "application/json")
            self.send_header("x-relay-sla", rec["promoted_to_sla"])
            self.send_header("x-relay-capability", rec["promoted_to_capability"])
            self.send_header("x-relay-reason", f"promoted:{pdec.reason}:after={elapsed:.1f}s")
            self.send_header("x-relay-caller", caller)
            self.send_header("x-relay-job-id", job_id)
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        except Exception as exc:  # noqa: BLE001
            # NOT RETRIED, and the reason is in relay.py: there is no claim below this path, so a
            # retry is a second generation on a GPU. The failure surfaces to the caller, who is
            # present on an interactive path and can decide for themselves.
            _upstream_errors.labels(kind=type(exc).__name__).inc()
            # THE CLASSIFICATION IS REPORTED ON THE FAILURE PATH TOO. The first live test returned a
            # 502 with no x-relay-tier, which is precisely backwards: when the upstream fails is
            # when you most need to know which pool the relay had chosen, because "the interactive
            # pool is unreachable" and "we classified into a pool that does not exist" are different
            # incidents that look identical without it.
            body = json.dumps({"error": "upstream unavailable", "kind": type(exc).__name__}).encode()
            self.send_response(502)
            self.send_header("content-type", "application/json")
            self.send_header("x-relay-sla", decision.sla)
            self.send_header("x-relay-capability", capability)
            self.send_header("x-relay-reason", decision.reason)
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        payload = r.content
        self.send_response(r.status_code)
        for h in ("content-type", CORRELATION_HEADER):
            if h in r.headers:
                self.send_header(h, r.headers[h])
        # The classification is echoed so a caller, and the efficacy work later, can see what the
        # relay decided without reading the relay's logs.
        self.send_header("x-relay-sla", decision.sla)
        self.send_header("x-relay-capability", capability)
        self.send_header("x-relay-reason", decision.reason)
        self.send_header("x-relay-caller", caller)
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def main() -> int:
    print(
        f"relay listening on :{LISTEN_PORT} -> {GATEWAY_ENDPOINT} "
        f"(upstream timeout {UPSTREAM_TIMEOUT_SECONDS}s, retries disabled, "
        f"kill switch {'ENGAGED' if _KILL_SWITCH.engaged else 'released'}, "
        f"no caller authentication: deploy behind your own identity layer)",
        flush=True,
    )
    http_bounds.BoundedThreadingHTTPServer(
        ("", LISTEN_PORT), Handler, MAX_CONNECTIONS,
        on_refused=lambda: _bounded.labels(reason="connection-cap").inc(),
    ).serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
