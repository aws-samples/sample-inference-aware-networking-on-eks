# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Regression test for the intermittent worker-latency defect (DEFECT-worker-latency.md).

Two guards that would have caught the two hypotheses:
1/ H2 (connection churn): the worker must issue inference calls through a reused module-level
   requests.Session (keep-alive), NOT a bare requests.post that opens a new connection per call.
2/ H1 (in-batch head-of-line blocking): the SQS_BATCH_SIZE default must be 1, so a default deploy
   cannot let messages 2..N in a receive batch wait behind a long message 1.

No AWS calls, no GPU. Run:  ./.venv/bin/python tests/validation/test_worker_http_session.py
"""
import os
import sys

os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")
os.environ.setdefault("GATEWAY_ENDPOINT", "http://gateway.inference.svc.cluster.local/v1")
os.environ.setdefault("DDB_TABLE", "inference-jobs")
os.environ.setdefault("SQS_QUEUE_URL", "http://sqs.local/q")
os.environ.pop("SQS_BATCH_SIZE", None)  # ensure we test the DEFAULT

import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "async-worker"))
import importlib
worker = importlib.import_module("worker")


class _FakeResp:
    status_code = 200
    headers = {}
    def json(self):
        return {"choices": [{"message": {"content": "ok"}}]}
    def raise_for_status(self):
        pass


def main() -> int:
    # ---- 1. a reused module-level Session exists ----
    assert hasattr(worker, "_HTTP"), "worker must hold a module-level HTTP session (_HTTP)"
    assert isinstance(worker._HTTP, requests.Session), "_HTTP must be a requests.Session (keep-alive pool)"
    print("OK  1 worker uses a module-level requests.Session (keep-alive), not per-call connections")

    # ---- 2. SQS_BATCH_SIZE default is 1 ----
    assert worker.BATCH_SIZE == 1, f"SQS_BATCH_SIZE default must be 1 (no in-batch head-of-line), got {worker.BATCH_SIZE}"
    print("OK  2 SQS_BATCH_SIZE default is 1")

    # ---- 3. run_inference goes through the Session, never bare requests.post ----
    calls = {"session": 0, "bare": 0}
    orig_session_post = worker._HTTP.post
    def spy_session_post(*a, **k):
        calls["session"] += 1
        return _FakeResp()
    worker._HTTP.post = spy_session_post
    orig_requests_post = requests.post
    def forbidden_post(*a, **k):
        calls["bare"] += 1
        raise AssertionError("run_inference must not call bare requests.post")
    requests.post = forbidden_post
    try:
        payload = {"model": "m", "messages": [{"role": "user", "content": "x"}]}
        worker.run_inference(payload, correlation_id="c#1")
        worker.run_inference(payload, correlation_id="c#2")
    finally:
        worker._HTTP.post = orig_session_post
        requests.post = orig_requests_post
    assert calls["session"] == 2, f"both calls must go through the session, got {calls['session']}"
    assert calls["bare"] == 0, "no call may use bare requests.post"
    print("OK  3 run_inference issues both calls through the one reused session (0 bare posts)")

    # ---- 4. the retry policy is connect-only: retry CONNECT, never READ or STATUS ----
    retry = worker._HTTP.get_adapter("http://x/").max_retries
    assert retry.connect and retry.connect >= 1, "must retry connect errors (request never reached the server)"
    assert retry.read == 0, "must NOT retry read errors (a retried POST could double-execute on a GPU)"
    assert retry.status == 0, "must NOT retry on 5xx status (a 5xx must surface as an InfraRejection)"
    assert "POST" in retry.allowed_methods, "POST must be retryable (connect only, since read/status are 0)"
    print("OK  4 adapter retries connect only (connect>=1, read=0, status=0, POST allowed)")

    # ---- 5. behavioral: a READ error is not retried (one POST reaches the server) and becomes
    #         an InfraRejection exactly once ----
    import http.server, socketserver, threading, time as _t
    hits = {"n": 0}

    class _Stub(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            hits["n"] += 1
            # Deliberate fixed wait in a measurement or test script: never respond within the client read timeout.
            _t.sleep(3)  # nosemgrep
            try:
                self.send_response(200); self.end_headers()
            except Exception:
                pass
        def log_message(self, *a):
            pass

    srv = socketserver.TCPServer(("127.0.0.1", 0), _Stub)
    port = srv.server_address[1]
    th = threading.Thread(target=srv.serve_forever, daemon=True); th.start()
    orig_gw, orig_to = worker.GATEWAY_ENDPOINT, worker.HTTP_TIMEOUT_SECONDS
    worker.GATEWAY_ENDPOINT = f"http://127.0.0.1:{port}"
    worker.HTTP_TIMEOUT_SECONDS = 1  # short read timeout so the stub's 3s sleep trips it
    try:
        raised = False
        try:
            worker.run_inference({"model": "m", "messages": [{"role": "user", "content": "x"}]}, "c#read")
        except worker.InfraRejection:
            raised = True
        assert raised, "a read timeout must surface as an InfraRejection"
        assert hits["n"] == 1, f"a read error must NOT be retried: the server must see exactly 1 POST, saw {hits['n']}"
    finally:
        worker.GATEWAY_ENDPOINT, worker.HTTP_TIMEOUT_SECONDS = orig_gw, orig_to
        srv.shutdown(); srv.server_close()
    print("OK  5 read timeout -> InfraRejection, exactly 1 POST reached the server (no read retry)")

    print("PASS: worker HTTP path is pooled/keep-alive, connect-only retry, BATCH_SIZE default is 1")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
