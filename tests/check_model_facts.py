#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
INV18's checker. The model-facts registry is COMPLETE and NOT BYPASSED.

Written as Python rather than as shell for one specific reason, and it is the reason INV14's
checker moved here too: half one has to enumerate the models a serving manifest actually names, and
the manifests name them in TWO DIFFERENT SYNTAXES. The NVIDIA manifests use `--model=<id>`; the
Neuron manifest passes the id POSITIONALLY after `vllm serve`, because the DLC entrypoint execs
argv[1:] as a full command. A grep for `--model=` finds two of the three and reports completeness,
which is this project's recurring failure family: an enumeration that quietly omits a member and a
conclusion drawn from what is left.

Prints one FAIL line per problem, or nothing and exits 0.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
REG = ROOT / "docs" / "specs" / "model-facts.yaml"
SERVING = ROOT / "manifests" / "02-serving"

# A Hugging Face model id: org/name, where name carries the punctuation these ids actually use.
HF_ID = re.compile(r"\b([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)\b")
# THE OPENAI-COMPATIBLE INFERENCE SURFACE, and the narrowness is deliberate rather than lazy.
#
# A first cut matched every `/v1/...` literal in the tree and produced sixteen failures, none of
# which were about addressing a model: Prometheus's `/v1/query` and `/v1/query_range`, and the
# design-doc generators quoting vLLM's internal `/v1/engine/async_llm`. A check whose output is
# dominated by things it should not be looking at is a check nobody reads, and the correct response
# is to say what it is checking rather than to add exemptions for what it is not.
#
# So half 2 asks one question: does any committed file address a model through a path on the
# OPENAI-COMPATIBLE INFERENCE SURFACE that the registry does not know? Prometheus is not on that
# surface. `/v1/engine/async_llm` is a vLLM internal and is not either.
ENDPOINT_LIT = re.compile(
    r"(/v1/(?:chat/completions|completions|embeddings|models|responses|moderations|rerank|score))"
)

fails: list[str] = []


def fail(msg: str) -> None:
    fails.append(msg)
    print(f"FAIL {msg}")


def models_named_in_serving() -> dict[str, str]:
    """Every model id a serving manifest names, whatever syntax it uses, with the file it came from."""
    found: dict[str, str] = {}
    for f in sorted(SERVING.glob("*.yaml")):
        doc = f.read_text()
        for line in doc.splitlines():
            stripped = line.strip().lstrip("-").strip()
            # Strip a trailing YAML comment before matching, or a comment mentioning an id
            # (the manifests do discuss meta-llama/... in prose) would be read as a served model.
            stripped = stripped.split("#", 1)[0].strip()
            if not stripped:
                continue
            if stripped.startswith("--model="):
                stripped = stripped.split("=", 1)[1]
            elif "/" not in stripped or " " in stripped or ":" in stripped:
                continue
            m = HF_ID.fullmatch(stripped.strip("\"'"))
            if m:
                found.setdefault(m.group(1), f.relative_to(ROOT).as_posix())
    return found


def main() -> int:
    if not REG.exists():
        fail(f"the registry is missing at {REG.relative_to(ROOT)}")
        return 1
    reg = yaml.safe_load(REG.read_text())
    entries = reg.get("models") or {}
    known_eps = set(reg.get("known_endpoints") or [])
    by_hf = {m["hf_id"]: (k, m) for k, m in entries.items()}

    # --- Half 1: completeness ------------------------------------------------------------------
    served = models_named_in_serving()
    if not served:
        fail("no model was found in any serving manifest, which means this check is reading the "
             "wrong files rather than that nothing is served")
    for hf_id, where in sorted(served.items()):
        if hf_id not in by_hf:
            fail(f"{where} serves '{hf_id}' with no model-facts entry. A model nobody wrote facts "
                 f"for is a model nobody decided how to address.")

    # --- Registry self-consistency, before it is trusted by the halves below --------------------
    for key, m in entries.items():
        for req in ("hf_id", "valid_endpoints", "content_generation_endpoint", "chat_template",
                    "bos_token", "eos_token", "template_emits_bos", "revision"):
            if req not in m:
                fail(f"registry entry '{key}' has no '{req}'")
        ce = m.get("content_generation_endpoint")
        if ce and ce not in (m.get("valid_endpoints") or []):
            fail(f"registry entry '{key}' names a content endpoint '{ce}' it does not list as valid")
        if ce and known_eps and ce not in known_eps:
            fail(f"registry entry '{key}' content endpoint '{ce}' is not in known_endpoints")
        if m.get("instruction_tuned") and ce and not ce.endswith("/chat/completions"):
            fail(f"registry entry '{key}' is instruction tuned but its content endpoint '{ce}' "
                 f"applies no chat template")

    # --- Half 2: no call site invents an endpoint path ------------------------------------------
    # git ls-files, so the check reads what is COMMITTED rather than whatever is lying in the tree.
    try:
        # Fixed git argv: list form, no shell.
        tracked = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True,  # nosec B603
                                 check=True).stdout.split()
    except Exception as e:                                     # noqa: BLE001
        fail(f"could not enumerate tracked files ({e}), so half 2 cannot run and is not passing")
        return 1
    exts = (".py", ".sh", ".yaml", ".yml", ".json")
    skip_dirs = ("loadtest/results/", "evidence/", "docs/agents/inbox/")
    for rel in tracked:
        if not rel.endswith(exts) or rel.startswith(skip_dirs):
            continue
        p = ROOT / rel
        if not p.exists():
            continue
        try:
            text = p.read_text(errors="ignore")
        except OSError:
            continue
        for lit in set(ENDPOINT_LIT.findall(text)):
            lit = lit.rstrip("/")
            if known_eps and lit not in known_eps:
                fail(f"{rel} addresses '{lit}', which the registry does not list in "
                     f"known_endpoints. Either it is a typo or the registry is out of date; "
                     f"either way one of the two is wrong.")

    if fails:
        return 1
    print("OK model-facts registry: complete against the serving manifests, and no committed file "
          "invents an endpoint path")
    return 0


if __name__ == "__main__":
    sys.exit(main())
