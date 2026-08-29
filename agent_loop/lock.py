"""One round at a time under one worktree root.

The kernel keeps no loop state on GitHub and none in a daemon, so the only way
two rounds can be told apart is what they leave on disk.  A round takes this
lock before it picks anything, and refuses up front - INFRA, with the holder in
the reason - rather than discovering the collision as a mangled worktree.  A
worktree under the root that this round did not create is the same collision
seen from the other side: an interactive session, or a round that was killed.
"""

from __future__ import annotations

import datetime
import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from .errors import InfraError
from .worktree import worktrees_under

LOCK_NAME = ".lock"


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True
    return True


def _holder(path: Path) -> Optional[dict]:
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def _take(path: Path) -> None:
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    try:
        handle = os.open(str(path), flags, 0o644)
    except FileExistsError:
        holder = _holder(path)
        pid = holder.get("pid") if holder else None
        if isinstance(pid, int) and _alive(pid):
            raise InfraError(
                "another round holds %s (pid %d since %s); wait for it or remove the file"
                % (path, pid, (holder or {}).get("since"))
            )
        # Nobody is behind the file: a killed round leaves it, and refusing for
        # ever afterwards would need a person for every crash.
        try:
            path.unlink()
            handle = os.open(str(path), flags, 0o644)
        except OSError as exc:
            raise InfraError("cannot take the round lock %s: %s" % (path, exc))
    except OSError as exc:
        raise InfraError("cannot take the round lock %s: %s" % (path, exc))
    with os.fdopen(handle, "w") as stream:
        json.dump({"pid": os.getpid(),
                   "since": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")}, stream)


@contextmanager
def hold(repo_root: Path, worktree_root: Path) -> Iterator[Path]:
    worktree_root.mkdir(parents=True, exist_ok=True)
    path = worktree_root / LOCK_NAME
    _take(path)
    try:
        foreign = worktrees_under(repo_root, worktree_root)
        if foreign:
            raise InfraError(
                "a worktree under %s is not this round's: %s; inspect it and "
                "`git worktree remove` it before running again"
                % (worktree_root, ", ".join(foreign))
            )
        yield path
    finally:
        try:
            path.unlink()
        except OSError:
            pass
