# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Pins job identity (defect B.3).

job_key() decides whether two submissions are the same logical job. It used to be copied
byte for byte into jobstore.py and submit_api.py, guarded only by a comment. If the copies
drifted, the producer and the claim path would disagree on identity and a duplicate would run
a second time on a GPU, silently breaking the headline exactly-once guarantee. The fix moved
the definition into jobkey.py and made both modules import it. This test proves two things:

1/ Identity: jobstore.job_key and submit_api.job_key are the SAME object as jobkey.job_key, so
   they cannot drift (there is only one function).
2/ Behavior: the function is deterministic and canonical, so logically identical payloads that
   differ only in key ordering or whitespace map to the same key, across unicode and nesting.

Runs with no AWS calls (job_key is pure); GPU-free.

Run:  ./.venv/bin/python tests/validation/test_job_key.py
"""
import os
import sys

os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")
os.environ.setdefault("SQS_QUEUE_URL", "http://sqs.local/queue")  # submit_api reads this at import
os.environ.setdefault("DDB_TABLE", "inference-jobs")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "async-worker"))
import importlib

jobkey = importlib.import_module("jobkey")
jobstore = importlib.import_module("jobstore")
submit_api = importlib.import_module("submit_api")


def main() -> int:
    # ---- 1. same object in all three modules: drift is impossible ----
    assert jobstore.job_key is jobkey.job_key, "jobstore must import job_key from jobkey, not redefine it"
    assert submit_api.job_key is jobkey.job_key, "submit_api must import job_key from jobkey, not redefine it"
    jk = jobkey.job_key
    print("OK  1 jobstore.job_key and submit_api.job_key are the one jobkey.job_key (cannot drift)")

    # ---- 2. key ordering does not change identity ----
    assert jk("c", {"a": 1, "b": 2}) == jk("c", {"b": 2, "a": 1}), "top-level key order must not matter"

    # ---- 3. nested structures and unicode are handled, order-independent throughout ----
    p_a = {"model": "m", "messages": [{"role": "user", "content": "héllo wörld"}], "temperature": 0.7}
    p_b = {"temperature": 0.7, "messages": [{"content": "héllo wörld", "role": "user"}], "model": "m"}
    assert jk("c", p_a) == jk("c", p_b), "nested + unicode payloads that are logically equal must match"

    # ---- 4. genuinely different payloads produce different keys ----
    assert jk("c", {"a": 1}) != jk("c", {"a": 2}), "different payloads must differ"
    assert jk("c", {"a": 1}) != jk("c", {"a": "1"}), "1 (int) and \"1\" (str) are different payloads"

    # ---- 5. client_id namespaces the identity ----
    assert jk("c1", {"a": 1}) != jk("c2", {"a": 1}), "same payload, different client, different job"

    # ---- 6. shape of the key: client_id#<32 hex> ----
    k = jk("client", {"a": 1})
    prefix, _, h = k.partition("#")
    assert prefix == "client", "key must be prefixed by client_id"
    assert len(h) == 32 and all(c in "0123456789abcdef" for c in h), "hash must be 32 lowercase hex chars"

    # ---- 7. determinism across calls ----
    assert jk("c", p_a) == jk("c", p_a), "must be deterministic"
    print("OK  2-7 canonical, order-independent, unicode-safe, namespaced, deterministic")

    print("PASS: job identity is single-sourced and stable")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
