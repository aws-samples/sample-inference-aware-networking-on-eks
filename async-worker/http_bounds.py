# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Request bounds shared by the two HTTP front doors: the relay (relay_server.py) and the submit API
(submit_api.py).

WHY THIS EXISTS. Both servers once read `Content-Length` bytes straight into memory with no upper
bound, so one large request could exhaust a pod's memory before any application logic ran. The
deep security scan of 2026-11-05 (finding M1) reproduced it offline: a 22 MB body peaked at about
400 MB inside the relay against a 512 Mi limit. A refusal computed AFTER the body is in memory
protects nothing, so every bound here applies before the bytes it bounds are held.

WHAT IT BOUNDS, per replica, and nothing else. This is a resource limit, not validation: a request
within the bounds reaches the handler unchanged, exactly as before.
  1. Connections. BoundedThreadingHTTPServer accepts at most `max_connections` at once; the next
     one gets 503 at once and is closed. Every other bound is per connection, so this one caps
     their sum.
  2. Header block. The request line and headers of each request draw on a byte budget (431-class
     close) and a deadline that starts when the server begins waiting for the request, so an idle
     keep-alive connection is also closed at the deadline. A malformed block (a line with no colon,
     which makes the parser hide every later header, including Content-Length) is refused with 400.
  3. Body size and framing. A declared length above the cap is refused with 413, unread. A
     malformed or conflicting Content-Length gets 400, and any Transfer-Encoding 411, because
     http.server does not decode chunked bodies.
  4. Body time. The whole body must arrive within a deadline (408); the socket timeout is tightened
     to the time left, so a stalled sender is cut at the deadline too.
Refusals after the header block close the connection gracefully (half-close, then a short bounded
drain), so the client reads the status instead of a reset caused by its unread upload.

What it does NOT bound: callers. One host that can reach the server can still occupy all of a
replica's connection slots, so new requests get 503 and readiness fails while it does. Liveness is
a TCP probe, which the accept loop always answers, so a saturated replica leaves rotation but is
not restarted. Per-caller rate limits belong in the identity layer in front.
"""
import io
import re
import socket
import threading
import time
from http.server import ThreadingHTTPServer

# 1 MiB. The largest deployed capability class holds 32,768 tokens, which is about 128 KiB of
# English prompt text and well under 512 KiB even for JSON-escaped non-Latin text. The cap is
# several times the largest servable request, so no request a backend could serve is refused here.
DEFAULT_MAX_BODY_BYTES = 1024 * 1024
DEFAULT_BODY_READ_DEADLINE_SECONDS = 30.0
# Request line plus headers. A normal request uses well under 2 KiB.
DEFAULT_HEADER_BUDGET_BYTES = 64 * 1024
DEFAULT_HEADER_DEADLINE_SECONDS = 10.0
DEFAULT_MAX_CONNECTIONS = 64

_DIGITS = re.compile(r"[0-9]{1,19}")
_CHUNK = 64 * 1024
_LINGER_SECONDS = 1.0
_LINGER_MAX_BYTES = 2 * DEFAULT_MAX_BODY_BYTES


class BodyRejected(Exception):
    """The request body was refused before or while it was read. Carries the status and reason."""

    def __init__(self, status: int, reason: str):
        super().__init__(f"{status} {reason}")
        self.status = status
        self.reason = reason


class HeaderBoundExceeded(TimeoutError):
    """Raised while the header block is read. http.server handles a TimeoutError raised there by
    closing the connection without a reply, which is the intended outcome for an abusive sender."""


def headers_malformed(headers) -> bool:
    """True when the header block is not one Python's parser read cleanly. Each case can hide or
    smuggle a framing header past the checks below:
      - a line with no colon: the parser treats every following line as payload (defects/payload);
      - a first line starting with "From ": taken as a Unix mailbox separator and dropped (unixfrom);
      - a bare CR or LF inside a name or value: a line split another parser may honour, or an
        obs-fold continuation that glues a Transfer-Encoding onto another header's value.
    An HTTP/0.9 request has no header block. Python 3.14's http.server then sets a plain dict, which
    is not malformed; Python 3.12, the image runtime, parses an empty block, which is not either."""
    if not hasattr(headers, "get_payload"):
        return False
    if getattr(headers, "defects", None) or headers.get_payload() or headers.get_unixfrom():
        return True
    return any("\r" in k or "\n" in k or "\r" in v or "\n" in v for k, v in headers.items())


def read_body(rfile, headers, max_bytes: int, deadline_seconds: float, sock=None) -> bytes:
    """Return the request body, or raise BodyRejected. Reads at most `max_bytes`.

    `headers` is the handler's `self.headers` (an email.message.Message). A request with no
    Content-Length and no Transfer-Encoding has an empty body, which is the behaviour both servers
    had before this module existed. With `sock`, each read waits at most for the time left.
    """
    if headers_malformed(headers):
        raise BodyRejected(400, "header-malformed")
    if headers.get("transfer-encoding") is not None:
        raise BodyRejected(411, "transfer-encoding-unsupported")
    values = headers.get_all("content-length") or []
    if not values:
        return b""
    distinct = {v.strip() for v in values}
    if len(distinct) != 1:
        raise BodyRejected(400, "content-length-conflicting")
    declared = distinct.pop()
    if not _DIGITS.fullmatch(declared):
        raise BodyRejected(400, "content-length-invalid")
    length = int(declared)
    if length > max_bytes:
        raise BodyRejected(413, "body-too-large")
    if length == 0:
        return b""
    deadline = time.monotonic() + deadline_seconds
    restore = sock.gettimeout() if sock is not None else None
    chunks, remaining = [], length
    try:
        while remaining:
            left = deadline - time.monotonic()
            if left <= 0:
                raise BodyRejected(408, "body-read-deadline")
            if sock is not None:
                sock.settimeout(left if restore is None else min(restore, left))
            chunk = rfile.read1(min(remaining, _CHUNK))
            if not chunk:
                raise BodyRejected(400, "body-truncated")
            chunks.append(chunk)
            remaining -= len(chunk)
    except (TimeoutError, OSError) as exc:
        raise BodyRejected(408, "body-read-deadline") from exc
    finally:
        if sock is not None:
            try:
                sock.settimeout(restore)
            except OSError:
                pass
    return b"".join(chunks)


class _BudgetedReader:
    """Wraps a handler's rfile. readline(), which http.server uses for the request line and the
    headers, draws on a per-request byte budget and deadline. read1() and read(), the body path,
    pass through; the body has its own bounds in read_body."""

    def __init__(self, raw, sock, budget: int, deadline_seconds: float, idle_timeout, on_exceeded):
        self._raw, self._sock = raw, sock
        self._budget, self._deadline_seconds = budget, deadline_seconds
        self._idle, self._on_exceeded = idle_timeout, on_exceeded
        self._buf = b""
        self.start_request()

    def start_request(self) -> None:
        self._left = self._budget
        self._deadline = time.monotonic() + self._deadline_seconds
        self._got = len(self._buf)
        # A CR that is not part of a CRLF line ending. Python's header parser treats it as a line
        # break, so "x-a: a\rcontent-length: 5" becomes two headers here while a stricter parser in
        # front of this server may see one: the framing would differ between the two.
        self.bare_cr = False

    def _exceeded(self, reason: str):
        # An idle keep-alive connection that reaches the deadline without sending a byte of a new
        # request is ordinary client behaviour, so it is closed without being counted as abuse.
        if reason == "header-deadline" and self._got == 0:
            return HeaderBoundExceeded("idle-keepalive")
        if self._on_exceeded is not None:
            self._on_exceeded(reason)
        return HeaderBoundExceeded(reason)

    def readline(self, limit=-1) -> bytes:
        while True:
            i = self._buf.find(b"\n")
            capped = limit is not None and limit >= 0
            if i >= 0 or (capped and len(self._buf) >= limit):
                end = i + 1 if i >= 0 else len(self._buf)
                if capped:
                    end = min(end, limit)
                line, self._buf = self._buf[:end], self._buf[end:]
                self._left -= len(line)
                if self._left < 0:
                    raise self._exceeded("header-too-large")
                content = line[:-2] if line.endswith(b"\r\n") else line.rstrip(b"\n")
                if b"\r" in content:
                    self.bare_cr = True
                return line
            if len(self._buf) > self._left:
                raise self._exceeded("header-too-large")
            left = self._deadline - time.monotonic()
            if left <= 0:
                raise self._exceeded("header-deadline")
            try:
                self._sock.settimeout(left if self._idle is None else min(self._idle, left))
                chunk = self._raw.read1(_CHUNK)
            except TimeoutError:
                raise self._exceeded("header-deadline") from None
            finally:
                try:
                    self._sock.settimeout(self._idle)
                except OSError:
                    pass
            if not chunk:
                line, self._buf = self._buf, b""
                return line
            self._got += len(chunk)
            self._buf += chunk

    def read1(self, n=-1) -> bytes:
        if self._buf:
            if n is None or n < 0:
                out, self._buf = self._buf, b""
            else:
                out, self._buf = self._buf[:n], self._buf[n:]
            return out
        return self._raw.read1(_CHUNK if n is None or n < 0 else n)

    def read(self, n=-1) -> bytes:
        if n is None or n < 0:
            raise io.UnsupportedOperation("unbounded read; use read_body")
        out, self._buf = self._buf[:n], self._buf[n:]
        if len(out) < n:
            out += self._raw.read(n - len(out))
        return out

    # Only what http.server needs is exposed. Anything else (readinto, peek, readlines) would read
    # the raw stream past bytes held in _buf, so it is not forwarded.
    def close(self) -> None:
        self._raw.close()

    @property
    def closed(self) -> bool:
        return self._raw.closed


class BoundedRequestMixin:
    """Header-block bounds and graceful refusal for a BaseHTTPRequestHandler subclass. Put it
    BEFORE BaseHTTPRequestHandler in the bases. Set the two class attributes from configuration."""

    header_budget_bytes = DEFAULT_HEADER_BUDGET_BYTES
    header_deadline_seconds = DEFAULT_HEADER_DEADLINE_SECONDS
    max_body_bytes = DEFAULT_MAX_BODY_BYTES

    def count_bounded(self, reason: str) -> None:
        """Hook for a metric. The default records nothing."""

    def setup(self) -> None:
        super().setup()
        self.rfile = _BudgetedReader(self.rfile, self.connection, self.header_budget_bytes,
                                     self.header_deadline_seconds, self.timeout, self.count_bounded)

    def handle_one_request(self) -> None:
        self.rfile.start_request()
        super().handle_one_request()

    def parse_request(self) -> bool:
        if not super().parse_request():
            return False
        if headers_malformed(self.headers) or getattr(self.rfile, "bare_cr", False):
            self.count_bounded("header-malformed")
            self.close_connection = True
            self.send_error(400, "malformed header block")
            self.linger_close()
            return False
        return True

    def handle_expect_100(self) -> bool:
        """Answer `Expect: 100-continue` with the refusal, not `100 Continue`, when the declared
        body is already known to be refused, so the client is not invited to upload it. http.server
        calls this only for HTTP/1.1 handlers (the relay); the submit API speaks HTTP/1.0."""
        declared = (self.headers.get("content-length") or "").strip()
        if self.headers.get("transfer-encoding") is not None or (
                _DIGITS.fullmatch(declared) and int(declared) > self.max_body_bytes):
            self.count_bounded("expect-refused")
            self.close_connection = True
            self.send_error(413 if declared else 411, "request body refused")
            return False
        return super().handle_expect_100()

    def linger_close(self) -> None:
        """Close after a refusal without resetting the connection: half-close, then drain what the
        client is still sending, bounded in time and bytes. A close with unread data makes the kernel
        send a reset, and the client would see that instead of the status it was sent."""
        self.close_connection = True
        try:
            self.wfile.flush()
            self.connection.shutdown(socket.SHUT_WR)
            deadline, drained = time.monotonic() + _LINGER_SECONDS, 0
            while drained < _LINGER_MAX_BYTES:
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                self.connection.settimeout(left)
                chunk = self.connection.recv(_CHUNK)
                if not chunk:
                    break
                drained += len(chunk)
        except OSError:
            pass


_CONN_CAP_BODY = b'{"error":"server connection limit reached","reason":"connection-cap"}'
_CONN_CAP_RESPONSE = (
    b"HTTP/1.1 503 Service Unavailable\r\ncontent-type: application/json\r\nretry-after: 1\r\n"
    b"x-relay-reason: bounded:connection-cap\r\nconnection: close\r\ncontent-length: "
    + str(len(_CONN_CAP_BODY)).encode() + b"\r\n\r\n" + _CONN_CAP_BODY)


class BoundedThreadingHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer with at most `max_connections` connections at once. One past the limit
    is answered 503 from the accept loop, without a thread, and closed."""

    daemon_threads = True

    def __init__(self, address, handler, max_connections: int = DEFAULT_MAX_CONNECTIONS, on_refused=None):
        self._slots = threading.BoundedSemaphore(max_connections)
        self._on_refused = on_refused
        super().__init__(address, handler)

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            if self._on_refused is not None:
                self._on_refused()
            try:
                request.sendall(_CONN_CAP_RESPONSE)
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()
