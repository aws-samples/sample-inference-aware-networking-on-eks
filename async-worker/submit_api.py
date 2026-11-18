# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Async submit API (the producer that fronts the SQS queue). Companion to worker.py.

Per the design (section 6.2) the submit path establishes job identity and enqueues.
Deduplication of actual GPU work happens at CLAIM time in worker.py (a conditional
PutItem), so this endpoint is deliberately thin: it does not write DynamoDB. A
duplicate submission enqueues a second SQS message that the worker's conditional
claim suppresses. Job identity is deterministic (client_id#sha256(payload)), so two
identical submissions always return the SAME job_id, which is what the client polls.

job_key() is imported from jobkey.py, the single shared definition, so the producer
and the claim path can never drift apart (identity would break if they did).

POST /submit         {"client_id": "...", "payload": {...}}  -> 202 {"job_id": "..."}
GET  /jobs/{job_id}  -> 200 job state, or 404 if there is no durable record
GET  /healthz        -> 200

The redemption route (design v6.1 section 5.1 step 5, spec 001 section 3.5) is what makes
the job id returned by /submit worth anything: before it existed, a caller was handed an
identifier with nowhere to redeem it. It reads DURABLE state through jobstore.get_item,
the same accessor the worker and the sweeper use, so there is exactly one read path into
the job table.
"""
import message_contract as mc
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # ThreadingHTTPServer: used by tests/validation/test_message_contract.py
from urllib.parse import unquote, urlsplit

import boto3
from prometheus_client import CONTENT_TYPE_LATEST, Counter, generate_latest

# Shared identity, imported (never re-implemented) so the producer and the claim path
# can never disagree on what a duplicate is. See jobkey.py and test_job_key.py.
from jobkey import job_key

# The request bounds the relay uses (size, framing, read deadline), shared so the two front doors
# cannot drift apart. See http_bounds.py and deep security scan finding M1.
import http_bounds

# The capability rule, imported from the classifier rather than restated: the relay refuses a
# direct batch+premium pin because premium is reachable on the batch lane only by promotion, and
# this endpoint is the batch lane. Restating the rule here would let the two copies diverge.
from relay import CAP_CLASSES, PROMOTION_ONLY_PAIRS, SLA_BATCH

# Durable job state, read through the SAME accessor the worker and sweeper use. Importing
# jobstore rather than opening a second DynamoDB client keeps one read path into the table,
# so a change to the item shape cannot leave this route reading a stale schema.
import jobstore

SQS_QUEUE_URL = os.environ["SQS_QUEUE_URL"]
PORT = int(os.environ.get("PORT", "8080"))
sqs = boto3.client("sqs")

MAX_BODY_BYTES = int(os.environ.get("SUBMIT_MAX_BODY_BYTES", str(http_bounds.DEFAULT_MAX_BODY_BYTES)))
BODY_READ_DEADLINE_SECONDS = float(os.environ.get(
    "SUBMIT_BODY_READ_DEADLINE_SECONDS", str(http_bounds.DEFAULT_BODY_READ_DEADLINE_SECONDS)))
CLIENT_SOCKET_TIMEOUT_SECONDS = float(os.environ.get("SUBMIT_CLIENT_SOCKET_TIMEOUT_SECONDS", "60"))
MAX_CONNECTIONS = int(os.environ.get("SUBMIT_MAX_CONNECTIONS", str(http_bounds.DEFAULT_MAX_CONNECTIONS)))
HEADER_BUDGET_BYTES = int(os.environ.get("SUBMIT_HEADER_BUDGET_BYTES", str(http_bounds.DEFAULT_HEADER_BUDGET_BYTES)))
HEADER_DEADLINE_SECONDS = float(os.environ.get(
    "SUBMIT_HEADER_DEADLINE_SECONDS", str(http_bounds.DEFAULT_HEADER_DEADLINE_SECONDS)))
# Parsing a 1 MiB body can retain about 24 MiB, and the parsed payload is held until the SQS send
# returns, so parse-to-enqueue runs under a small slot count. Callers WAIT for a slot rather than
# being refused: the load tests submit with up to 60 concurrent callers, and an enqueue takes
# milliseconds, so waiting costs them milliseconds where a refusal would cost them a retry.
MAX_CONCURRENT_PARSES = int(os.environ.get("SUBMIT_MAX_CONCURRENT_PARSES", "8"))
_PARSE_SLOTS = threading.BoundedSemaphore(MAX_CONCURRENT_PARSES)
# Capability classes a direct submission may name: every deployed class except those the batch lane
# reaches only by promotion.
SUBMITTABLE_CAPABILITIES = tuple(c for c in CAP_CLASSES if (SLA_BATCH, c) not in PROMOTION_ONLY_PAIRS)

# The anchor of the async path for reconciliation: every accepted submission is one enqueue.
# Observability only: this counter does not affect submission or dedup. Scraped at /metrics.
SUBMISSIONS = Counter("submit_api_submissions_total", "Accepted async job submissions (enqueued)")
BOUNDED = Counter("submit_api_requests_bounded_total", "Requests refused by a resource bound", ["reason"])

# Redemptions served, split by the state the caller was told. Lets an operator see whether
# callers are polling for jobs that have no durable record (the not_found case below), which
# is the signal that the pre-registration gap is being hit in practice.
REDEMPTIONS = Counter(
    "submit_api_redemptions_total",
    "GET /jobs/{job_id} responses, by reported status",
    ["status"],
)

# Attributes that must never leave the process. claim_token is an ownership credential;
# lease_expires and ttl are scheduler internals a caller would misread as promises; payload
# and client_id are echoed input; backend_endpoint, requeued_at and infra_rejections describe
# infrastructure, not the job. Everything the caller gets is built field by field in
# _job_view, so a new attribute added to the item is invisible here until someone adds it
# deliberately. That direction (allow-list, not deny-list) is the point.
_INTERNAL_ONLY = frozenset({
    "claim_token", "lease_expires", "ttl", "payload", "client_id",
    "backend_endpoint", "backend_selected_at", "requeued_at", "infra_rejections",
})


def _s(item: dict, name: str) -> str | None:
    """Read a DynamoDB string attribute, or None if absent."""
    return item.get(name, {}).get("S")


def _n(item: dict, name: str) -> int | None:
    """Read a DynamoDB numeric attribute as an int, or None if absent or unparseable."""
    raw = item.get(name, {}).get("N")
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _job_view(job_id: str, item: dict) -> dict:
    """Project a durable job item onto the caller-facing view.

    The five states a caller must act on differently:

      pending   a durable record exists but no worker currently holds it. Either the claim
                lapsed (the owner died) or it was released after an infrastructure rejection
                (jobstore.release sets lease_expires to 0 and leaves status processing). A
                redelivery or the sweeper will re-drive it. Keep polling.
      running   a worker holds a live claim. Keep polling.
      complete  terminal and successful. `result` carries the inference response.
      failed    the attempt failed for a reason attributable to the job (a 4xx or a bad
                result). `retryable` says whether a re-claim is possible right now.
      unknown   no durable record. Served as 404 by the caller of this function.

    `pending` versus `running` is derived from the lease rather than from status, because
    the status attribute alone cannot distinguish a live owner from an abandoned claim: both
    read `processing`. That is the same lease test try_claim uses to decide whether a claim
    is stealable, so the caller's view and the claim logic agree by construction.
    """
    status = _s(item, "status")
    lease = _n(item, "lease_expires") or 0
    now = jobstore.now_epoch()

    view = {
        "job_id": job_id,
        # Reserved for the promotion work (design v6.1 section 5.4, decision D-1). Until
        # promotion ships, every durable record arrived through POST /submit. The field is
        # present now so that a caller written today keeps working when "promoted" starts
        # appearing, rather than having to handle a key that suddenly exists.
        "delivery": "declared_async",
        "attempts": _n(item, "claim_count") or 0,
        "updated_at": _s(item, "updated_at"),
    }
    submitted_at = _s(item, "submitted_at")
    if submitted_at:
        view["submitted_at"] = submitted_at

    if status == "complete":
        view["status"] = "complete"
        view["completed_at"] = _s(item, "completed_at")
        raw = _s(item, "result")
        # The worker stores the response as a JSON string. Hand back the parsed object when
        # it parses, and the raw string when it does not (the worker truncates at 300000
        # characters, so a very large response can be cut mid-JSON; returning the fragment
        # is more useful to a caller than an error, and is honest about what was stored).
        if raw is not None:
            try:
                view["result"] = json.loads(raw)
            except ValueError:
                view["result"] = raw
                view["result_truncated"] = True
    elif status == "failed":
        view["status"] = "failed"
        view["error"] = _s(item, "error")
        # Retryable NOW, not retryable in principle: try_claim admits a re-claim only once
        # the lease has passed. Whether a retry actually happens depends on an SQS message
        # still being live or the sweeper re-driving it, which this route cannot see, so the
        # field is deliberately named for what it asserts.
        view["retryable"] = lease <= now
    elif status == "processing":
        view["status"] = "running" if lease > now else "pending"
    else:
        # An item exists but carries a status this version does not know. Report it as
        # unrecognised rather than guessing, per DESIGN.md directive 7.
        view["status"] = "unrecognised"
        view["detail"] = "the durable record carries a status this API does not recognise"
    return view


class Handler(http_bounds.BoundedRequestMixin, BaseHTTPRequestHandler):
    # Bounds each read and write on the client socket (StreamRequestHandler.setup applies it). The
    # request line and headers have their own budget and deadline (http_bounds).
    timeout = CLIENT_SOCKET_TIMEOUT_SECONDS
    header_budget_bytes = HEADER_BUDGET_BYTES
    header_deadline_seconds = HEADER_DEADLINE_SECONDS
    max_body_bytes = MAX_BODY_BYTES

    def count_bounded(self, reason: str) -> None:
        BOUNDED.labels(reason=reason).inc()

    def _send(self, code: int, obj: dict) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        # Split off any query string before matching. self.path is the raw request target,
        # so a client appending ?foo=bar must not turn a valid route into a 404.
        path = urlsplit(self.path).path
        if path == "/healthz":
            self._send(200, {"ok": True})
        elif path == "/metrics":
            body = generate_latest()
            self.send_response(200)
            self.send_header("content-type", CONTENT_TYPE_LATEST)
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path.startswith("/jobs/"):
            self._get_job(path[len("/jobs/"):])
        else:
            self._send(404, {"error": "not found"})

    def _get_job(self, raw_id: str) -> None:
        """GET /jobs/{job_id}.

        Job identity is client_id#sha256(payload) (jobkey.job_key), so every job id contains
        a '#'. In a URL that character starts the fragment, and a fragment is NOT transmitted
        to the server, so a client that sends the id literally gives us a TRUNCATED id and
        gets a 404 for a job that exists. Callers must percent-encode it as %23, and this
        handler unquotes. That asymmetry is the obvious way this route breaks in production,
        so it is tested (tests/validation/test_redemption_route.py).

        Authorization: the caller is NOT required to supply client_id. It would add no
        protection, because client_id is the literal prefix of the job id, so anyone able to
        present the id already knows it, and cross-checking the two would only catch typos.
        The real property is that the id embeds sha256(payload), so it is unguessable without
        the exact request body: the job id functions as a bearer capability. That is a
        deliberate ruling, not an oversight, and it has a limit worth stating plainly: anyone
        who obtains a job id can read that job's result, and this service has no
        authentication to prevent it. Multi-tenant deployments need authentication at the
        ingress. Recorded as a gap rather than papered over with a check that does not check.
        """
        job_id = unquote(raw_id)
        if not job_id:
            self._send(400, {"error": "missing job_id"})
            return
        item = jobstore.get_item(job_id)
        if item is None:
            # No durable record. This is genuinely ambiguous and the ambiguity is structural:
            # POST /submit enqueues without writing DynamoDB (deliberately, see the module
            # docstring), and the first write happens at CLAIM time in the worker. So an id
            # that was submitted a moment ago and an id that was never submitted are
            # indistinguishable here. Closing it needs pre-registration at submit time, which
            # cannot be bolted on without changing try_claim's attribute_not_exists condition,
            # the gate the exactly-once guarantee rests on. Reported to the design rather than
            # worked around; the response says which two cases it cannot separate.
            REDEMPTIONS.labels(status="not_found").inc()
            self._send(404, {
                "job_id": job_id,
                "status": "not_found",
                "detail": (
                    "no durable job record: the job was either never submitted, or was "
                    "submitted and has not yet been claimed by a worker. A record appears "
                    "when a worker claims the job."
                ),
            })
            return
        view = _job_view(job_id, item)
        REDEMPTIONS.labels(status=view["status"]).inc()
        self._send(200, view)

    def do_POST(self) -> None:
        if self.path != "/submit":
            self.close_connection = True   # the body is unread, so the connection cannot be reused
            self._send(404, {"error": "not found"})
            self.linger_close()
            return
        try:
            raw = http_bounds.read_body(self.rfile, self.headers, MAX_BODY_BYTES, BODY_READ_DEADLINE_SECONDS,
                                        sock=self.connection)
        except http_bounds.BodyRejected as rej:
            # The unread body is still in the socket, so this connection cannot carry another request,
            # and it is closed gracefully so the client reads this status rather than a reset.
            self.close_connection = True
            self.count_bounded(rej.reason)
            self._send(rej.status, {"error": "request refused by a resource bound", "reason": rej.reason})
            self.linger_close()
            return
        with _PARSE_SLOTS:
            self._submit(raw)

    def _submit(self, raw: bytes) -> None:
        try:
            body = json.loads(raw or b"{}")
            client_id = body["client_id"]
            payload = body["payload"]
            # Optional. A submit-API caller is on the batch lane by definition, so this is economy,
            # standard or absent. Premium reaches the batch lane only by promotion, which enqueues
            # through the relay, never through this endpoint.
            capability = body.get("capability")
        # FIXED MESSAGES, never the exception text: the reply says what is wrong with the request
        # without echoing parser internals back to the caller. A JSON body that is not an object
        # (a list, a string) is a 400 too, rather than an unhandled TypeError in the handler.
        except ValueError:
            self._send(400, {"error": "bad request: body is not valid JSON"})
            return
        except KeyError as e:
            self._send(400, {"error": "bad request: missing field %s" % e.args[0]})
            return
        except (TypeError, AttributeError):
            self._send(400, {"error": "bad request: body must be a JSON object"})
            return
        # THE SAME ADMISSION RULE THE RELAY ENFORCES. Before release candidate 7 any value passed
        # through to the worker's capability header, so a direct submission could reach the
        # promotion-only batch+premium pair the relay refuses. Absent or empty still means standard.
        # Fixed messages: the caller's value is never echoed back.
        if capability:
            if isinstance(capability, str) and (SLA_BATCH, capability) in PROMOTION_ONLY_PAIRS:
                self._send(400, {"error": (
                    "bad request: this capability is reachable on the batch lane only by promotion; "
                    "send the request to the relay as interactive with the same capability pin")})
                return
            if not isinstance(capability, str) or capability not in SUBMITTABLE_CAPABILITIES:
                self._send(400, {"error": "bad request: capability must be one of "
                                          + ", ".join(SUBMITTABLE_CAPABILITIES)})
                return
        job_id = job_key(client_id, payload)
        # Correlation ID for end-to-end tracing IS the deterministic job_id. It is carried
        # as a sibling of client_id/payload (NOT inside payload, so the dedup hash is
        # unchanged) so the worker can persist it and the submission timestamp without
        # recomputing. submitted_at is the first timestamp in the per-request trace.
        sqs.send_message(
            QueueUrl=SQS_QUEUE_URL,
            MessageBody=json.dumps({
                "client_id": client_id,
                "payload": payload,
                "job_id": job_id,
                "submitted_at": int(time.time()),
                # CAPABILITY, a sibling of payload for the same reason correlation_id is: it must
                # NOT enter the dedup hash. job_key(client_id, payload) is computed above from
                # payload alone, so adding this cannot change a job id and exactly-once is
                # untouched. Omitted when the caller did not declare one, and an absent field means
                # standard at the worker, which preserves today's behaviour exactly.
                    # The KEY is the shared contract's, not a local spelling: this file and the promotion
                # producer once spelled it two different ways (see message_contract.py).
                **({mc.CAPABILITY: capability} if capability else {}),
            }),
        )
        SUBMISSIONS.inc()
        # 202 Accepted: work enqueued. Duplicate submissions return the same job_id.
        self._send(202, {"job_id": job_id})

    def log_message(self, *args) -> None:  # quieter logs
        return


if __name__ == "__main__":
    http_bounds.BoundedThreadingHTTPServer(
        ("0.0.0.0", PORT), Handler, MAX_CONNECTIONS,  # nosec B104 - the pod IP is the only interface; a ClusterIP Service decides reachability
        on_refused=lambda: BOUNDED.labels(reason="connection-cap").inc(),
    ).serve_forever()
