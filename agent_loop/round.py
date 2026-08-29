"""One round, in mode ``once``.

    lock-free pick -> worktree -> worker (bounded bundle, one repair) -> verify
    -> ledger line -> one notification

It ends in exactly one of the four terminal states, and emits exactly one
notification for it, deduplicated by (item, state, sha).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from . import backlog, config as config_module, context, ledger, lock, notify, pick, verify
from .adapters import allowed_tools, build, invoke_with_one_repair
from .config import Config
from .context import ContextTooLarge
from .errors import ConfigError, InfraError
from .schemas import WORKER_OUTPUT_SCHEMA
from .states import BLOCKED, INFRA, NO_ITEM, PR_READY
from .worktree import Workspace, commit_all, head_sha, workspace


@dataclass(frozen=True)
class Outcome:
    state: str
    item: Optional[str]
    reason: str
    cost: Optional[float]
    duration_s: float
    notified: bool


def _retain(space: Workspace, item: backlog.Item) -> Optional[str]:
    """Keep the round's diff where it can be inspected, or say why it could not.

    The diff is committed onto ``explore/<item>`` and the branch is kept.  A
    ``git commit`` can still fail - a repository hook, ``commit.gpgsign`` - and
    then the worktree holds the only copy, so it is kept too and the round ends
    INFRA rather than deleting the evidence it exists to keep.
    """
    space.keep_branch = True
    try:
        commit_all(space.tree, "agent-loop: %s" % item.id)
    except InfraError as exc:
        space.keep_tree = True
        return "%s; worktree kept at %s" % (exc, space.tree)
    return None


def _worker_round(config: Config, selection: pick.Selection, sha: str) -> Tuple[str, str, Optional[float]]:
    item = selection.item
    with workspace(config.root, config.branch, config.worktree_root, item.id) as space:
        bundle = context.build_worker_bundle(
            item=item,
            probe_output=selection.probe.output,
            probe_exit_code=selection.probe.exit_code,
            root=space.tree,
            schema=WORKER_OUTPUT_SCHEMA,
            sha=sha,
        )
        try:
            encoded = context.encode(bundle)
        except ContextTooLarge as exc:
            return BLOCKED, str(exc), None
        adapter = build(
            config.ladder("worker")[0],
            cwd=space.tree,
            allowed_tools=allowed_tools([config.verify_for(item.cost_class).command, item.probe]),
        )
        result = invoke_with_one_repair(
            adapter,
            role="worker",
            bundle=encoded,
            schema=WORKER_OUTPUT_SCHEMA,
            sandbox="worktree-write",
            budget=config.budget("worker"),
        )
        if result.status != "ok":
            return INFRA, "worker returned %s: %s" % (result.status, result.raw_tail[-800:]), result.cost
        payload: Dict[str, Any] = result.json or {}
        if payload.get("status") == "blocked":
            return BLOCKED, "worker blocked: %s" % payload.get("reason", ""), result.cost
        outcome = verify.verify(config, item, space.tree, sha)
        if not outcome.ok:
            # A BLOCKED that comes from verify has a diff worth reading: the
            # worker answered `done` and something the kernel checked said no.
            # It is retained exactly as PR_READY is, so the failing check can be
            # judged against the change that met it.  A worker that answers
            # `blocked` returned above, with nothing to keep.
            failure = _retain(space, item)
            if failure:
                return INFRA, "%s; %s" % (outcome.reason, failure), result.cost
            return BLOCKED, outcome.reason, result.cost
        evidence = payload.get("mutation_evidence") or {}
        reason = "%s; test %s; reverted %r observed %r" % (
            outcome.reason,
            payload.get("test_path"),
            evidence.get("reverted_command"),
            evidence.get("observed_failure_line"),
        )
        failure = _retain(space, item)
        if failure:
            return INFRA, failure, result.cost
        return PR_READY, reason, result.cost


def run_once(config_path: Path) -> Outcome:
    started = time.time()
    try:
        config = config_module.load(config_path)
    except ConfigError as exc:
        print(notify.line(None, INFRA, "", str(exc)))
        return Outcome(INFRA, None, str(exc), None, time.time() - started, True)

    item_id: Optional[str] = None
    sha = ""
    cost: Optional[float] = None
    try:
        sha = head_sha(config.root, config.branch)
        with lock.hold(config.root, config.worktree_root):
            items = backlog.load(config.backlog)
            records = ledger.read(config.ledger)
            selection = pick.pick(config, items, config.root, ledger.blocked_at(records, sha))
            if selection is None:
                state, reason = NO_ITEM, "all selectable probes pass"
            else:
                item_id = selection.item.id
                state, reason, cost = _worker_round(config, selection, sha)
    except (ConfigError, InfraError) as exc:
        records = ledger.read(config.ledger)
        state, reason = INFRA, str(exc)
    except Exception as exc:  # noqa: BLE001 - a round always ends in a state
        records = ledger.read(config.ledger)
        state, reason = INFRA, "unexpected %s: %s" % (type(exc).__name__, exc)

    versions = ledger.tool_versions(
        sorted({spec.adapter for specs in config.agents.values() for spec in specs})
    )
    notified = not ledger.already_notified(records, item_id, state, sha)
    duration = time.time() - started
    ledger.append(
        config.ledger,
        {
            "ts": ledger.now(),
            "item": item_id,
            "sha": sha,
            "state": state,
            "reason": reason,
            "cost": cost,
            "duration_s": round(duration, 3),
            "tool_versions": versions,
            "warning": ledger.drift(records, versions),
        },
    )
    if notified:
        notify.notify(config, item=item_id, state=state, sha=sha, reason=reason)
    return Outcome(state, item_id, reason, cost, duration, notified)
