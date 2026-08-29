"""The append-only JSONL ledger: one line per round, and the only loop state."""

from __future__ import annotations

import calendar
import datetime
import json
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set

from .states import BLOCKED, PR_READY

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
    "review_posted",
    "diff_stat",
    "pr_state",
    "notified_at",
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


def reviewed(records: Sequence[Dict[str, Any]], pr_url: str) -> bool:
    """Whether a review comment was already posted on this pull request.

    The ledger is the loop's only state (invariant 4), so this is where the
    question is asked - not by reading a marker back off the forge, which would
    be loop state living on GitHub.
    """
    return any(
        record.get("pr_url") == pr_url and record.get("review_posted") for record in records
    )


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


def epoch(ts: Any) -> float:
    """A ledger timestamp as epoch seconds; one that cannot be read never expires."""
    try:
        return calendar.timegm(time.strptime(str(ts), "%Y-%m-%dT%H:%M:%SZ"))
    except (TypeError, ValueError):
        return float("inf")


def open_pull_requests(records: Sequence[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """item -> its latest pr-bearing record, for those the ledger still shows open.

    A record's own ``pr_state``, when present, is the newest word on it; a
    PR_READY round with none yet is assumed open until a later record - this
    round's own merge, or a trigger poll's observation - says otherwise.
    """
    latest: Dict[str, Dict[str, Any]] = {}
    for record in records:
        item = record.get("item")
        if item and record.get("pr_url"):
            latest[item] = record
    return {
        item: record for item, record in latest.items()
        if (record.get("pr_state") or ("OPEN" if record.get("state") == "PR_READY" else None))
        == "OPEN"
    }


def reopened_since(records: Sequence[Dict[str, Any]], backlog_mtime: float) -> bool:
    """Whether any item's latest round is BLOCKED from before the backlog's mtime.

    A ``pr_state``-only line carries no ``duration_s`` and is not a round, so it
    is not "latest" for this question even when it is the newest line for the
    item.
    """
    latest: Dict[str, Dict[str, Any]] = {}
    for record in records:
        item = record.get("item")
        if item and record.get("duration_s") is not None:
            latest[item] = record
    return any(
        record.get("state") == BLOCKED and epoch(record.get("ts")) < backlog_mtime
        for record in latest.values()
    )


def note_pr_state(path: Path, *, item: str, sha: str, pr_url: str, pr_state: str) -> Dict[str, Any]:
    """Record what a trigger poll observed about a pull request, without a round.

    ``state`` stays PR_READY - the same value the round that opened it already
    wrote - so a reader that only knows the four terminal states sees nothing
    new; ``duration_s`` stays absent, which is what tells a round apart from
    this note.
    """
    return append(path, {
        "ts": now(), "item": item, "sha": sha, "state": PR_READY,
        "reason": "trigger poll observed pull request state %s" % pr_state,
        "pr_url": pr_url, "pr_state": pr_state,
    })


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
