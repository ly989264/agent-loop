"""One round, in mode ``once``.

    lock -> pick -> worktree -> worker (bounded bundle, one repair) -> verify
    -> ledger line -> one notification

It ends in exactly one of the four terminal states, and emits exactly one
notification for it, deduplicated by (item, state, sha).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from . import (
    backlog, config as config_module, context, ledger, level, lock, notify, pick, scm, verify,
)
from .adapters import allowed_tools, build, invoke_with_one_repair
from .config import Config
from .context import ContextTooLarge
from .errors import ConfigError, InfraError
from .schemas import REVIEWER_OUTPUT_SCHEMA, WORKER_OUTPUT_SCHEMA
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
    pr_url: Optional[str] = None


@dataclass(frozen=True)
class Result:
    """What the worker part of a round produced, before it is written down."""

    state: str
    reason: str
    cost: Optional[float] = None
    pr_url: Optional[str] = None
    decision: Optional[str] = None


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


def _publish(
    config: Config,
    space: Workspace,
    item: backlog.Item,
    sha: str,
    reason: str,
    payload: Mapping[str, Any],
    cost: Optional[float],
    outcome: verify.VerifyOutcome,
) -> Tuple[str, Optional[str], Optional[str]]:
    """Open or update the item's pull request, before anything is cleaned up.

    2a deferred 3: cleanup used to delete ``explore/<item>``, so a Stage 3 push
    has to happen while the branch is still there.  Once origin holds it the
    local branch is no longer the only copy, so cleanup takes it after all -
    which is also what stops a later round on the same item tripping over it.
    """
    publisher = scm.build(config.scm)
    _, diff_stat = pick.run_command(
        "git diff --stat %s...%s" % (sha, space.branch), config.root
    )
    body = scm.pr_body(
        item_id=item.id,
        statement=item.statement,
        worker_reason=reason,
        evidence=payload.get("mutation_evidence") or {},
        ledger_line={"ts": ledger.now(), "item": item.id, "sha": sha,
                     "state": PR_READY, "cost": cost, "duration_s": None},
        diff_stat=diff_stat,
    )
    publication = publisher.publish(
        root=config.root,
        branch=space.branch,
        base=config.branch,
        title="agent-loop: %s" % item.id,
        body=body,
    )
    if publication.pull_request is None:
        return publication.reason, None, None
    space.keep_branch = False
    findings, note = _review(config, publisher, item, sha, space.branch, publication.pull_request)
    chosen = level.decide(
        config.level(item.cost_class), findings, outcome.protected, outcome.ok
    )
    if chosen.merge:
        refusal = publisher.merge(config.root, publication.pull_request)
        note += "; merged" if not refusal else "; not merged: %s" % refusal
        if refusal:
            chosen = level.Decision(False, level.DECIDE, "the squash-merge was refused")
    return (
        "%s; %s; %s" % (publication.pull_request.url, note, chosen.reason),
        publication.pull_request.url,
        chosen.decision,
    )


def _review(
    config: Config,
    publisher: scm.Publisher,
    item: backlog.Item,
    sha: str,
    branch: str,
    pull_request: scm.PullRequest,
) -> Tuple[List[Dict[str, Any]], str]:
    """Ask the reviewer about the published diff and post its findings, once.

    The findings are posted and nothing else: they are not fed back to the
    worker, which in this stage has no fix loop.  A review that cannot be run
    is said so on the round's line and never costs the round its pull request -
    the change is already published, and losing it to a review failure would
    lose the thing the round exists to produce.
    """
    try:
        _, diff = pick.run_command("git diff %s...%s" % (sha, branch), config.root)
        bundle = context.build_reviewer_bundle(
            item=item, diff=diff, sha=sha, schema=REVIEWER_OUTPUT_SCHEMA
        )
        result = invoke_with_one_repair(
            build(config.ladder("reviewer")[0], cwd=config.root),
            role="reviewer",
            bundle=context.encode(bundle),
            schema=REVIEWER_OUTPUT_SCHEMA,
            sandbox="read-only",
            budget=config.budget("reviewer"),
        )
    except (ConfigError, InfraError, ContextTooLarge) as exc:
        return [], "no review: %s" % exc
    if result.status != "ok":
        return [], "no review: reviewer returned %s" % result.status
    findings = (result.json or {}).get("findings") or []
    try:
        posted = publisher.comment(config.root, pull_request, scm.review_comment(findings))
    except InfraError as exc:
        return findings, "%d finding(s), not posted: %s" % (len(findings), exc)
    return findings, "%d finding(s), %s" % (
        len(findings),
        "posted" if posted else "already commented",
    )


def _worker_round(config: Config, selection: pick.Selection, sha: str) -> Result:
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
            return Result(BLOCKED, str(exc))
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
            return Result(
                INFRA,
                "worker returned %s: %s" % (result.status, result.raw_tail[-800:]),
                result.cost,
            )
        payload: Dict[str, Any] = result.json or {}
        if payload.get("status") == "blocked":
            return Result(BLOCKED, "worker blocked: %s" % payload.get("reason", ""), result.cost)
        outcome = verify.verify(config, item, space.tree, sha)
        if not outcome.ok:
            # A BLOCKED that comes from verify has a diff worth reading: the
            # worker answered `done` and something the kernel checked said no.
            # It is retained exactly as PR_READY is, so the failing check can be
            # judged against the change that met it.  A worker that answers
            # `blocked` returned above, with nothing to keep.
            failure = _retain(space, item)
            if failure:
                return Result(INFRA, "%s; %s" % (outcome.reason, failure), result.cost)
            return Result(BLOCKED, outcome.reason, result.cost)
        evidence = payload.get("mutation_evidence") or {}
        reason = "%s; test %s; reverted %r observed %r" % (
            outcome.reason,
            payload.get("test_path"),
            evidence.get("reverted_command"),
            evidence.get("observed_failure_line"),
        )
        failure = _retain(space, item)
        if failure:
            return Result(INFRA, failure, result.cost)
        published, pr_url, decision = _publish(
            config, space, item, sha, reason, payload, result.cost, outcome
        )
        return Result(PR_READY, "%s; %s" % (reason, published), result.cost, pr_url, decision)


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
    pr_url: Optional[str] = None
    decision: Optional[str] = None
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
                # An unanswered DECIDE belongs to the operator: a further round
                # on that item would only ask the same question again.
                publisher = scm.build(config.scm)
                stale = level.expired(
                    records, item_id, time.time(),
                    lambda url: publisher.is_open(config.root, url),
                )
                if stale:
                    state, reason = BLOCKED, stale
                else:
                    result = _worker_round(config, selection, sha)
                    state, reason, cost, pr_url, decision = (
                        result.state, result.reason, result.cost, result.pr_url, result.decision)
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
            "pr_url": pr_url,
            "decision": decision,
        },
    )
    if notified:
        notify.notify(
            config, item=item_id, state=state, sha=sha, reason=reason, decision=decision
        )
    return Outcome(state, item_id, reason, cost, duration, notified, pr_url)
