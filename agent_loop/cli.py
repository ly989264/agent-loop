"""``agent-loop run --config <path> --mode once`` and ``agent-loop status --config <path>``."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Optional

from . import backlog, config as config_module, ledger, round as round_module
from .errors import ConfigError
from .states import BLOCKED, INFRA, NO_ITEM, PR_READY

EXIT_CODES = {PR_READY: 0, NO_ITEM: 0, BLOCKED: 1, INFRA: 2}


def _status(config_path: Path) -> int:
    try:
        config = config_module.load(config_path)
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


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="agent-loop")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="run one round")
    run_parser.add_argument("--config", required=True, type=Path)
    run_parser.add_argument("--mode", required=True, choices=["once"])

    status_parser = subparsers.add_parser("status", help="show the configuration and the ledger")
    status_parser.add_argument("--config", required=True, type=Path)

    arguments = parser.parse_args(argv)
    if arguments.command == "status":
        return _status(arguments.config)
    outcome = round_module.run_once(arguments.config)
    return EXIT_CODES[outcome.state]


if __name__ == "__main__":
    sys.exit(main())
