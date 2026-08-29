"""The worker's bounded bundle.

Ported from valkey_scale_lab ``.github/milestone-loop/context_builder.py``: the
bundle is assembled whole, encoded once, and **refused** when it exceeds the byte
cap.  It is never truncated, because a silently shortened context is a context
whose agent was answering a different question than the one recorded.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Mapping

from .backlog import Item

MAX_CONTEXT_BYTES = 192_000
EXCERPT_RADIUS_LINES = 20


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
