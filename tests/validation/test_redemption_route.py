# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Offline proof for the redemption route, GET /jobs/{job_id} (design v6.1 section 5.1 step 5).

Runs against a mocked DynamoDB (moto) and a REAL http.server bound to a loopback port, so
it needs no cluster, no GPU and no AWS credentials. The HTTP layer is exercised rather than
simulated on purpose: the percent-encoding behaviour this route depends on is a property of
URL parsing, and a test that called the handler function directly would prove nothing about
it.

What it proves:
  1. Every state a caller must act on differently is distinguishable: pending, running,
     complete, failed, and not-found.
  2. A job id containing '#' round-trips when percent-encoded, and the literal form is
     shown to truncate, which is the production failure mode.
  3. No internal attribute leaks into a response.
  4. An unknown id is distinguishable from a known id whose result is not ready.

Run:  ./.venv/bin/python tests/validation/test_redemption_route.py
"""
import json
import os
import threading
import urllib.error
import urllib.request
from urllib.parse import quote

os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_SESSION_TOKEN", "testing")
os.environ.setdefault("SQS_QUEUE_URL", "https://sqs.us-west-2.amazonaws.com/000/x")
os.environ.setdefault("DDB_TABLE", "inference-jobs")

import sys

import boto3
from moto import mock_aws


def _urlopen_http(target, *args, **kwargs):
    """urllib.request.urlopen restricted to http and https URLs (bandit B310)."""
    import urllib.request
    url = getattr(target, "full_url", target)
    if not str(url).lower().startswith(("http://", "https://")):
        raise ValueError("refusing a non-HTTP URL: %r" % (url,))
    return urllib.request.urlopen(target, *args, **kwargs)  # nosec B310 - scheme checked above


TABLE = os.environ["DDB_TABLE"]


def _get(url: str) -> tuple[int, dict]:
    """Fetch a URL, returning (status, parsed body). A 4xx is a result here, not an error."""
    try:
        with _urlopen_http(url, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


@mock_aws
def main() -> int:
    boto3.client("dynamodb").create_table(
        TableName=TABLE,
        AttributeDefinitions=[{"AttributeName": "job_id", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "job_id", "KeyType": "HASH"}],
        BillingMode="PAY_PER_REQUEST",
    )
    # Import AFTER the mock and table exist, so the module-level boto3 clients are mocked.
    import importlib
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "async-worker"))
    submit_api = importlib.import_module("submit_api")
    jobstore = importlib.import_module("jobstore")
    ddb = jobstore.ddb
    now = jobstore.now_epoch()

    # Bind to port 0 so the OS picks a free port: a hardcoded port makes a test suite fail
    # for the wrong reason on a busy machine.
    from http.server import ThreadingHTTPServer
    srv = ThreadingHTTPServer(("127.0.0.1", 0), submit_api.Handler)
    base = f"http://127.0.0.1:{srv.server_port}"
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        # ---- 1. unknown id -> 404, and it says WHICH two cases it cannot separate -------
        code, body = _get(f"{base}/jobs/{quote('nobody#ffff', safe='')}")
        assert code == 404, f"unknown id should 404, got {code}"
        assert body["status"] == "not_found", body
        assert "not yet been claimed" in body["detail"], "404 must disclose the ambiguity"
        print("OK  1 unknown id: 404 not_found, ambiguity disclosed")

        # ---- 2. running: processing with a LIVE lease -----------------------------------
        k_run = "c1#" + "a" * 32
        ddb.put_item(TableName=TABLE, Item={
            "job_id": {"S": k_run}, "status": {"S": "processing"},
            "lease_expires": {"N": str(now + 600)}, "claim_count": {"N": "1"},
            "claim_token": {"S": "secret-token"}, "payload": {"S": '{"model":"x"}'},
            "client_id": {"S": "c1"}, "updated_at": {"S": "2026-08-02T00:00:00+00:00"},
        })
        code, body = _get(f"{base}/jobs/{quote(k_run, safe='')}")
        assert code == 200 and body["status"] == "running", body
        assert body["attempts"] == 1, body
        assert body["delivery"] == "declared_async", "promotion field must be present now"
        print("OK  2 running: live lease reported as running")

        # ---- 3. pending: processing with an EXPIRED lease (released or orphaned) --------
        k_pend = "c1#" + "b" * 32
        ddb.put_item(TableName=TABLE, Item={
            "job_id": {"S": k_pend}, "status": {"S": "processing"},
            "lease_expires": {"N": "0"}, "claim_count": {"N": "2"},
        })
        code, body = _get(f"{base}/jobs/{quote(k_pend, safe='')}")
        assert code == 200 and body["status"] == "pending", body
        assert body["attempts"] == 2, body
        print("OK  3 pending: expired lease distinguished from running by the lease, not status")

        # ---- 4. complete: result parsed back into an object -----------------------------
        k_done = "c1#" + "c" * 32
        ddb.put_item(TableName=TABLE, Item={
            "job_id": {"S": k_done}, "status": {"S": "complete"},
            "lease_expires": {"N": str(now + 600)}, "claim_count": {"N": "1"},
            "result": {"S": json.dumps({"choices": [{"text": "hello"}]})},
            "submitted_at": {"S": "1785000000"}, "completed_at": {"S": "1785000061"},
            "claim_token": {"S": "secret-token"},
        })
        code, body = _get(f"{base}/jobs/{quote(k_done, safe='')}")
        assert code == 200 and body["status"] == "complete", body
        assert body["result"]["choices"][0]["text"] == "hello", "result must be parsed JSON"
        assert body["completed_at"] == "1785000061", body
        print("OK  4 complete: terminal state carries the parsed result")

        # ---- 5. failed: error surfaced, retryability stated as of NOW -------------------
        k_fail = "c1#" + "d" * 32
        ddb.put_item(TableName=TABLE, Item={
            "job_id": {"S": k_fail}, "status": {"S": "failed"},
            "lease_expires": {"N": str(now + 600)}, "claim_count": {"N": "1"},
            "error": {"S": "400 Bad Request"},
        })
        code, body = _get(f"{base}/jobs/{quote(k_fail, safe='')}")
        assert code == 200 and body["status"] == "failed", body
        assert body["error"] == "400 Bad Request", body
        assert body["retryable"] is False, "lease still live: not re-claimable yet"
        ddb.update_item(
            TableName=TABLE, Key={"job_id": {"S": k_fail}},
            UpdateExpression="SET lease_expires = :z", ExpressionAttributeValues={":z": {"N": "0"}},
        )
        _, body = _get(f"{base}/jobs/{quote(k_fail, safe='')}")
        assert body["retryable"] is True, "expired lease: re-claimable now"
        print("OK  5 failed: error surfaced, retryable tracks the lease")

        # ---- 6. THE production trap: '#' must be percent-encoded ------------------------
        # Encoded: the full id reaches the server and resolves.
        code, body = _get(f"{base}/jobs/{quote(k_done, safe='')}")
        assert code == 200 and body["job_id"] == k_done, "percent-encoded id must round-trip"
        # Literal: the client strips everything from '#' as a fragment and never sends it,
        # so the server sees a TRUNCATED id. This is the real-world breakage, pinned here.
        code, body = _get(f"{base}/jobs/{k_done}")
        assert code == 404, f"literal '#' truncates the id, so it must 404, got {code}"
        assert body["job_id"] == "c1", f"server saw only the pre-fragment part: {body['job_id']}"
        print("OK  6 percent-encoded '#' round-trips; literal '#' truncates to a 404 (pinned)")

        # ---- 7. no internal attribute leaks ---------------------------------------------
        for k in (k_run, k_done, k_fail):
            _, body = _get(f"{base}/jobs/{quote(k, safe='')}")
            leaked = submit_api._INTERNAL_ONLY & set(body)
            assert not leaked, f"internal attributes leaked for {k}: {leaked}"
            assert "claim_token" not in json.dumps(body), "ownership credential must never appear"
        print("OK  7 no internal attribute leaked (allow-list projection holds)")

        # ---- 8. query string tolerated, empty id rejected -------------------------------
        code, _ = _get(f"{base}/jobs/{quote(k_done, safe='')}?poll=1")
        assert code == 200, "a query string must not turn a valid route into a 404"
        code, body = _get(f"{base}/jobs/")
        assert code == 400 and body["error"] == "missing job_id", body
        print("OK  8 query string tolerated, empty job id rejected with 400")

        # ---- 9. a malformed submission gets a FIXED 400 message, never exception text -----
        # Added 2026-11-02 after a content security scan noted the 400 echoed the parser's
        # exception. A JSON body that is not an object used to escape as an unhandled TypeError.
        def _post(raw: bytes) -> tuple[int, dict]:
            req = urllib.request.Request(f"{base}/submit", data=raw, method="POST",
                                         headers={"content-type": "application/json"})
            try:
                with _urlopen_http(req, timeout=10) as resp:
                    return resp.status, json.loads(resp.read())
            except urllib.error.HTTPError as e:
                return e.code, json.loads(e.read())
        for raw, want in ((b"{not json", "bad request: body is not valid JSON"),
                          (b'{"payload": {}}', "bad request: missing field client_id"),
                          (b'{"client_id": "c"}', "bad request: missing field payload"),
                          (b'["a list"]', "bad request: body must be a JSON object")):
            code, body = _post(raw)
            assert code == 400 and body == {"error": want}, (raw, code, body)
        print("OK  9 malformed submissions get a fixed 400 message, never exception text")

        print("PASS: redemption route distinguishes every actionable state and leaks nothing")
        return 0
    finally:
        srv.shutdown()
        srv.server_close()


if __name__ == "__main__":
    raise SystemExit(main())
