"""Probe-based selection.

Every selectable item's probe is run; the item chosen is the first one in file
order whose probe fails, because file order is priority order.  An item without a
probe cannot be admitted, and an item already BLOCKED at this sha is skipped
without spending its probe.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

from .backlog import Item
from .config import Config
from .environment import agent_environment

PROBE_TIMEOUT_S = 600


@dataclass(frozen=True)
class ProbeRun:
    item: Item
    exit_code: int
    output: str

    @property
    def failing(self) -> bool:
        return self.exit_code != 0


@dataclass(frozen=True)
class Selection:
    item: Item
    probe: ProbeRun
    probes: Tuple[ProbeRun, ...]


def command_cwd(config: Config, item: Item, tree: Path) -> Path:
    """Where a consumer's own commands run, per its verify entry for this class."""
    return tree / config.verify_for(item.cost_class).cwd


def run_command(command: str, cwd: Path, timeout: int = PROBE_TIMEOUT_S) -> Tuple[int, str]:
    try:
        completed = subprocess.run(
            ["sh", "-c", command],
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
            env=agent_environment(),
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return 124, "command exceeded its %ds timeout: %s" % (timeout, command)
    except OSError as exc:
        return 127, "cannot run %s: %s" % (command, exc)
    return completed.returncode, completed.stdout


def run_probes(
    config: Config,
    items: Sequence[Item],
    tree: Path,
    skip_ids: Iterable[str] = (),
) -> List[ProbeRun]:
    skip = set(skip_ids)
    runs: List[ProbeRun] = []
    for item in items:
        if not item.selectable or not item.probe or item.id in skip:
            continue
        exit_code, output = run_command(item.probe, command_cwd(config, item, tree))
        runs.append(ProbeRun(item=item, exit_code=exit_code, output=output))
    return runs


def pick(
    config: Config,
    items: Sequence[Item],
    tree: Path,
    skip_ids: Iterable[str] = (),
) -> Optional[Selection]:
    runs = run_probes(config, items, tree, skip_ids)
    for run in runs:
        if run.failing:
            return Selection(item=run.item, probe=run, probes=tuple(runs))
    return None
