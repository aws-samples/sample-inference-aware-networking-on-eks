# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Structural check for the merged Karpenter CloudFormation template.

WHY THIS EXISTS, and it is not the reason originally proposed. The review recommended
`aws cloudformation validate-template` as the primary guard on the merged artifact, on the
reasoning that CloudFormation's own parser handles every intrinsic natively and would have caught
the fused-newline defect before the rollback. That was tested by injection rather than adopted, and
it is FALSE for both failure modes this merge can produce:

    injection                      validate-template     this checker
    fused newline (Arn# Overlay)   ACCEPTED              REJECTED
    duplicate logical ID           ACCEPTED              REJECTED

`validate-template` checks template syntax and returns parameters and capabilities. It does NOT
resolve `!GetAtt` attribute names against resource schemas, which happens later at changeset time,
so it accepted the exact artifact that rolled the real stack back on 2026-08-03. And a duplicate
logical ID is collapsed by YAML last-key-wins before CloudFormation ever sees the template, so
there is nothing left for it to reject. This checker is therefore the ONLY layer that catches
either, and it runs offline with no credentials.

PARSE FOR VALIDATION ONLY, NEVER RE-EMIT. CloudFormation short-form intrinsics (!Sub, !Ref,
!GetAtt) are not standard YAML and `yaml.safe_load` rejects them. A loader that silently
stringifies them would produce a template that validates and deploys the WRONG resources, which is
this defect class wearing its best disguise. So unknown tags are mapped to an opaque marker that
carries no meaning and is never written back: the original concatenated text is what ships.

THE SUPERSET ASSERTION, and it closes the dangerous case the first version left open. Rejecting
fused attributes and duplicate logical IDs is necessary and NOT sufficient: the overlay lands as
indentation-sensitive text inside an existing mapping, so if the append ever opens a SECOND
`Resources:` key, or lands at the wrong indentation and nests under an existing resource, then
last-key-wins silently REPLACES or DROPS AWS's own resources. No duplicate ID, no fused attribute,
valid YAML, and a clean deploy of the wrong template. Nothing else here would object.

So when the pristine template and the overlay are both supplied, this asserts:

    merged_ids == pristine_ids | overlay_ids        catches silent drops and replacements
    len(merged_ids) == len(pristine) + len(overlay) catches an overlay ID colliding with upstream,
                                                    which set union alone would mask

Run:  ./.venv/bin/python tests/check_cfn_merge.py <merged.yaml> [<pristine.yaml> <overlay.yaml>]
"""
import sys

import yaml


class _Opaque:
    """A CloudFormation intrinsic, preserved as an inert marker. Never re-emitted."""

    def __init__(self, tag, value):
        self.tag, self.value = tag, value

    def __repr__(self):
        return f"<{self.tag}>"


class CfnLoader(yaml.SafeLoader):
    """SafeLoader that tolerates CloudFormation tags and REFUSES duplicate mapping keys.

    The duplicate-key refusal is the load-bearing part. Standard YAML semantics are last-key-wins,
    so two resources sharing a logical ID silently become one and the second overwrites the first.
    That is exactly the collision a text-append merge can introduce, and it is invisible to every
    downstream tool including CloudFormation itself.
    """

    def construct_mapping(self, node, deep=False):
        seen = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)
            if key in seen:
                raise yaml.constructor.ConstructorError(
                    None, None,
                    f"duplicate key {key!r} at line {key_node.start_mark.line + 1}: "
                    "two resources or properties share a name and one would silently "
                    "overwrite the other",
                    key_node.start_mark,
                )
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


def _opaque(loader, tag_suffix, node):
    if isinstance(node, yaml.ScalarNode):
        return _Opaque(tag_suffix, node.value)
    if isinstance(node, yaml.SequenceNode):
        return _Opaque(tag_suffix, loader.construct_sequence(node, deep=True))
    return _Opaque(tag_suffix, loader.construct_mapping(node, deep=True))


CfnLoader.add_multi_constructor("!", _opaque)


def _ids(path: str, key: str | None = "Resources") -> set:
    """Logical IDs in a template's Resources section, or in a bare overlay fragment."""
    doc = yaml.load(open(path).read(), Loader=CfnLoader)  # nosec B506 - CfnLoader subclasses yaml.SafeLoader
    if key and isinstance(doc, dict) and key in doc:
        doc = doc[key]
    return set(doc) if isinstance(doc, dict) else set()


def check(path: str, pristine: str | None = None, overlay: str | None = None) -> int:
    raw = open(path).read()
    problems = []

    # 1. Fused concatenation. AWS's template ends WITHOUT a trailing newline, so appending without
    #    a separator glues the first overlay byte onto the last template byte. In YAML a '#' that
    #    is NOT preceded by whitespace does not start a comment, so it becomes part of the scalar
    #    and produces a GetAtt attribute that does not exist. This is a text-level check because by
    #    the time it is a parsed scalar it looks entirely legitimate.
    for i, line in enumerate(raw.splitlines(), 1):
        stripped = line.split("#", 1)[0]
        if "#" in line and stripped and not stripped.endswith((" ", "\t")) and "!" in stripped:
            problems.append(
                f"line {i}: '#' fused directly onto a value with no preceding space, so it is part "
                f"of the scalar rather than a comment: {line.strip()[:90]}")

    # 2. Structure, with duplicate logical IDs refused by the loader.
    try:
        doc = yaml.load(raw, Loader=CfnLoader)  # nosec B506 - CfnLoader subclasses yaml.SafeLoader
    except yaml.constructor.ConstructorError as e:
        problems.append(f"duplicate key: {e.problem}")
        doc = None
    except yaml.YAMLError as e:
        problems.append(f"not parseable as YAML: {str(e)[:160]}")
        doc = None

    if doc is not None:
        if not isinstance(doc, dict) or "Resources" not in doc:
            problems.append("no top-level Resources section")
        else:
            res = doc["Resources"]
            if not isinstance(res, dict) or not res:
                problems.append("Resources is empty or not a mapping")
            else:
                # The overlay's contract: these must exist after a successful merge.
                for required in ("KarpenterControllerRole", "KarpenterNodeInstanceProfile",
                                 "KarpenterNodeRole"):
                    if required not in res:
                        problems.append(f"missing expected resource {required}")

    # 3. SUPERSET ASSERTION. Only possible when the inputs are supplied, and it is the check that
    #    catches a merge which silently replaced or dropped upstream resources.
    if doc is not None and pristine and overlay:
        try:
            p_ids, o_ids = _ids(pristine), _ids(overlay, key=None)
            m_ids = set(doc.get("Resources", {}))
            expected = p_ids | o_ids
            if m_ids != expected:
                dropped, added = expected - m_ids, m_ids - expected
                if dropped:
                    problems.append(
                        f"merge DROPPED or REPLACED {len(dropped)} resource(s) present in the "
                        f"inputs: {sorted(dropped)[:6]}")
                if added:
                    problems.append(f"merge INVENTED {len(added)} resource(s): {sorted(added)[:6]}")
            if len(m_ids) != len(p_ids) + len(o_ids):
                problems.append(
                    f"cardinality mismatch: merged has {len(m_ids)} resources, inputs have "
                    f"{len(p_ids)} + {len(o_ids)} = {len(p_ids) + len(o_ids)}. An overlay ID "
                    "colliding with an upstream ID looks fine to set-union and shows up here.")
        except Exception as e:  # noqa: BLE001
            problems.append(f"could not compare against the inputs: {str(e)[:120]}")

    if problems:
        print(f"FAIL  {path}")
        for p in problems:
            print(f"  {p}")
        return 1
    extra = " and is the exact union of the inputs" if (pristine and overlay) else ""
    print(f"OK    {path}: parses, logical IDs unique, overlay resources present, "
          f"no fused scalar{extra}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) not in (2, 4):
        print("usage: check_cfn_merge.py <merged.yaml> [<pristine.yaml> <overlay.yaml>]")
        raise SystemExit(2)
    raise SystemExit(check(*sys.argv[1:]))
