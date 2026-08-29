"""Autonomy levels: what the loop may do with the pull request it just opened.

ROADMAP.md §3: at L1 the loop opens the pull request and a person merges it; at
L2 the loop merges it itself when the reviewer found nothing that sends work
back.  Invariant 8 is the floor under both: a protected path, or a diff the
verify step flagged, never auto-merges at any level.

Everything else is an `FYI`.  A held L2 pull request becomes a `DECIDE` - one
question, one link - and a `DECIDE` nobody answered within `EXPIRY_S` makes the
next round on that item BLOCKED, because a question that has been open for a
day is waiting on the operator and not on another round.
"""

from __future__ import annotations

import calendar
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional, Sequence

FYI = "FYI"
DECIDE = "DECIDE"
# §0's two classes that send work back; a suggestion never holds a merge.
HOLDING_KINDS = ("contract", "defect")
EXPIRY_S = 24 * 60 * 60


@dataclass(frozen=True)
class Decision:
    merge: bool
    decision: str
    reason: str


def decide(
    level: str,
    findings: Sequence[Mapping[str, Any]],
    protected: Sequence[str],
    verify_ok: bool,
) -> Decision:
    holds = []
    if protected:
        holds.append("the diff touches protected %s" % ", ".join(protected))
    if not verify_ok:
        holds.append("the verify step flagged this diff")
    holding = [finding for finding in findings if finding.get("kind") in HOLDING_KINDS]
    if holding:
        holds.append(
            "the reviewer returned %d %s finding(s)"
            % (len(holding), "/".join(sorted({str(f.get("kind")) for f in holding})))
        )
    if level != "L2":
        return Decision(False, FYI, "level %s, so a person merges" % level)
    if holds:
        return Decision(False, DECIDE, "%s - merge anyway?" % "; ".join(holds))
    return Decision(True, FYI, "no contract or defect finding and nothing held")


def _seconds(timestamp: Any) -> float:
    """A ledger timestamp as epoch seconds; one that cannot be read never expires."""
    try:
        return calendar.timegm(time.strptime(str(timestamp), "%Y-%m-%dT%H:%M:%SZ"))
    except (TypeError, ValueError):
        return float("inf")


def expired(
    records: Sequence[Mapping[str, Any]],
    item_id: str,
    now: float,
    is_open: Callable[[str], Optional[bool]],
) -> Optional[str]:
    """Why this item is BLOCKED on an unanswered DECIDE, or None."""
    for record in records:
        url = record.get("pr_url")
        if record.get("item") != item_id or record.get("decision") != DECIDE or not url:
            continue
        age = now - _seconds(record.get("ts"))
        if age < EXPIRY_S or not is_open(str(url)):
            continue
        return (
            "the DECIDE on %s is %.0f h old and still open; it is waiting on the "
            "operator, not on another round" % (url, age / 3600.0)
        )
    return None
