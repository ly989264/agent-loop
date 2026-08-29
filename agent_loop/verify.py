"""The verify step: the probe passes, the cost class's command passes, and no
protected path was touched.  Failing any of the three is BLOCKED with the reason.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Sequence, Tuple

from .backlog import Item
from .config import Config
from .pick import command_cwd, run_command


@dataclass(frozen=True)
class VerifyOutcome:
    ok: bool
    reason: str


def touched_protected_paths(
    changed: Sequence[str], protected: Sequence[str]
) -> Tuple[str, ...]:
    hits = []
    for name in changed:
        for guarded in protected:
            if name == guarded or name.startswith(guarded.rstrip("/") + "/"):
                hits.append(name)
                break
    return tuple(hits)


def verify(config: Config, item: Item, tree: Path) -> VerifyOutcome:
    cwd = command_cwd(config, item, tree)

    exit_code, output = run_command(item.probe or "false", cwd)
    if exit_code != 0:
        return VerifyOutcome(False, "probe still fails (exit %d): %s" % (exit_code, output[-800:]))

    command = config.verify_for(item.cost_class).command
    exit_code, output = run_command(command, cwd)
    if exit_code != 0:
        return VerifyOutcome(
            False, "verify command %r failed (exit %d): %s" % (command, exit_code, output[-800:])
        )

    exit_code, output = run_command("git diff --name-only", tree)
    if exit_code != 0:
        return VerifyOutcome(False, "cannot read the round's diff: %s" % output[-800:])
    changed = [line.strip() for line in output.splitlines() if line.strip()]
    hits = touched_protected_paths(changed, config.protected_paths)
    if hits:
        return VerifyOutcome(False, "diff touches protected paths: %s" % ", ".join(hits))
    return VerifyOutcome(True, "probe passes, %s passes, no protected path touched" % command)
