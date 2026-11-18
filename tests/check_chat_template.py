#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
INV20: ONE NON-THINKING TEMPLATE, MOUNTED ON EVERY QWEN3 TIER, AGREEING EVERYWHERE IT IS NAMED.

the maintainer's ruling of 2026-09-22 makes the hard template switch spec-002's control: one committed
file, one model family, all three tiers, so the thinking mode cannot drift per tier (and drift is
measurable contamination, since the template overhead differs by mode). A control that lives in
four places is a control that can disagree with itself in four ways, so this checks the chain:

  1. THE FILE IS FORCED OFF. With its Jinja comments removed, the committed template never reads
     enable_thinking. A template that consults the switch is a template a caller can flip.
  2. THE CONFIGMAP CARRIES EXACTLY THOSE BYTES. The ConfigMap manifest is what reaches the pod; the
     .jinja file is what is reviewed. Equal, byte for byte, or the reviewed file is decoration.
  3. EVERY QWEN3 SERVING MANIFEST MOUNTS IT AND POINTS VLLM AT IT. Derived, not listed: every
     manifests/02-serving/vllm-*.yaml whose model is Qwen/Qwen3-* must pass --chat-template naming
     the mounted file, in EITHER argument syntax (--flag=value, or --flag then value; the Neuron
     manifest uses the second, and a checker that only knows one is the truncated-enumeration
     family this repository keeps finding), and must mount the ConfigMap at that directory.

Exit 0 and one OK line, or exit 1 with FAIL lines. Offline.

`--write-configmap` regenerates the ConfigMap manifest from the .jinja file (the only supported way
to change it), then verifies the round trip.
"""
from __future__ import annotations

import hashlib
import os
import re
import sys

import yaml

ROOT = os.path.join(os.path.dirname(__file__), "..")
SERVING = os.path.join(ROOT, "manifests/02-serving")
TEMPLATE_REL = "manifests/02-serving/chat-templates/qwen3-nonthinking.jinja"
TEMPLATE = os.path.join(ROOT, TEMPLATE_REL)
CM_FILE = os.path.join(SERVING, "chat-template-qwen3-nonthinking.yaml")
CM_NAME = "chat-template-qwen3-nonthinking"
# A ConfigMap data key (the template file name), not a secret; gitleaks generic-api-key matches the name.
CM_KEY = "qwen3-nonthinking.jinja"  # gitleaks:allow
MOUNT_DIR = "/etc/vllm/chat-templates"
MOUNTED = f"{MOUNT_DIR}/{CM_KEY}"

CM_HEADER = """# ConfigMap: the qwen3-nonthinking chat template, mounted by every Qwen3 serving Deployment.
# NOTICE: the data below is a modified copy of the Qwen3 chat template (Apache License 2.0).
# Amazon.com, Inc. or its affiliates changed it. Its header states the change. See THIRD-PARTY-LICENSES.
# GENERATED from manifests/02-serving/chat-templates/qwen3-nonthinking.jinja by
#   ./.venv/bin/python tests/check_chat_template.py --write-configmap
# DO NOT EDIT BY HAND: INV20 fails unless these bytes equal the reviewed .jinja file exactly.
# spec-002 ruling 4.2; the maintainer's ruling of 2026-09-22. INVARIANT 8: namespace inference.
"""


def render_configmap(content: str) -> str:
    style = "|-" if not content.endswith("\n") else "|"
    body = "\n".join(("    " + ln) if ln else "" for ln in content.rstrip("\n").split("\n"))
    return (CM_HEADER + "apiVersion: v1\nkind: ConfigMap\nmetadata:\n"
            f"  name: {CM_NAME}\n  namespace: inference\ndata:\n  {CM_KEY}: {style}\n{body}\n")


def strip_jinja_comments(src: str) -> str:
    return re.sub(r"\{#.*?#\}", "", src, flags=re.DOTALL)


def model_of(args: list) -> str:
    for i, a in enumerate(args):
        if a.startswith("--model="):
            return a.split("=", 1)[1]
        if a == "serve" and i + 1 < len(args):
            return args[i + 1]
    return ""


def flag_value(args: list, flag: str) -> str | None:
    for i, a in enumerate(args):
        if a.startswith(flag + "="):
            return a.split("=", 1)[1]
        if a == flag and i + 1 < len(args):
            return args[i + 1]
    return None


def check() -> list:
    fails = []
    content = open(TEMPLATE).read()

    if "enable_thinking" in strip_jinja_comments(content):
        fails.append(f"{TEMPLATE_REL} consults enable_thinking, so a caller can switch thinking back on")

    try:
        cm = yaml.safe_load(open(CM_FILE))
        if cm["data"][CM_KEY] != content:
            fails.append(f"ConfigMap {CM_NAME} does not carry the reviewed template bytes; regenerate with --write-configmap")
    except (OSError, KeyError, TypeError) as e:
        fails.append(f"ConfigMap manifest {os.path.relpath(CM_FILE, ROOT)} unreadable or missing key {CM_KEY}: {e}")

    qwen3 = 0
    for fn in sorted(os.listdir(SERVING)):
        if not (fn.startswith("vllm-") and fn.endswith(".yaml")):
            continue
        for doc in yaml.safe_load_all(open(os.path.join(SERVING, fn))):
            if not doc or doc.get("kind") != "Deployment":
                continue
            pod = doc["spec"]["template"]["spec"]
            c = pod["containers"][0]
            args = [str(a) for a in (c.get("args") or [])]
            if not model_of(args).startswith("Qwen/Qwen3-"):
                continue
            qwen3 += 1
            name = doc["metadata"]["name"]
            val = flag_value(args, "--chat-template")
            if val != MOUNTED:
                fails.append(f"{fn} ({name}) --chat-template is {val!r}, not {MOUNTED!r}; this tier would run upstream thinking-ON")
            vols = {v["name"]: v for v in (pod.get("volumes") or [])}
            mnts = [m for m in (c.get("volumeMounts") or []) if m.get("mountPath") == MOUNT_DIR]
            src = vols.get(mnts[0]["name"], {}).get("configMap", {}).get("name") if mnts else None
            if src != CM_NAME:
                fails.append(f"{fn} ({name}) does not mount ConfigMap {CM_NAME} at {MOUNT_DIR} (found {src!r})")
    if qwen3 == 0:
        fails.append("no Qwen3 serving Deployment found, so the mount half checked nothing")

    sha = hashlib.sha256(content.encode()).hexdigest()
    return fails, qwen3, sha


def main() -> int:
    if "--write-configmap" in sys.argv[1:]:
        content = open(TEMPLATE).read()
        out = render_configmap(content)
        assert yaml.safe_load(out)["data"][CM_KEY] == content, "round trip lost bytes; refusing to write"
        open(CM_FILE, "w").write(out)
        print(f"wrote {os.path.relpath(CM_FILE, ROOT)} ({len(content)} template bytes, round trip verified)")
    fails, n, sha = check()
    for f in fails:
        print(f"FAIL {f}")
    if fails:
        return 1
    print(f"OK chat template forced off, ConfigMap byte-equal, mounted on all {n} Qwen3 serving Deployments, "
          f"template sha256 {sha[:12]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
