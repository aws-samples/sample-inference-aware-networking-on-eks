# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Deterministic job identity, the single source of truth.

job_key() is the ONE definition of a logical job's identity, shared by the producer
(submit_api.py) and the consumers (jobstore.py, and through it worker.py and
sweeper.py). It used to be copied byte for byte into two modules, guarded only by a
comment. A silent drift between the copies would break deduplication: the submit API
and the claim path would disagree on identity, so a duplicate submission would hash
to a different key and run a second time on a GPU. This module removes that risk by
making both importers reference the same function. The equivalence is pinned by
tests/validation/test_job_key.py.

It deliberately has NO third-party dependency (no boto3), so importing it carries no
runtime configuration or client setup, and any module can use it freely.
"""

import hashlib
import json


def job_key(client_id: str, payload: dict) -> str:
    """Stable identity for a logical job. Two submissions with the same client and
    the same request body hash to the same key and therefore the same job. The
    serialization is canonical (sort_keys, no whitespace) so that logically identical
    payloads with different key ordering or spacing map to the same identity."""
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    request_hash = hashlib.sha256(body).hexdigest()[:32]
    return f"{client_id}#{request_hash}"
