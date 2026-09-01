"""One round, in mode ``once``.

    lock -> pick -> worktree -> worker (bounded bundle, one repair) -> verify
    -> ledger line -> one notification

It ends in exactly one of the four terminal states, and emits exactly one
notification for it, deduplicated by (item, state, sha).
"""

from __future__ import annotations

import signal
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from . import (
    backlog, config as config_module, context, ledger, level, lock, notify, pick, scm, verify,
)
from .adapters import allowed_tools, build, invoke_with_one_repair
from .config import Config
from .context import ContextTooLarge
from .errors import ConfigError, InfraError
from .schemas import REVIEWER_OUTPUT_SCHEMA, WORKER_OUTPUT_SCHEMA
from .states import BLOCKED, INFRA, NO_ITEM, PR_READY
from .worktree import EXPLORE_PREFIX, Workspace, branch_exists, commit_all, head_sha, workspace


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
    review_posted: bool = False
    diff_stat: str = ""
    pr_state: Optional[str] = None


def _round_wall_handler(seconds: int):
    """caps.round_wall_s, enforced in-process: the alarm's InfraError takes the
    same path any other one does - existing worktree cleanup (workspace()'s
    finally), the existing except clause below, one ledger line, one
    deduplicated notification. Nothing is bypassed because nothing new exists.
    """
    def _handler(signum, frame):
        raise InfraError("round exceeded caps.round_wall_s (%ds)" % seconds)
    return _handler


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
    records: Sequence[Dict[str, Any]],
    space: Workspace,
    item: backlog.Item,
    sha: str,
    reason: str,
    payload: Mapping[str, Any],
    cost: Optional[float],
    outcome: verify.VerifyOutcome,
) -> Result:
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
    try:
        publication = publisher.publish(
            root=config.root,
            branch=space.branch,
            base=config.branch,
            title="agent-loop: %s" % item.id,
            body=body,
        )
    except InfraError:
        # A publish that fails part-way may already have put explore/<item> on
        # origin.  Keeping the local branch as well would make every later round
        # on this item INFRA at `git worktree add -b`, so the branch goes and the
        # item stays re-runnable; what the round did is in the reason below.
        space.keep_branch = False
        raise
    if publication.pull_request is None:
        return Result(PR_READY, "%s; %s" % (reason, publication.reason), cost, diff_stat=diff_stat)
    space.keep_branch = False
    findings, note, posted = _review(
        config, records, publisher, item, sha, space.branch, publication.pull_request
    )
    chosen = level.decide(
        config.level(item.cost_class), findings, outcome.protected, outcome.ok
    )
    pr_state: Optional[str] = None
    if chosen.merge:
        refusal = publisher.merge(config.root, publication.pull_request)
        note += "; merged" if not refusal else "; not merged: %s" % refusal
        if refusal:
            chosen = level.Decision(False, level.DECIDE, "the squash-merge was refused")
        else:
            pr_state = "MERGED"
    return Result(
        PR_READY,
        "%s; %s; %s; %s" % (reason, publication.pull_request.url, note, chosen.reason),
        cost,
        publication.pull_request.url,
        chosen.decision,
        posted,
        diff_stat,
        pr_state,
    )


def _review(
    config: Config,
    records: Sequence[Dict[str, Any]],
    publisher: scm.Publisher,
    item: backlog.Item,
    sha: str,
    branch: str,
    pull_request: scm.PullRequest,
) -> Tuple[List[Dict[str, Any]], str, bool]:
    """Ask the reviewer about the published diff and post its findings, once.

    The findings are posted and nothing else: they are not fed back to the
    worker, which in this stage has no fix loop.  A review that cannot be run
    is said so on the round's line and never costs the round its pull request -
    the change is already published, and losing it to a review failure would
    lose the thing the round exists to produce.

    A pull request the ledger already records a posted review for gets no second
    comment.  The ledger is asked, not the forge: a marker read back off the
    comments would be loop state living on GitHub.
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
        return [], "no review: %s" % exc, False
    if result.status != "ok":
        return [], "no review: reviewer returned %s" % result.status, False
    findings = (result.json or {}).get("findings") or []
    if ledger.reviewed(records, pull_request.url):
        return findings, "%d finding(s), already commented" % len(findings), False
    try:
        publisher.comment(config.root, pull_request, scm.review_comment(findings))
    except InfraError as exc:
        return findings, "%d finding(s), not posted: %s" % (len(findings), exc), False
    return findings, "%d finding(s), posted" % len(findings), True


def _worker_round(
    config: Config, records: Sequence[Dict[str, Any]], selection: pick.Selection, sha: str
) -> Result:
    item = selection.item
    kept = EXPLORE_PREFIX + item.id
    if branch_exists(config.root, kept):
        # A previous round on this item left its result on that branch and
        # nothing has taken custody of it - at L1 the merge is a person's, and
        # under `scm: local-only` the branch is the whole deliverable, so it is
        # never deleted.  `git worktree add -b` would then fail, and reporting a
        # raw git error as INFRA is wrong twice over: nothing about the machine
        # failed, and INFRA is not skipped at this sha, so pick - which takes the
        # first failing probe in file order - chooses the same item every round
        # and no later item is ever reached.  BLOCKED is what this is: the round
        # cannot proceed until a person acts, it says what they must do, and
        # ledger.blocked_at lets the next round move on to the next item.
        return Result(
            BLOCKED,
            "%s already holds a previous round's result; merge or delete it "
            "before this item is run again" % kept,
        )
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
            # Stage 6: the worker is the role that writes, so it is the role
            # the jail is for.  The reviewer reads a published diff read-only
            # and verify's commands are the operator's own data; both stay
            # host-side.
            jail=config.jail,
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
        return _publish(
            config, records, space, item, sha, reason, payload, result.cost, outcome
        )


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
    review_posted: bool = False
    diff_stat: str = ""
    pr_state: Optional[str] = None
    signal.signal(signal.SIGALRM, _round_wall_handler(config.round_wall_s))
    signal.alarm(config.round_wall_s)
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
                    result = _worker_round(config, records, selection, sha)
                    state, reason, cost, pr_url, decision, review_posted, diff_stat, pr_state = (
                        result.state, result.reason, result.cost, result.pr_url,
                        result.decision, result.review_posted, result.diff_stat,
                        result.pr_state)
    except (ConfigError, InfraError) as exc:
        records = ledger.read(config.ledger)
        state, reason = INFRA, str(exc)
    except Exception as exc:  # noqa: BLE001 - a round always ends in a state
        records = ledger.read(config.ledger)
        state, reason = INFRA, "unexpected %s: %s" % (type(exc).__name__, exc)
    finally:
        signal.alarm(0)

    versions = ledger.tool_versions(
        sorted({spec.adapter for specs in config.agents.values() for spec in specs})
    )
    notified = not ledger.already_notified(records, item_id, state, sha)
    ts = ledger.now()
    notified_at: Optional[str] = None
    if notified:
        # ts is fixed before the notification goes out, so notified_at can
        # never read as earlier than the terminal state it is timing from -
        # Stage 4b metrics reads the gap between the two off this line.
        notify.notify(
            config, item=item_id, state=state, sha=sha, reason=reason, decision=decision
        )
        notified_at = ledger.now()
    duration = time.time() - started
    ledger.append(
        config.ledger,
        {
            "ts": ts,
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
            "review_posted": review_posted,
            "diff_stat": diff_stat or None,
            "pr_state": pr_state,
            "notified_at": notified_at,
        },
    )
    return Outcome(state, item_id, reason, cost, duration, notified, pr_url)
