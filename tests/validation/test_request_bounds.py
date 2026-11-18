# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Offline proof for the request bounds of the two HTTP front doors (deep security scan finding M1).

Runs the REAL relay and submit API handlers on loopback ports, with a loopback stub standing in for
the gateway and a mocked SQS (moto), so it needs no cluster, no GPU and no AWS credentials. Raw
sockets are used for the malformed requests, because an HTTP client library refuses to send most
of them, and a test that cannot send the attack proves nothing about the defence.

What it proves, each against its own failure mode:
  1. An ordinary request still reaches the upstream and is classified exactly as before.
  2. Size: a declared body above the cap is refused with 413 and never reaches the upstream.
  3. Framing: negative, non-numeric and conflicting Content-Length get 400; Transfer-Encoding 411.
  4. Time: a trickled body and a stalled body both hit the read deadline (408); a body cut short
     gets 400.
  5. In-flight: with the cap full, the next request gets 503 with Retry-After at once, and the slot
     is free again after the first request finishes. A slow upload does not hold an in-flight slot.
  6. An idle keep-alive connection is closed at the header deadline.
  7. Connections: past the connection cap the next connection gets 503 from the accept loop.
  8. Header block: an oversized block and a trickled block are closed without a reply, and a
     malformed block (a line with no colon, which hides Content-Length) gets 400.
  9. A refused upload of 900 KB reads its 413 instead of a connection reset.
  9b. `Expect: 100-continue` with a body already known to be refused gets the refusal, not 100;
      bare CR, a leading "From " line and obs-fold are malformed (400); HTTP/0.9 does not crash.
 10. The word count is identical to the old list-based count and no longer allocates per word.
 13. A promoted request's queue message carries exactly the body that was accepted, in every
     encoding json.loads accepts, and an unparseable body is never enqueued.
 14. Through the real relay: an object body is promoted under the id the worker computes, an array
     body is not promoted, and a promotion that cannot get an enqueue slot fails fast with 504.
 11. The submit API applies the same bounds, serves 60 concurrent submissions through its parse
     slots, and refuses the promotion-only batch+premium pair and
     unknown capability values, while economy, standard and absent are still accepted.

Run:  ./.venv/bin/python tests/validation/test_request_bounds.py
"""
import json
import os
import re
import socket
import sys
import threading
import time
import tracemalloc
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Small bounds so the test runs in seconds. The production defaults are asserted separately (8).
os.environ.update({
    "RELAY_MAX_BODY_BYTES": "4096",
    "RELAY_BODY_READ_DEADLINE_SECONDS": "1",
    "RELAY_CLIENT_SOCKET_TIMEOUT_SECONDS": "2",
    "RELAY_MAX_IN_FLIGHT": "1",
    "RELAY_MAX_CONNECTIONS": "4",
    "RELAY_HEADER_BUDGET_BYTES": "4096",
    "RELAY_HEADER_DEADLINE_SECONDS": "1",
    "SUBMIT_MAX_BODY_BYTES": "4096",
    "SUBMIT_BODY_READ_DEADLINE_SECONDS": "1",
    "SUBMIT_CLIENT_SOCKET_TIMEOUT_SECONDS": "2",
    "AWS_DEFAULT_REGION": "us-west-2",
    "AWS_ACCESS_KEY_ID": "testing",
    "AWS_SECRET_ACCESS_KEY": "testing",
    "AWS_SESSION_TOKEN": "testing",
    "DDB_TABLE": "inference-jobs",
    "NO_PROXY": "127.0.0.1,localhost",
    "no_proxy": "127.0.0.1,localhost",
})
os.environ.pop("SQS_QUEUE_URL", None)   # the relay must not promote in this test
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "async-worker"))

# ---- the gateway stand-in --------------------------------------------------------------------
UPSTREAM_HITS = []
UPSTREAM_GATE = threading.Event()
UPSTREAM_GATE.set()
UPSTREAM_ENTERED = threading.Event()


class _Upstream(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("content-length") or 0)
        self.rfile.read(n)
        UPSTREAM_HITS.append((self.path, self.headers.get("x-gateway-inference-objective"),
                              self.headers.get("x-inference-model-class")))
        UPSTREAM_ENTERED.set()
        UPSTREAM_GATE.wait(10)
        body = b'{"choices":[{"text":"ok"}]}'
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _serve(handler, server=None):
    srv = server or ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _raw(port, data, *, trickle=None, trickle_head=False, half_close=False, timeout=8):
    """Send raw bytes and return (status, headers, body). `trickle` sends one byte per interval,
    of the body only, or of everything with `trickle_head`. Status 0 means no reply."""
    s = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    try:
        if trickle is not None:
            head, _, rest = data.partition(b"\r\n\r\n")
            if trickle_head:
                head, rest = b"", data
            else:
                s.sendall(head + b"\r\n\r\n")
            try:
                for b in rest:
                    s.sendall(bytes([b]))
                    time.sleep(trickle)
            except OSError:
                pass   # the server closed on us, which is the point
        else:
            try:
                s.sendall(data)
            except OSError:
                pass   # a refusal may close the read side; the reply, if any, is still read below
        if half_close:
            s.shutdown(socket.SHUT_WR)
        buf = b""
        while True:
            try:
                chunk = s.recv(65536)
            except (socket.timeout, ConnectionResetError):
                break
            if not chunk:
                break
            buf += chunk
            m = re.search(rb"\r\ncontent-length: *(\d+)\r\n", buf, re.I)
            if m and b"\r\n\r\n" in buf and len(buf.split(b"\r\n\r\n", 1)[1]) >= int(m.group(1)):
                break
    finally:
        s.close()
    head, _, body = buf.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    status = int(lines[0].split()[1]) if lines and lines[0] else 0
    hdrs = {k.strip().lower(): v.strip() for k, v in (ln.split(":", 1) for ln in lines[1:] if ":" in ln)}
    return status, hdrs, body


def _post(path, body, extra=b""):
    return (b"POST " + path + b" HTTP/1.1\r\nHost: t\r\ncontent-type: application/json\r\n" + extra
            + b"content-length: " + str(len(body)).encode() + b"\r\n\r\n" + body)


def test_relay() -> None:
    up = _serve(_Upstream)
    os.environ["GATEWAY_ENDPOINT"] = f"http://127.0.0.1:{up.server_port}"
    import relay_server
    import http_bounds
    srv = _serve(None, http_bounds.BoundedThreadingHTTPServer(
        ("127.0.0.1", 0), relay_server.Handler, relay_server.MAX_CONNECTIONS,
        on_refused=lambda: relay_server._bounded.labels(reason="connection-cap").inc()))
    p = srv.server_port
    try:
        # 1. ordinary request: forwarded, classified, headers set exactly as before
        st, h, _ = _raw(p, _post(b"/v1/completions", b'{"prompt":"hello","max_tokens":16}'))
        assert st == 200 and h.get("x-relay-sla") and h.get("x-relay-capability"), (st, h)
        assert UPSTREAM_HITS and UPSTREAM_HITS[-1][0] == "/v1/completions", UPSTREAM_HITS
        print(f"OK  1 an ordinary request is forwarded and classified ({h['x-relay-sla']}+{h['x-relay-capability']})")

        # 2. size: refused unread with 413, upstream untouched
        before = len(UPSTREAM_HITS)
        big = b'{"prompt":"' + b"ab " * 2000 + b'"}'
        st, h, b = _raw(p, _post(b"/v1/completions", big))
        assert st == 413 and h.get("x-relay-reason") == "bounded:body-too-large", (st, h, b)
        assert h.get("connection", "").lower() == "close", h
        assert len(UPSTREAM_HITS) == before, "an oversized body reached the upstream"
        print("OK  2 a body above the cap gets 413 and never reaches the upstream")

        # 3. framing
        req = b"POST /v1/completions HTTP/1.1\r\nHost: t\r\n%s\r\n\r\n"
        for hdr, code, reason in ((b"content-length: -1", 400, "content-length-invalid"),
                                  (b"content-length: 12abc", 400, "content-length-invalid"),
                                  (b"content-length: 5\r\ncontent-length: 6", 400, "content-length-conflicting"),
                                  (b"transfer-encoding: chunked", 411, "transfer-encoding-unsupported")):
            st, h, _ = _raw(p, req % hdr + b"5\r\nhello\r\n0\r\n\r\n")
            assert st == code and h.get("x-relay-reason") == f"bounded:{reason}", (hdr, st, h)
        assert len(UPSTREAM_HITS) == before, "a malformed request reached the upstream"
        print("OK  3 negative, non-numeric and conflicting Content-Length get 400; Transfer-Encoding gets 411")

        # 4. time
        t0 = time.monotonic()
        st, h, _ = _raw(p, _post(b"/v1/completions", b'{"prompt":"' + b"x" * 200 + b'"}'), trickle=0.3)
        took = time.monotonic() - t0
        assert st == 408 and h.get("x-relay-reason") == "bounded:body-read-deadline", (st, h)
        assert took < 4, f"the read deadline did not bound the trickle ({took:.1f}s)"
        partial = b"POST /v1/completions HTTP/1.1\r\nHost: t\r\ncontent-length: 100\r\n\r\n" + b"x" * 10
        t0 = time.monotonic()
        st, h, _ = _raw(p, partial)
        stall = time.monotonic() - t0
        assert st == 408 and h.get("x-relay-reason") == "bounded:body-read-deadline", (st, h)
        assert stall < 3, f"a stalled body held its connection for {stall:.1f}s"
        t0 = time.monotonic()
        st, _, _ = _raw(p, partial.replace(b"content-length: 100", b"content-length: 100\r\nx-n: 1"))
        assert st == 408 and len(UPSTREAM_HITS) == before
        st, h, _ = _raw(p, partial, half_close=True)
        assert st == 400 and h.get("x-relay-reason") == "bounded:body-truncated", (st, h)
        print(f"OK  4 trickled body hits the deadline in {took:.1f}s, a stalled one in {stall:.1f}s (408); truncated 400")

        # 5. in-flight cap (set to 1 above)
        UPSTREAM_GATE.clear()
        UPSTREAM_ENTERED.clear()
        first = {}
        t = threading.Thread(target=lambda: first.update(
            r=_raw(p, _post(b"/v1/completions", b'{"prompt":"hold"}'), timeout=15)))
        t.start()
        assert UPSTREAM_ENTERED.wait(5), "the first request never reached the upstream"
        t0 = time.monotonic()
        st, h, _ = _raw(p, _post(b"/v1/completions", b'{"prompt":"second"}'))
        assert st == 503 and h.get("retry-after") == "1", (st, h)
        assert h.get("x-relay-reason") == "bounded:in-flight-cap", h
        assert time.monotonic() - t0 < 2, "the 503 was not immediate"
        UPSTREAM_GATE.set()
        t.join(10)
        assert first["r"][0] == 200, first
        st, _, _ = _raw(p, _post(b"/v1/completions", b'{"prompt":"third"}'))
        assert st == 200, f"the slot was not released ({st})"
        print("OK  5 with the in-flight cap full the next request gets 503 at once; the slot is then released")

        # 5b. a slow upload holds no in-flight slot: with an upload stalled mid-body, another
        # request is still served (the in-flight cap is 1 here)
        slow = socket.create_connection(("127.0.0.1", p), timeout=6)
        slow.sendall(b"POST /v1/completions HTTP/1.1\r\nHost: t\r\ncontent-length: 50\r\n\r\n{\"pro")
        time.sleep(0.2)
        st, _, _ = _raw(p, _post(b"/v1/completions", b'{"prompt":"while-slow"}'))
        slow.close()
        assert st == 200, f"a stalled upload took the in-flight slot ({st})"
        print("OK  5b a stalled upload does not hold an in-flight slot")

        # 5c. a refusal written while the in-flight slot is held does not wait on the client. 3
        # callers whose pinned request is refused (economy cannot hold 4,000 tokens) never read
        # their 400. The in-flight cap is 1, so a handler still writing would make the next request
        # 503. This holds because a refusal (about 425 bytes) always fits the socket send buffer;
        # the check guards that assumption rather than any lock ordering.
        lurkers = []
        for _ in range(3):
            c = socket.create_connection(("127.0.0.1", p), timeout=6)
            c.sendall(_post(b"/v1/completions", b'{"prompt":"x","max_tokens":4000}',
                            b"x-inference-model-class: economy\r\n"))
            lurkers.append(c)
            time.sleep(0.05)
        t0 = time.monotonic()
        st, _, _ = _raw(p, _post(b"/v1/completions", b'{"prompt":"after-lurkers"}'))
        took = time.monotonic() - t0
        for c in lurkers:
            c.close()
        time.sleep(0.5)   # let the server see the closes and free their connection slots
        assert st == 200 and took < 1, (st, took)
        print(f"OK  5c refusals to clients that never read do not hold the in-flight slot ({took*1000:.0f} ms)")

        # 6. idle keep-alive connection closed at the header deadline (1 s here)
        s = socket.create_connection(("127.0.0.1", p), timeout=6)
        t0 = time.monotonic()
        closed = s.recv(1) == b""
        took = time.monotonic() - t0
        s.close()
        assert closed and 0.7 < took < 2.5, (closed, took)
        print(f"OK  6 an idle connection is closed at the header deadline after {took:.1f}s")

        # 7. connection cap (4 here): four idle connections, then the fifth gets 503 at once
        idle = [socket.create_connection(("127.0.0.1", p), timeout=6) for _ in range(4)]
        time.sleep(0.1)
        t0 = time.monotonic()
        st, h, _ = _raw(p, _post(b"/v1/completions", b'{"prompt":"fifth"}'))
        assert st == 503 and h.get("x-relay-reason") == "bounded:connection-cap", (st, h)
        assert time.monotonic() - t0 < 1, "the connection-cap 503 was not immediate"
        for c in idle:
            c.close()
        time.sleep(0.3)
        st, _, _ = _raw(p, _post(b"/v1/completions", b'{"prompt":"after"}'))
        assert st == 200, f"connection slots were not released ({st})"
        print("OK  7 past the connection cap the next connection gets 503 at once; slots are released on close")

        # 8. header block: oversized (budget 4096 here), trickled (deadline 1 s), malformed
        st, _, _ = _raw(p, b"POST /v1/completions HTTP/1.1\r\nHost: t\r\nx-big: " + b"a" * 5000 + b"\r\n\r\n")
        assert st == 0, f"an oversized header block got a reply ({st})"
        t0 = time.monotonic()
        st, _, _ = _raw(p, b"POST /v1/completions HTTP/1.1\r\nHost: t\r\nx-slow: abcdefghij\r\n\r\n",
                        trickle=0.3, trickle_head=True)
        took = time.monotonic() - t0
        assert st == 0 and took < 3, (st, took)
        before = len(UPSTREAM_HITS)
        st, h, _ = _raw(p, b"POST /v1/completions HTTP/1.1\r\nHost: t\r\nnot a header line\r\n"
                           b"content-length: 5\r\n\r\nhello")
        assert st == 400 and len(UPSTREAM_HITS) == before, (st, h)
        print(f"OK  8 oversized and trickled ({took:.1f}s) header blocks are closed; a malformed block gets 400")

        # 9. a refused 900 KB upload reads its status, not a reset
        big = b'{"prompt":"' + b"ab " * 300000 + b'"}'
        st, h, _ = _raw(p, _post(b"/v1/completions", big))
        assert st == 413 and h.get("x-relay-reason") == "bounded:body-too-large", (st, h)
        print("OK  9 a refused 900 KB upload reads its 413 instead of a connection reset")

        # 9b. Expect, header smuggling shapes, HTTP/0.9
        st, h, b = _raw(p, b"POST /v1/completions HTTP/1.1\r\nHost: t\r\nexpect: 100-continue\r\n"
                           b"content-length: 2000000\r\n\r\n")
        assert st == 413, f"Expect with an oversized body got {st}, not 413 before the upload"
        before = len(UPSTREAM_HITS)
        for bad in (b"POST /v1/completions HTTP/1.1\r\nHost: t\r\nx-a: a\rcontent-length: 5\r\n\r\nhello",
                    b"POST /v1/completions HTTP/1.1\r\nFrom hostile\r\nHost: t\r\ncontent-length: 2\r\n\r\n{}",
                    b"POST /v1/completions HTTP/1.1\r\nHost: t\r\nx-a: a\r\n transfer-encoding: chunked\r\n"
                    b"content-length: 2\r\n\r\n{}"):
            st, _, _ = _raw(p, bad)
            assert st == 400, (bad[:60], st)
        assert len(UPSTREAM_HITS) == before, "a malformed header block reached the upstream"
        s09 = socket.create_connection(("127.0.0.1", p), timeout=4)
        s09.sendall(b"GET /healthz\r\n")
        try:
            s09.recv(256)
        except OSError:
            pass
        s09.close()
        st, _, _ = _raw(p, _post(b"/v1/completions", b'{"prompt":"after-0.9"}'))
        assert st == 200, f"the server did not survive an HTTP/0.9 request ({st})"
        print("OK  9b Expect refused before upload (413); bare CR, From line and obs-fold get 400; HTTP/0.9 survives")

        # the refusals are visible as a metric, labelled by reason
        st, _, metrics = _raw(p, b"GET /metrics HTTP/1.1\r\nHost: t\r\n\r\n")
        for reason in ("body-too-large", "content-length-invalid", "in-flight-cap", "body-read-deadline",
                       "connection-cap", "header-too-large", "header-deadline", "header-malformed"):
            assert f'relay_requests_bounded_total{{reason="{reason}"}}'.encode() in metrics, reason
        assert b'reason="idle-keepalive"' not in metrics, "idle keep-alive closes were counted as abuse"
        print("OK  6b every refusal is counted in relay_requests_bounded_total by reason")
    finally:
        srv.shutdown(); srv.server_close(); up.shutdown(); up.server_close()


def test_promotion_message() -> None:
    """13. A promoted request's queue message carries the body the parse phase accepted, for every
    encoding json.loads accepts, and is always valid JSON for the worker. Anything it cannot carry
    is refused (the caller gets 504), never enqueued as a message the worker cannot read."""
    import relay_server
    sent = []
    class _SQS:
        def send_message(self, QueueUrl, MessageBody):
            # SQS accepts only #x9 | #xA | #xD | #x20-#xD7FF | #xE000-#xFFFD | #x10000-#x10FFFF.
            for ch in MessageBody:
                o = ord(ch)
                if not (o in (9, 10, 13) or 0x20 <= o <= 0xD7FF or 0xE000 <= o <= 0xFFFD or 0x10000 <= o <= 0x10FFFF):
                    raise ValueError(f"SQS rejects U+{o:04X}")
            sent.append(MessageBody)
    import boto3
    real, boto3.client = boto3.client, (lambda *a, **k: _SQS())
    os.environ["SQS_QUEUE_URL"] = "https://sqs.example/000/q"
    rec = {"promoted_to_sla": "batch", "promoted_to_capability": "standard"}
    try:
        cases = [json.dumps({"prompt": "hi"}).encode(),
                 b"\xef\xbb\xbf" + json.dumps({"prompt": "bom"}).encode(),
                 json.dumps({"prompt": "le"}).encode("utf-16-le"),
                 json.dumps({"prompt": "be"}).encode("utf-16"),
                 json.dumps({"prompt": "u32"}).encode("utf-32"),
                 json.dumps({"prompt": "w\u00f6rter \u5358\u8a9e"}, ensure_ascii=False).encode(),
                 b"  [1, 2]  ", b"", b'{"a": 1, "a": 2}', b'{"n": NaN}']
        for raw in cases:
            sent.clear()
            ok = relay_server._enqueue_promoted("c#k", raw, rec, None, "standard", "c", raw_is_json=True)
            want = json.loads(raw) if raw else {}
            assert ok and len(sent) == 1, raw[:30]
            got = json.loads(sent[0])["payload"]
            assert json.dumps(got, sort_keys=True) == json.dumps(want, sort_keys=True), (raw[:30], got, want)
        # An unpaired surrogate as raw UTF-8 bytes: json.loads accepts it (surrogatepass), but no
        # SQS message can carry it, so the enqueue must fail closed (504), not send garbage.
        lone = b'{"x": "\xed\xa0\x80"}'
        assert json.loads(lone)["x"] == "\ud800"
        sent.clear()
        assert relay_server._enqueue_promoted("c#k", lone, rec, None, "standard", "c", raw_is_json=True) is False
        assert not sent, "a message the worker cannot read was sent"
        assert relay_server._enqueue_promoted("c#k", b"{not json", rec, None, "standard", "c", raw_is_json=False) is False
    finally:
        boto3.client = real
        os.environ.pop("SQS_QUEUE_URL", None)
    print(f"OK 13 promotion messages carry the accepted body for {len(cases)} encodings and shapes; invalid bodies are not enqueued")


def test_promotion_end_to_end() -> None:
    """14. Through the real relay handler, with an upstream slower than the promotion deadline: an
    object body is promoted (202) under the job id the worker will compute from the message; an
    array body is not promoted at all, because its id could never match; and with every enqueue
    slot held a promotion fails fast (504) instead of queueing behind a slow SQS."""
    import importlib
    import jobkey
    import relay_server
    import http_bounds
    sent = []
    class _SQS:
        def send_message(self, QueueUrl, MessageBody):
            sent.append(MessageBody)
    import boto3
    real, boto3.client = boto3.client, (lambda *a, **k: _SQS())
    os.environ["SQS_QUEUE_URL"] = "https://sqs.example/000/q"
    saved = (relay_server.PROMOTION_ENABLED, relay_server.PROMOTION_DEADLINE_SECONDS, relay_server.ENQUEUE_WAIT_SECONDS)
    relay_server.PROMOTION_ENABLED, relay_server.PROMOTION_DEADLINE_SECONDS, relay_server.ENQUEUE_WAIT_SECONDS = True, 1, 0.5
    up = _serve(_Upstream)
    relay_server.GATEWAY_ENDPOINT = f"http://127.0.0.1:{up.server_port}"
    srv = _serve(None, http_bounds.BoundedThreadingHTTPServer(("127.0.0.1", 0), relay_server.Handler, 16))
    p = srv.server_port
    hdr = b"x-gateway-inference-objective: interactive\r\nx-caller-id: c1\r\n"
    try:
        UPSTREAM_GATE.clear()
        body = {"prompt": "promote me", "max_tokens": 16}
        st, h, b = _raw(p, _post(b"/v1/completions", json.dumps(body).encode(), hdr), timeout=15)
        assert st == 202 and len(sent) == 1, (st, h, len(sent))
        msg = json.loads(sent[0])
        assert h.get("x-relay-job-id") == msg["job_key"] == jobkey.job_key("c1", msg["payload"]), (h, msg["job_key"])
        assert msg["payload"] == body, msg["payload"]
        sent.clear()
        threading.Timer(2.0, UPSTREAM_GATE.set).start()
        st, h, _ = _raw(p, _post(b"/v1/completions", b'[{"prompt":"array"}]', hdr), timeout=15)
        assert st == 200 and not sent and "x-relay-job-id" not in h, (st, h, sent)
        UPSTREAM_GATE.clear()
        for _ in range(relay_server.MAX_CONCURRENT_ENQUEUES):
            relay_server._ENQUEUE_SLOTS.acquire()
        try:
            t0 = time.monotonic()
            st, h, _ = _raw(p, _post(b"/v1/completions", json.dumps({"prompt": "no slot"}).encode(), hdr), timeout=15)
            took = time.monotonic() - t0
            assert st == 504 and h.get("x-relay-reason") == "promotion-enqueue-failed" and took < 3, (st, h, took)
        finally:
            for _ in range(relay_server.MAX_CONCURRENT_ENQUEUES):
                relay_server._ENQUEUE_SLOTS.release()
    finally:
        UPSTREAM_GATE.set()
        relay_server.PROMOTION_ENABLED, relay_server.PROMOTION_DEADLINE_SECONDS, relay_server.ENQUEUE_WAIT_SECONDS = saved
        boto3.client = real
        os.environ.pop("SQS_QUEUE_URL", None)
        srv.shutdown(); srv.server_close(); up.shutdown(); up.server_close()
    print("OK 14 an object body is promoted under the id the worker computes; an array body is not promoted; "
          "a held enqueue slot fails fast with 504")


def test_word_count() -> None:
    import relay
    old = re.compile(r"\S+")
    samples = ["", " ", "one", "two  words", "tabs\tand\nnewlines\r\nmixed", "unicode wörter 単語 ok",
               "a " * 5000, "  leading and trailing  ", json.dumps({"k": ["v"] * 50})]
    for s in samples:
        assert relay.estimate_prompt_tokens(s) == -(-len(old.findall(s)) * 4 // 3), repr(s[:40])
    big = "ab " * (1024 * 1024 // 3)
    tracemalloc.start()
    relay.estimate_prompt_tokens(big)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert peak < 256 * 1024, f"the word count still allocates per word: peak {peak} bytes"
    print(f"OK 10 the word count equals the list-based count on {len(samples)} samples; 1 MiB prompt peak {peak} bytes")


def test_submit_api() -> None:
    import boto3
    from moto import mock_aws
    with mock_aws():
        q = boto3.client("sqs").create_queue(QueueName="t")["QueueUrl"]
        os.environ["SQS_QUEUE_URL"] = q
        import importlib
        submit_api = importlib.import_module("submit_api")
        import http_bounds
        assert http_bounds.DEFAULT_MAX_BODY_BYTES == 1024 * 1024
        assert submit_api.SUBMITTABLE_CAPABILITIES == ("economy", "standard"), submit_api.SUBMITTABLE_CAPABILITIES
        srv = _serve(submit_api.Handler)
        p = srv.server_port
        try:
            ok = json.dumps({"client_id": "c", "payload": {"prompt": "x"}}).encode()
            st, _, b = _raw(p, _post(b"/submit", ok))
            assert st == 202 and "job_id" in json.loads(b), (st, b)
            big = json.dumps({"client_id": "c", "payload": {"prompt": "ab " * 2000}}).encode()
            st, _, b = _raw(p, _post(b"/submit", big))
            assert st == 413 and json.loads(b)["reason"] == "body-too-large", (st, b)
            st, _, b = _raw(p, b"POST /submit HTTP/1.1\r\nHost: t\r\ntransfer-encoding: chunked\r\n\r\n0\r\n\r\n")
            assert st == 411, (st, b)
            st, _, b = _raw(p, b"POST /submit HTTP/1.1\r\nHost: t\r\ncontent-length: -1\r\n\r\n")
            assert st == 400 and json.loads(b)["reason"] == "content-length-invalid", (st, b)
            st, _, b = _raw(p, b"POST /submit HTTP/1.1\r\nHost: t\r\nnot a header line\r\ncontent-length: 2\r\n\r\n{}")
            assert st == 400, (st, b)
            import concurrent.futures as cf
            def one(i):
                d = json.dumps({"client_id": "c", "payload": {"prompt": f"concurrent-{i}"}}).encode()
                return _raw(p, _post(b"/submit", d))[0]
            with cf.ThreadPoolExecutor(max_workers=60) as ex:
                codes = list(ex.map(one, range(60)))
            assert codes.count(202) == 60, codes
            print("OK 11 the submit API applies the same size, framing and header bounds (413, 411, 400) "
                  "and serves 60 concurrent submissions through its parse slots")
            for cap, want in (("premium", 400), ("turbo", 400), (["standard"], 400),
                              ("economy", 202), ("standard", 202), ("", 202), (None, 202)):
                d = {"client_id": "c", "payload": {"prompt": f"cap-{cap}"}}
                if cap is not None:
                    d["capability"] = cap
                st, _, b = _raw(p, _post(b"/submit", json.dumps(d).encode()))
                assert st == want, (cap, st, b)
                if st == 400:
                    assert "turbo" not in b.decode(), "the caller's value was echoed back"
            print("OK 12 submit API refuses premium (promotion-only) and unknown values; economy, standard, absent accepted")
        finally:
            srv.shutdown(); srv.server_close()


def main() -> int:
    test_relay()
    test_promotion_message()
    test_promotion_end_to_end()
    test_word_count()
    test_submit_api()
    print("PASS: both front doors bound size, framing, time and concurrency before a body costs memory")
    return 0


if __name__ == "__main__":
    sys.exit(main())
