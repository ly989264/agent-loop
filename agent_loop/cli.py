"""``agent-loop run --config <path> --mode <mode>`` and the operator commands
around it: ``status``, ``pause``, ``resume``, ``metrics``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Optional

from . import backlog, config as config_module, ledger, modes, plan as plan_module, round as round_module
from .errors import ConfigError
from .states import BLOCKED, INFRA, NO_ITEM, PR_READY

EXIT_CODES = {PR_READY: 0, NO_ITEM: 0, BLOCKED: 1, INFRA: 2}


def _load(config_path: Path):
    try:
        return config_module.load(config_path), None
    except ConfigError as exc:
        print("config error: %s" % exc)
        return None, 2


def _status(config_path: Path) -> int:
    config, failed = _load(config_path)
    if failed is not None:
        return failed
    try:
        items = backlog.load(config.backlog)
    except ConfigError as exc:
        print("config error: %s" % exc)
        return 2
    selectable = [item for item in items if item.selectable]
    probed = [item for item in selectable if item.probe]
    print("branch          %s" % config.branch)
    print("backlog         %s" % config.backlog)
    print("items           %d total, %d selectable, %d with a probe"
          % (len(items), len(selectable), len(probed)))
    print("ledger          %s" % config.ledger)
    print("paused          %s" % ("yes" if modes.paused(config.worktree_root) else "no"))
    records = ledger.read(config.ledger)
    if not records:
        print("no rounds recorded")
        return 0
    print("rounds          %d" % len(records))
    for record in records[-10:]:
        print("  %s %-8s %-44s %s"
              % (record.get("ts"), record.get("state"), record.get("item") or "-",
                 (record.get("reason") or "")[:80]))
    return 0


def _pause(config_path: Path, want_paused: bool) -> int:
    config, failed = _load(config_path)
    if failed is not None:
        return failed
    if want_paused:
        modes.pause(config.worktree_root)
        print("paused: continuous/until will idle between rounds until `agent-loop resume`")
    else:
        modes.resume(config.worktree_root)
        print("resumed")
    return 0


def _metrics(config_path: Path) -> int:
    config, failed = _load(config_path)
    if failed is not None:
        return failed
    print(modes.metrics_report(config))
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="agent-loop")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="run one round, or a driven loop of them")
    run_parser.add_argument("--config", required=True, type=Path)
    run_parser.add_argument(
        "--mode", required=True, choices=["once", "continuous", "schedule", "until"]
    )
    run_parser.add_argument("--until-prs", type=int, default=None)
    run_parser.add_argument("--until-hours", type=float, default=None)
    run_parser.add_argument("--until-cost", type=float, default=None)

    status_parser = subparsers.add_parser("status", help="show the configuration and the ledger")
    status_parser.add_argument("--config", required=True, type=Path)

    pause_parser = subparsers.add_parser("pause", help="idle continuous/until between rounds")
    pause_parser.add_argument("--config", required=True, type=Path)

    resume_parser = subparsers.add_parser("resume", help="undo pause")
    resume_parser.add_argument("--config", required=True, type=Path)

    plan_parser = subparsers.add_parser(
        "plan", help="ask the planner role for backlog proposals and admit them")
    plan_parser.add_argument("--config", required=True, type=Path)

    metrics_parser = subparsers.add_parser("metrics", help="ledger-derived numbers, text only")
    metrics_parser.add_argument("--config", required=True, type=Path)

    arguments = parser.parse_args(argv)
    if arguments.command == "status":
        return _status(arguments.config)
    if arguments.command == "pause":
        return _pause(arguments.config, True)
    if arguments.command == "resume":
        return _pause(arguments.config, False)
    if arguments.command == "metrics":
        return _metrics(arguments.config)
    if arguments.command == "plan":
        # A plan run is not a round, so it has no terminal state and no exit
        # code of one: 0 when the planner answered and admission ran, 2 when it
        # could not - which is what INFRA already means to a caller.
        return 0 if plan_module.run_plan(arguments.config).ok else 2

    # `schedule` needs nothing `once` does not already do - one round, exit
    # code says which of the four states - so it runs the same path.
    if arguments.mode in ("once", "schedule"):
        outcome = round_module.run_once(arguments.config)
        return EXIT_CODES[outcome.state]

    stop = None
    if arguments.mode == "until":
        stop = modes.Stop(
            prs=arguments.until_prs, hours=arguments.until_hours, cost=arguments.until_cost
        )
        if stop.prs is None and stop.hours is None and stop.cost is None:
            print("--mode until needs at least one of --until-prs --until-hours --until-cost")
            return 2
    return modes.run_continuous(arguments.config, stop=stop)


if __name__ == "__main__":
    sys.exit(main())
