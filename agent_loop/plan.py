"""``agent-loop plan``: the planner role, and admission of what it proposes.

    bundle (backlog + ledger tail + plan_sources) -> planner -> proposals
      -> duplicate guard -> probe run -> proposals.yaml -> one FYI

A plan run is not a round: it picks no item, opens no pull request and cannot
end in one of the four terminal states, so it writes no ledger line and takes
``notify.fyi`` - the one operator line that carries no state column - rather
than inventing a fifth state or reusing a wrong one.

Invariant 2 is the whole of admission: the kernel runs each proposal's probe
itself and a probe that exits 0 is rejected.  What the planner claims to have
watched fail is not evidence; what the kernel watched fail is.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

import yaml

from . import backlog, config as config_module, context, ledger, notify
from .adapters import build, invoke_with_one_repair
from .config import PLANNER, Config
from .context import ContextTooLarge
from .errors import ConfigError, InfraError
from .pick import run_command
from .schemas import PLANNER_OUTPUT_SCHEMA
from .states import INFRA

PROBE_TAIL_BYTES = 800
PROPOSALS_FILE = "proposals.yaml"


@dataclass(frozen=True)
class PlanOutcome:
    ok: bool
    admitted: int
    rejected: int
    reason: str
    proposals_path: Optional[Path] = None


def _normalise(statement: Any) -> str:
    return " ".join(str(statement or "").split()).casefold()


def admit(
    config: Config, items: Sequence[backlog.Item], proposals: Sequence[Mapping[str, Any]]
) -> List[Dict[str, Any]]:
    """Judge each proposal, in order, and record why.

    A proposal admitted here joins the sets the next one is judged against, so
    a planner that proposes the same item twice has it rejected the second time.
    """
    ids = {item.id for item in items}
    statements = {_normalise(item.statement) for item in items}
    judged: List[Dict[str, Any]] = []
    for proposal in proposals:
        record = dict(proposal)
        cost_class = str(proposal.get("cost_class"))
        if proposal.get("id") in ids or _normalise(proposal.get("statement")) in statements:
            record.update(admitted=False, rejection="duplicates an existing backlog item")
        elif cost_class not in config.verify:
            record.update(
                admitted=False,
                rejection="cost class %r has no verify entry, so its probe has no cwd"
                % cost_class,
            )
        else:
            cwd = config.root / config.verify[cost_class].cwd
            exit_code, output = run_command(str(proposal.get("probe")), cwd)
            record["probe_observed"] = {
                "exit_code": exit_code,
                "cwd": config.verify[cost_class].cwd,
                "output_tail": output[-PROBE_TAIL_BYTES:],
            }
            if exit_code == 0:
                record.update(
                    admitted=False,
                    rejection="probe exits 0 on this tree; invariant 2 admits nothing "
                    "without a probe watched to fail",
                )
            else:
                record.update(admitted=True, rejection=None)
                ids.add(str(proposal.get("id")))
                statements.add(_normalise(proposal.get("statement")))
        judged.append(record)
    return judged


def write_proposals(path: Path, judged: Sequence[Mapping[str, Any]]) -> Path:
    """This plan run's whole record - admitted and rejected alike, with the
    probe exit and output tail behind each verdict.  It is rewritten each run:
    a proposal is a judgement about the tree as it is now, and yesterday's is
    in the ledger of nothing.  It is deliberately not the backlog.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(
            {"generated": ledger.now(), "proposals": [dict(entry) for entry in judged]},
            default_flow_style=False,
            sort_keys=True,
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    return path


def run_plan(config_path: Path) -> PlanOutcome:
    try:
        config = config_module.load(config_path)
    except ConfigError as exc:
        print(notify.line(None, INFRA, "", str(exc)))
        return PlanOutcome(False, 0, 0, str(exc))

    try:
        items = backlog.load(config.backlog)
        bundle = context.build_planner_bundle(
            items=items,
            records=ledger.read(config.ledger),
            excerpts=context.plan_excerpts(config.root, config.plan_sources),
            schema=PLANNER_OUTPUT_SCHEMA,
        )
        result = invoke_with_one_repair(
            build(config.ladder(PLANNER)[0], cwd=config.root),
            role=PLANNER,
            bundle=context.encode(bundle),
            schema=PLANNER_OUTPUT_SCHEMA,
            sandbox="read-only",
            budget=config.budget(PLANNER),
        )
    except (ConfigError, InfraError, ContextTooLarge) as exc:
        notify.fyi(config, "plan: no proposals - %s" % exc)
        return PlanOutcome(False, 0, 0, str(exc))
    if result.status != "ok":
        reason = "planner returned %s: %s" % (result.status, result.raw_tail[-400:])
        notify.fyi(config, "plan: no proposals - %s" % " ".join(reason.split()))
        return PlanOutcome(False, 0, 0, reason)

    judged = admit(config, items, (result.json or {}).get("proposals") or [])
    path = write_proposals(config.worktree_root / PROPOSALS_FILE, judged)
    admitted = [entry for entry in judged if entry.get("admitted")]
    reason = "%d admitted, %d rejected; %s" % (len(admitted), len(judged) - len(admitted), path)
    notify.fyi(config, "plan: " + reason)
    return PlanOutcome(True, len(admitted), len(judged) - len(admitted), reason, path)
