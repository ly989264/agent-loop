"""The worker's bounded bundle.

Ported from valkey_scale_lab ``.github/milestone-loop/context_builder.py``: the
bundle is assembled whole, encoded once, and **refused** when it exceeds the byte
cap.  It is never truncated, because a silently shortened context is a context
whose agent was answering a different question than the one recorded.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence

from .backlog import Item

MAX_CONTEXT_BYTES = 192_000
EXCERPT_RADIUS_LINES = 20
# What `agent-loop plan` shows a planner, all of it consumer data.
PLAN_LEDGER_LINES = 20
PLAN_SOURCE_BYTES = 8_000
PLAN_SOURCE_FILES = 40


class ContextTooLarge(RuntimeError):
    """The bundle is over its cap; the round refuses rather than truncating."""


def _site_excerpt(root: Path, site: str) -> Dict[str, Any]:
    """One cited ``path:line`` as a bounded window of the file around that line."""
    path_text, _, line_text = site.rpartition(":")
    if not path_text or not line_text.isdigit():
        return {"site": site, "absent_reason": "site is not in path:line form"}
    line_number = int(line_text)
    try:
        lines = (root / path_text).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        return {"site": site, "absent_reason": "cannot read cited file: %s" % exc}
    first = max(1, line_number - EXCERPT_RADIUS_LINES)
    last = min(len(lines), line_number + EXCERPT_RADIUS_LINES)
    if line_number > len(lines):
        return {"site": site, "absent_reason": "file has only %d lines" % len(lines)}
    return {
        "site": site,
        "path": path_text,
        "line": line_number,
        "first_line": first,
        "last_line": last,
        "text": "\n".join(lines[first - 1 : last]),
    }


def build_worker_bundle(
    *,
    item: Item,
    probe_output: str,
    probe_exit_code: int,
    root: Path,
    schema: Mapping[str, Any],
    sha: str,
) -> Dict[str, Any]:
    return {
        "schema_version": "agent-loop-bundle-v1",
        "role": "worker",
        "sha": sha,
        "item": {
            "id": item.id,
            "group": item.group,
            "statement": item.statement,
            "cost_class": item.cost_class,
            "proof": item.proof,
            "notes": item.notes,
        },
        "probe": {
            "command": item.probe,
            "exit_code": probe_exit_code,
            "output": probe_output,
        },
        "sites": [_site_excerpt(root, site) for site in item.sites],
        "design_doc_section": item.design_doc,
        "output_schema": dict(schema),
    }


# ROADMAP.md §0: the classes a finding must be one of, and what each costs to
# make.  They travel with the bundle because the reviewer is told the rules
# rather than trusted to remember them.
FINDING_CLASSES = {
    "contract": "violates a ROADMAP.md line or an invariant; must cite it",
    "defect": "the deliverable does not do what it says; must show the failing case",
    "suggestion": "anything else; recorded, never acted on in this stage",
    "_rules": (
        "A finding without a citation or a failing case is a suggestion. "
        "A finding that would grow the diff is a suggestion by definition. "
        "Ask first whether any hunk is required by nothing in the item: each "
        "such hunk is a contract finding and comes back as remove."
    ),
}


# ROADMAP.md invariant 2 and §7's last risk line, told to the planner rather
# than assumed of it; the kernel enforces every one of them again at admission.
PLANNING_RULES = (
    "Propose only items you can name a probe for: a shell command, run from the "
    "cost class's verify cwd, that exits non-zero while the item is open and 0 "
    "once it is closed. Run it and see it fail before you propose it - the "
    "kernel runs it again and rejects a proposal whose probe exits 0. Do not "
    "propose an item whose id or statement is already in the backlog below. "
    "Do not propose work that needs a paid or fleet run. A proposal's sites are "
    "path:line where the item lives today."
)


def plan_excerpts(root: Path, patterns: Sequence[str]) -> List[Dict[str, Any]]:
    """The consumer-named files a planner may read, each bounded and said to be.

    A glob can name a megabyte of documentation, so each file contributes at
    most ``PLAN_SOURCE_BYTES`` and carries ``truncated`` when it was cut. That is
    not the silent shortening ``encode`` refuses: the bundle says what it did.
    """
    excerpts: List[Dict[str, Any]] = []
    for pattern in patterns:
        try:
            matches = sorted(root.glob(pattern))
        except (NotImplementedError, ValueError) as exc:
            excerpts.append({"pattern": pattern, "absent_reason": "unusable glob: %s" % exc})
            continue
        for path in matches:
            if len(excerpts) >= PLAN_SOURCE_FILES:
                return excerpts
            if not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
                name = str(path.resolve().relative_to(root.resolve()))
            except (OSError, ValueError) as exc:
                excerpts.append({"pattern": pattern, "absent_reason": "cannot read: %s" % exc})
                continue
            excerpts.append({
                "path": name,
                "text": text[:PLAN_SOURCE_BYTES],
                "truncated": len(text) > PLAN_SOURCE_BYTES,
            })
    return excerpts


def build_planner_bundle(
    *,
    items: Sequence[Item],
    records: Sequence[Mapping[str, Any]],
    excerpts: Sequence[Mapping[str, Any]],
    schema: Mapping[str, Any],
) -> Dict[str, Any]:
    """What the planner sees: consumer data only - no tree, no diff.

    The backlog is ids, statements and selectable, because that is what a
    duplicate is judged against; the ledger tail is what the loop has been
    doing; the excerpts are whatever `plan_sources` named.
    """
    return {
        "schema_version": "agent-loop-bundle-v1",
        "role": "planner",
        "backlog": [
            {"id": item.id, "statement": item.statement, "cost_class": item.cost_class,
             "selectable": item.selectable}
            for item in items
        ],
        "ledger_tail": [
            {key: record.get(key) for key in ("ts", "item", "state", "reason")}
            for record in list(records)[-PLAN_LEDGER_LINES:]
        ],
        "sources": [dict(excerpt) for excerpt in excerpts],
        "planning_rules": PLANNING_RULES,
        "output_schema": dict(schema),
    }


def build_reviewer_bundle(
    *,
    item: Item,
    diff: str,
    sha: str,
    schema: Mapping[str, Any],
) -> Dict[str, Any]:
    """What the reviewer sees: the item, the diff, and the finding classes.

    Not the worker's reasoning - §0's review rule - and not the tree, because the
    reviewer runs read-only against the diff it is asked about.
    """
    return {
        "schema_version": "agent-loop-bundle-v1",
        "role": "reviewer",
        "sha": sha,
        "item": {
            "id": item.id,
            "group": item.group,
            "statement": item.statement,
            "cost_class": item.cost_class,
            "proof": item.proof,
            "notes": item.notes,
        },
        "diff": diff,
        "finding_classes": dict(FINDING_CLASSES),
        "output_schema": dict(schema),
    }


def encode(bundle: Mapping[str, Any]) -> str:
    """Encode the bundle, refusing anything over the cap."""
    encoded = json.dumps(bundle, indent=2, sort_keys=True)
    size = len(encoded.encode("utf-8"))
    if size > MAX_CONTEXT_BYTES:
        raise ContextTooLarge(
            "bundle is %d bytes, above the %d byte cap; refused rather than truncated"
            % (size, MAX_CONTEXT_BYTES)
        )
    return encoded
