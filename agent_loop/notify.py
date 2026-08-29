"""One notification per terminal state, deduplicated by (item, state, sha).

Nothing else in the kernel notifies.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional, Sequence

from .config import Config, NotifyTarget


def line(
    item: Optional[str], state: str, sha: str, reason: str, decision: Optional[str] = None
) -> str:
    """One operator-visible line; ``decision`` is FYI or DECIDE where there is one.

    A round with nothing to decide - NO_ITEM, INFRA, a BLOCKED with no pull
    request - carries no prefix, because there is no question in it.
    """
    text = "%-8s %-44s %s  %s" % (state, item or "-", sha[:12], reason)
    return text if decision is None else "%-6s %s" % (decision, text)


def _emit(target: NotifyTarget, root: Path, text: str) -> None:
    if target.kind == "stdout":
        print(text)
        return
    if target.kind == "file":
        path = Path(target.path)
        if not path.is_absolute():
            path = root / path
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(text + "\n")
        return
    if target.kind == "macos":
        message = text.replace('"', "'")
        subprocess.run(
            [
                "osascript",
                "-e",
                'display notification "%s" with title "agent-loop"' % message,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
        )


def notify(
    config: Config,
    *,
    item: Optional[str],
    state: str,
    sha: str,
    reason: str,
    decision: Optional[str] = None,
    targets: Optional[Sequence[NotifyTarget]] = None,
) -> str:
    text = line(item, state, sha, reason, decision)
    for target in config.notify if targets is None else targets:
        _emit(target, config.root, text)
    return text
