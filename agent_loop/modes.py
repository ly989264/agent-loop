"""Modes other than ``once``: ``continuous``, ``schedule``, ``until``.

``once`` (``round.run_once``) is a single round and is unchanged. Every other
mode drives it in-process, round after round - no subprocess, no daemon.
``caps.round_wall_s`` is enforced inside ``run_once`` itself (a
``signal.alarm``, round.py), so a round that runs long ends the same way any
other INFRA does: existing worktree cleanup, one ledger line, one
deduplicated notification. This module never spawns a process for a round and
never notifies a round's own outcome - only ``notify.py`` does that, and only
once per (item, state, sha).

``schedule`` needs nothing ``once`` does not already do - one round, exit
code says which of the four states - so it is that mode under cron's name,
not a second implementation.

Back-pressure and triggers are both polls; the loop sleeps ``caps.poll_s``
between checks and nothing here is a background process.
"""

from __future__ import annotations

import statistics
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

from . import config as config_module, ledger, notify, round as round_module, scm
from .config import Config
from .states import BLOCKED, INFRA, NO_ITEM, PR_READY

PAUSE_NAME = ".paused"
NON_PROGRESS_STATES = (NO_ITEM, INFRA)


@dataclass(frozen=True)
class Stop:
    """``until``'s stop conditions; any combination, first met wins."""

    prs: Optional[int] = None
    hours: Optional[float] = None
    cost: Optional[float] = None


def paused(worktree_root: Path) -> bool:
    return (worktree_root / PAUSE_NAME).exists()


def pause(worktree_root: Path) -> None:
    worktree_root.mkdir(parents=True, exist_ok=True)
    (worktree_root / PAUSE_NAME).write_text(ledger.now(), encoding="utf-8")


def resume(worktree_root: Path) -> None:
    try:
        (worktree_root / PAUSE_NAME).unlink()
    except OSError:
        pass


def _backlog_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _stop_met(stop: Stop, started: float, prs_opened: int, cost_spent: float) -> bool:
    return (
        (stop.prs is not None and prs_opened >= stop.prs)
        or (stop.hours is not None and (time.time() - started) / 3600.0 >= stop.hours)
        or (stop.cost is not None and cost_spent >= stop.cost)
    )


def _after_round(config: Config, state: str, non_progress: int) -> int:
    """Update the non-progress counter; back off once when it caps out.

    Sleeping and the one FYI happen here, so a test can drive this directly
    without running the whole loop to reach a particular round count.
    """
    non_progress = non_progress + 1 if state in NON_PROGRESS_STATES else 0
    if non_progress < config.non_progress_rounds:
        return non_progress
    time.sleep(config.idle_s)
    notify.notify(
        config, item=None, state="IDLE", sha="",
        reason="%d consecutive non-progress rounds; slept %ds, retrying"
               % (non_progress, config.idle_s),
    )
    return 0


def _wait_for_trigger(config: Config, publisher: scm.Publisher, last_backlog_mtime: float) -> str:
    """Block (polling every ``caps.poll_s``) until one of §3's triggers fires."""
    deadline = time.time() + config.idle_s
    while True:
        if paused(config.worktree_root):
            return "paused"
        mtime = _backlog_mtime(config.backlog)
        if mtime != last_backlog_mtime:
            if ledger.reopened_since(ledger.read(config.ledger), mtime):
                return "a blocked item was reopened by editing the backlog"
            return "the backlog was edited"
        if config.scm == "github":
            for item, record in ledger.open_pull_requests(ledger.read(config.ledger)).items():
                state = publisher.state(config.root, record["pr_url"])
                if state and state != "OPEN":
                    ledger.note_pr_state(config.ledger, item=item, sha=record.get("sha") or "",
                                         pr_url=record["pr_url"], pr_state=state)
                    return "pull request %s" % state.lower()
        if time.time() >= deadline:
            return "idle timer"
        time.sleep(min(config.poll_s, max(deadline - time.time(), 0.01)))


def run_continuous(config_path: Path, stop: Optional[Stop] = None) -> int:
    """``continuous`` with no ``stop``; ``until`` with one. Runs until then."""
    config = config_module.load(config_path)
    publisher = scm.build(config.scm)
    started = time.time()
    prs_opened = 0
    cost_spent = 0.0
    non_progress = 0
    last_backlog_mtime = _backlog_mtime(config.backlog)
    while True:
        while paused(config.worktree_root):
            time.sleep(config.poll_s)
        while len(ledger.open_pull_requests(ledger.read(config.ledger))) >= config.open_prs:
            if paused(config.worktree_root):
                break
            time.sleep(config.poll_s)
        if paused(config.worktree_root):
            continue

        outcome = round_module.run_once(config_path)
        prs_opened += 1 if outcome.state == PR_READY and outcome.pr_url else 0
        cost_spent += outcome.cost or 0.0
        if stop is not None and _stop_met(stop, started, prs_opened, cost_spent):
            return 0

        non_progress = _after_round(config, outcome.state, non_progress)

        reason = _wait_for_trigger(config, publisher, last_backlog_mtime)
        print("agent-loop: continuous - %s" % reason)
        last_backlog_mtime = _backlog_mtime(config.backlog)


def _touches_plumbing(diff_stat: str) -> bool:
    for line in diff_stat.splitlines():
        if "|" not in line:
            continue
        path = line.split("|", 1)[0].strip()
        if path == ".agent-loop" or path.startswith(".agent-loop/"):
            return True
    return False


def metrics_report(config: Config) -> str:
    """Text, from the ledger alone - no chart, no new file."""
    records = ledger.read(config.ledger)
    rounds = [record for record in records if record.get("duration_s") is not None]
    counts = Counter(record.get("state") for record in rounds)
    pr_rounds = [record for record in rounds
                if record.get("state") == PR_READY and record.get("pr_url")]
    pr_urls = {record["pr_url"] for record in pr_rounds}
    merged = {record["pr_url"] for record in records
             if record.get("pr_state") == "MERGED" and record.get("pr_url")}
    cost_by_pr: Dict[str, float] = {}
    plumbing = 0
    for record in pr_rounds:
        url = record["pr_url"]
        if url not in cost_by_pr and record.get("cost") is not None:
            cost_by_pr[url] = record["cost"]
        if _touches_plumbing(record.get("diff_stat") or ""):
            plumbing += 1
    merged_costs = [cost_by_pr[url] for url in merged if url in cost_by_pr]
    gaps = [
        ledger.epoch(record["notified_at"]) - ledger.epoch(record["ts"])
        for record in rounds if record.get("notified_at")
    ]
    return "\n".join([
        "rounds by state    " + ", ".join(
            "%s=%d" % (state, counts.get(state, 0)) for state in (PR_READY, BLOCKED, NO_ITEM, INFRA)
        ),
        "PRs                opened=%d merged=%d" % (len(pr_urls), len(merged)),
        ("plumbing share     %d/%d PR(s) touched .agent-loop/" % (plumbing, len(pr_urls))
         if pr_urls else "plumbing share     no PRs yet"),
        ("time to notify     median %.3fs over %d round(s)" % (statistics.median(gaps), len(gaps))
         if gaps else "time to notify     no notified_at recorded"),
        ("cost per merged    $%.4f" % (sum(merged_costs) / len(merged_costs))
         if merged_costs else "cost per merged    no merged PR cost recorded"),
    ])
