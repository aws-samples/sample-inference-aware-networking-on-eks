#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
THE NON-THINKING TEMPLATE IS WHAT ITS HEADER SAYS IT IS. Offline, no cluster, no model download.

the maintainer's condition on the ruling (2026-09-22): derive the file from the pinned revision's official
template with the thinking branch forced off, and record provenance (source revision, what
changed). A provenance note is a claim; this test is what makes it a fact. Four properties:

  1. SOURCE. upstream/qwen3-a55ee1b1.jinja is byte-for-byte the pinned upstream template (its
     sha256 is the one fetched from all three pinned revisions on 2026-09-22).
  2. WHAT CHANGED. The derived file, minus its Jinja comment header, equals upstream with exactly
     ONE substitution applied: the conditional empty think block made unconditional. Any other
     edit, however small, fails here.
  3. BEHAVIOUR. Rendered, the derived template equals upstream-with-enable_thinking=false for every
     fixture conversation, WHATEVER the request passes (nothing, true, false). That is what
     "forced off" means: the caller cannot switch thinking back on.
  4. THE CONTROL. Upstream with thinking unspecified must render DIFFERENTLY from upstream with
     it false. Without that, property 3 could pass on a fixture set too shallow to reach the tail.

Rendering uses the same Jinja settings transformers applies to chat templates (sandboxed,
trim_blocks, lstrip_blocks, loopcontrols, a raise_exception global, a tojson filter). Both
templates share every line but the tail, so any renderer difference affects both identically.
"""
from __future__ import annotations

import hashlib
import json
import os
import re

import jinja2
import jinja2.ext
from jinja2.sandbox import ImmutableSandboxedEnvironment

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
DIR = os.path.join(ROOT, "manifests/02-serving/chat-templates")
UPSTREAM = os.path.join(DIR, "upstream/qwen3-a55ee1b1.jinja")
DERIVED = os.path.join(DIR, "qwen3-nonthinking.jinja")
UPSTREAM_SHA256 = "a55ee1b1660128b7098723e0abcd92caa0788061051c62d51cbe87d9cf1974d8"

THE_ONE_CHANGE = (
    "    {%- if enable_thinking is defined and enable_thinking is false %}\n"
    "        {{- '<think>\\n\\n</think>\\n\\n' }}\n"
    "    {%- endif %}\n",
    "    {{- '<think>\\n\\n</think>\\n\\n' }}\n",
)

TOOLS = [{"type": "function", "function": {
    "name": "get_weather", "description": "Weather for a city",
    "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}}]

FIXTURES = {
    "single user turn": dict(messages=[{"role": "user", "content": "What is 2+2?"}]),
    "system then user": dict(messages=[{"role": "system", "content": "Be brief."},
                                       {"role": "user", "content": "Name a prime."}]),
    "multi-turn, prior answer carried reasoning": dict(messages=[
        {"role": "user", "content": "Hi"},
        {"role": "assistant", "content": "<think>\nthe user greets me\n</think>\n\nHello."},
        {"role": "user", "content": "And now?"}]),
    "tools and a tool result": dict(tools=TOOLS, messages=[
        {"role": "user", "content": "Weather in Oslo?"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"type": "function", "function": {"name": "get_weather", "arguments": {"city": "Oslo"}}}]},
        {"role": "tool", "content": "{\"temp_c\": 4}"},
        {"role": "user", "content": "Thanks, and Bergen?"}]),
}


def env() -> ImmutableSandboxedEnvironment:
    e = ImmutableSandboxedEnvironment(trim_blocks=True, lstrip_blocks=True,
                                      extensions=[jinja2.ext.loopcontrols])

    def raise_exception(msg):
        raise jinja2.exceptions.TemplateError(msg)

    e.globals["raise_exception"] = raise_exception
    e.filters["tojson"] = lambda x, indent=None, ensure_ascii=False: json.dumps(
        x, indent=indent, ensure_ascii=ensure_ascii)
    return e


def render(src: str, fixture: dict, **kw) -> str:
    return env().from_string(src).render(**fixture, **kw)


def strip_comment_header(src: str) -> str:
    m = re.match(r"\{#-.*?-#\}\n", src, re.DOTALL)
    assert m, "the derived template has lost its provenance header (a leading {#- ... -#} block)"
    return src[m.end():]


def main() -> int:
    up = open(UPSTREAM).read()
    dv = open(DERIVED).read()

    got = hashlib.sha256(up.encode()).hexdigest()
    assert got == UPSTREAM_SHA256, f"upstream copy is not the pinned template: sha256 {got}"
    print(f"OK  1 source: upstream copy is the pinned template, sha256 {UPSTREAM_SHA256[:16]}...")

    old, new = THE_ONE_CHANGE
    assert up.count(old) == 1, "upstream no longer has exactly one thinking branch; re-derive deliberately"
    assert strip_comment_header(dv) == up.replace(old, new, 1), (
        "the derived body is not upstream with exactly the one recorded substitution; some other "
        "edit crept in (diff it against upstream/qwen3-a55ee1b1.jinja)")
    print("OK  2 what changed: derived == upstream with exactly one substitution (the think block made unconditional)")
    # THE MODIFICATION NOTICE (Apache License 2.0, section 4(b)) is a licence condition, not a comment:
    # P551891987 approved the models on condition that the modified template says it was modified.
    notice = "MODIFIED FILE NOTICE (Apache License 2.0, section 4(b))"
    assert notice in dv.split("-#}", 1)[0], "the derived template has lost its modification notice"
    print("OK  2b the derived template carries its Apache License 2.0 modification notice")

    kwargs_variants = ({}, {"enable_thinking": True}, {"enable_thinking": False})
    for name, fx in FIXTURES.items():
        want = render(up, fx, add_generation_prompt=True, enable_thinking=False)
        for kw in kwargs_variants:
            have = render(dv, fx, add_generation_prompt=True, **kw)
            assert have == want, f"'{name}' with {kw or 'no kwarg'}: derived differs from upstream(thinking=false)"
        for kw in kwargs_variants:
            assert render(dv, fx, add_generation_prompt=False, **kw) == render(up, fx, add_generation_prompt=False, **kw), (
                f"'{name}' without a generation prompt: derived and upstream must agree, the tail is not rendered")
    print(f"OK  3 behaviour: {len(FIXTURES)} fixtures x 3 request variants render identically to upstream(enable_thinking=false)")

    controls = [n for n, fx in FIXTURES.items()
                if render(up, fx, add_generation_prompt=True)
                == render(up, fx, add_generation_prompt=True, enable_thinking=False)]
    assert not controls, f"control broken: upstream ignores the switch on {controls}, so property 3 proved nothing"
    assert all(render(dv, fx, add_generation_prompt=True, enable_thinking=True).endswith("<think>\n\n</think>\n\n")
               for fx in FIXTURES.values()), "a thinking request did not receive the empty think block"
    print("OK  4 control: upstream DOES change with the switch on every fixture, so the equivalence above discriminates")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
