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


# A row or line carrying one of these names what failed.  Lower-case "fail"
# is deliberately absent: "0 failures" is not a failing line.
FAILURE_MARKERS = ("FAIL", "ERROR", "Error", "Traceback")
FAILING_LINE_LIMIT = 40


def failing_lines(output: str, limit: int = FAILING_LINE_LIMIT) -> str:
    """The lines that name what failed, not whatever the tail happened to hold.

    A suite runner prints one row per check and a short summary, so the last
    800 characters of a failing run are the rows that passed plus `Status:
    FAIL` - the failing check's name is above the cut and is lost.  Keeping the
    marked lines instead makes the ledger's reason name it.  Output with no
    marked line at all falls back to the tail, which is all there is.
    """
    marked = [
        line.rstrip() for line in output.splitlines()
        if any(marker in line for marker in FAILURE_MARKERS)
    ]
    if not marked:
        return output[-800:]
    dropped = len(marked) - limit
    kept = marked[-limit:]
    if dropped > 0:
        kept.insert(0, "... %d earlier failing lines" % dropped)
    return "\n".join(kept)


@dataclass(frozen=True)
class VerifyOutcome:
    ok: bool
    reason: str


def changed_paths(tree: Path, base_sha: str) -> Tuple[int, Sequence[str], str]:
    """Every path this round touched: committed, modified, added or untracked.

    ``git diff --name-only`` alone sees neither a file the worker committed nor
    one it created, so a new project/schemas/*.json would pass unnoticed.
    """
    names = []
    exit_code, output = run_command("git diff --name-only %s" % base_sha, tree)
    if exit_code != 0:
        return exit_code, names, output
    names.extend(line.strip() for line in output.splitlines() if line.strip())
    exit_code, output = run_command("git status --porcelain --untracked-files=all", tree)
    if exit_code != 0:
        return exit_code, names, output
    for line in output.splitlines():
        if not line.strip():
            continue
        name = line[3:].strip() if len(line) > 3 else ""
        if " -> " in name:
            name = name.split(" -> ", 1)[1]
        name = name.strip('"')
        if name:
            names.append(name)
    return 0, sorted(set(names)), ""


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


def verify(config: Config, item: Item, tree: Path, base_sha: str) -> VerifyOutcome:
    cwd = command_cwd(config, item, tree)

    exit_code, output = run_command(item.probe or "false", cwd)
    if exit_code != 0:
        return VerifyOutcome(False, "probe still fails (exit %d): %s" % (exit_code, output[-800:]))

    command = config.verify_for(item.cost_class).command
    exit_code, output = run_command(command, cwd)
    if exit_code != 0:
        return VerifyOutcome(
            False,
            "verify command %r failed (exit %d): %s"
            % (command, exit_code, failing_lines(output)),
        )

    exit_code, changed, output = changed_paths(tree, base_sha)
    if exit_code != 0:
        return VerifyOutcome(False, "cannot read the round's diff: %s" % output[-800:])
    hits = touched_protected_paths(changed, config.protected_paths)
    if hits:
        return VerifyOutcome(False, "diff touches protected paths: %s" % ", ".join(hits))
    return VerifyOutcome(True, "probe passes, %s passes, no protected path touched" % command)
