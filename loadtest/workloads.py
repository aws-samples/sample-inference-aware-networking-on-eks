# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Seeded, deterministic request corpora for the load-test suite (Phase 2.3).

Everything is derived from a fixed SEED so runs are reproducible and prefix sharing
is controlled. Four workloads:

  interactive-short : short prompt, short max_tokens, latency sensitive (sync tier).
  batch-long        : long document prompt, long max_tokens, async submit path.
  prefix-heavy      : many requests sharing ONE long system prompt, to exercise
                      prefix-cache-aware routing (where the EPP should beat round-robin).
  reasoning-long    : prompts that induce long generations, to exercise streaming
                      and the idle-timeout / keepalive path.

Model is read from LOADTEST_MODEL (default the design target). No network here; this
module only builds request bodies.
"""
import hashlib
import os
import random

SEED = int(os.environ.get("LOADTEST_SEED", "1337"))
# Must equal the served model name or vLLM returns 400 (model not found). This pass
# serves the ungated mirror; override with LOADTEST_MODEL for the official repo.
MODEL = os.environ.get("LOADTEST_MODEL", "NousResearch/Meta-Llama-3.1-8B-Instruct")

_WORDS = (
    "system throughput latency cache token prefix batch interactive routing kernel "
    "tensor vllm inference cluster scheduler request replica gateway endpoint scorer "
    "queue memory bandwidth prefill decode attention sequence context window sampler"
).split()


def _rng(tag: str) -> random.Random:
    # Deterministic per-tag stream so each workload is stable across runs and hosts.
    return random.Random(f"{SEED}:{tag}")


def _lorem(r: random.Random, n: int) -> str:
    return " ".join(r.choice(_WORDS) for _ in range(n))


def _chat(system: str, user: str, max_tokens: int, stream: bool):
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.append({"role": "user", "content": user})
    return {"model": MODEL, "messages": msgs, "max_tokens": max_tokens, "stream": stream,
            "temperature": 0.0}


def interactive_short(i: int, stream: bool = True) -> dict:
    r = _rng(f"int-short:{i}")
    return _chat("", f"Q{i}: {_lorem(r, 12)}? Answer briefly.", 32, stream)


def batch_long(i: int, stream: bool = False) -> dict:
    r = _rng(f"batch-long:{i}")
    doc = _lorem(r, 900)  # long document prompt
    return _chat("You summarize documents.", f"Summarize:\n{doc}", 512, stream)


# Many distinct long prefix GROUPS (llm-d benchmark shape). Prefix-cache-aware routing
# wins here because it can send each group consistently to one pod (high cache hit),
# whereas round-robin spreads each group across all pods (cache misses, recompute). A
# single shared prefix would NOT show the difference (round-robin caches it everywhere),
# so multiple groups are essential for a valid LT-2.
PREFIX_GROUPS = int(os.environ.get("PREFIX_GROUPS", "30"))
_PREFIXES = ["You are support agent for tenant %d. Policies:\n" % g + _lorem(_rng(f"grp:{g}"), 600)
             for g in range(PREFIX_GROUPS)]


def prefix_heavy(i: int, stream: bool = True) -> dict:
    g = i % PREFIX_GROUPS               # request i belongs to prefix group g
    r = _rng(f"prefix:{i}")
    return _chat(_PREFIXES[g], f"Ticket {i}: {_lorem(r, 15)}?", 48, stream)


def reasoning_long(i: int, stream: bool = True) -> dict:
    r = _rng(f"reason:{i}")
    return _chat("Think step by step and show all work.",
                 f"Solve in detail: {_lorem(r, 25)}. Enumerate every step.", 1024, stream)


def async_job(i: int) -> dict:
    """Async submit body: client_id + payload. The payload IS the batch-long request
    (no stream on the async path). Deterministic so duplicates hash identically."""
    return {"client_id": "loadtest", "payload": batch_long(i, stream=False)}


def job_id(client_id: str, payload: dict) -> str:
    """Byte-identical to worker.py.job_key / submit_api.job_key."""
    import json
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return f"{client_id}#{hashlib.sha256(body).hexdigest()[:32]}"


WORKLOADS = {
    "interactive-short": interactive_short,
    "batch-long": batch_long,
    "prefix-heavy": prefix_heavy,
    "reasoning-long": reasoning_long,
}
