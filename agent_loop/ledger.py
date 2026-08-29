"""The append-only JSONL ledger: one line per round, and the only loop state."""

from __future__ import annotations

import datetime
import json
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set

from .states import BLOCKED

FIELDS = (
    "ts",
    "item",
    "sha",
    "state",
    "reason",
    "cost",
    "duration_s",
    "tool_versions",
    "warning",
    "pr_url",
    "decision",
)


def now() -> str:
    return datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def read(path: Path) -> List[Dict[str, Any]]:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return []
    records = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def append(path: Path, record: Dict[str, Any]) -> Dict[str, Any]:
    line = {key: record.get(key) for key in FIELDS}
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(line, sort_keys=True) + "\n")
    return line


def blocked_at(records: Sequence[Dict[str, Any]], sha: str) -> Set[str]:
    """Item ids recorded BLOCKED at this sha; they are skipped until it moves."""
    return {
        str(record.get("item"))
        for record in records
        if record.get("state") == BLOCKED and record.get("sha") == sha and record.get("item")
    }


def already_notified(
    records: Sequence[Dict[str, Any]], item: Optional[str], state: str, sha: str
) -> bool:
    """A round emits its notification unless (item, state, sha) is already a line.

    Every recorded round notified once, so the presence of the line is the record
    of the notification and no separate flag is kept.
    """
    key = (item or "", state, sha)
    for record in records:
        if (str(record.get("item") or ""), record.get("state"), record.get("sha")) == key:
            return True
    return False


def drift(records: Sequence[Dict[str, Any]], versions: Mapping[str, str]) -> Optional[str]:
    """How this round's tools differ from the last recorded round's, or None.

    A worker whose behaviour changes between two rounds is usually explained by
    the tool under it changing, and that is invisible once the rounds are a week
    apart.  It is a note on the round's own line and nothing more: no state, no
    notification, no effect on the terminal state - an upgrade is not a failure.
    """
    previous = None
    for record in reversed(list(records)):
        recorded = record.get("tool_versions")
        if isinstance(recorded, dict) and recorded:
            previous = recorded
            break
    if previous is None or previous == dict(versions):
        return None
    changes = [
        "%s %s -> %s" % (name, previous.get(name, "absent"), versions.get(name, "absent"))
        for name in sorted(set(previous) | set(versions))
        if previous.get(name) != versions.get(name)
    ]
    return "tool versions changed since the previous round: " + "; ".join(changes)


def _version(argv: Sequence[str]) -> str:
    try:
        completed = subprocess.run(
            list(argv),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "absent"
    if completed.returncode != 0:
        return "absent"
    lines = completed.stdout.strip().splitlines()
    return lines[0][:200] if lines else "absent"


def tool_versions(adapters: Sequence[str] = ()) -> Dict[str, str]:
    versions = {
        "git": _version(["git", "--version"]),
        "python3": _version(["python3", "--version"]),
    }
    for adapter in adapters:
        if adapter == "claude-code":
            versions["claude"] = _version(["claude", "--version"])
        elif adapter == "codex":
            versions["codex"] = _version(["codex", "--version"])
    return versions
