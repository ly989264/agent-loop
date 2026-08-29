"""The round's throwaway worktree, removed on every exit path.

The scoping idea is ported from valkey_scale_lab ``.github/milestone-loop/recovery.py``:
cleanup only ever touches resources this round created, identified by where they
live.  A path outside the configured worktree root is refused rather than removed.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterator, Optional, Sequence

from .errors import InfraError


EXPLORE_PREFIX = "explore/"


@dataclass
class Workspace:
    tree: Path
    temp_dir: Optional[Path] = None
    branch: str = ""
    # Set by the round once it has something worth inspecting: the worktree
    # and temp dir still go, the explore/ branch stays.
    keep_branch: bool = False


def _git(argv: Sequence[str], cwd: Path, timeout: int = 120) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["git"] + list(argv),
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise InfraError("git %s cannot run: %s" % (" ".join(argv), exc))


def head_sha(repo_root: Path, branch: str) -> str:
    result = _git(["rev-parse", branch], repo_root)
    if result.returncode != 0:
        raise InfraError("cannot resolve branch %r: %s" % (branch, result.stdout.strip()))
    return result.stdout.strip()


def remove(repo_root: Path, workspace: Workspace, worktree_root: Path) -> None:
    """Remove the worktree, its branch and the temp dir this round created.

    Nothing else: a path outside the configured worktree root and a branch
    outside the explore/ prefix are this round's to touch only if it made them.
    A workspace marked ``keep_branch`` keeps its explore/ branch, so a
    PR_READY result leaves a diff to inspect after the worktree is gone.
    """
    root = worktree_root.resolve()
    tree = workspace.tree.resolve()
    if tree != root and root in tree.parents:
        if tree.exists():
            _git(["worktree", "remove", "--force", str(tree)], repo_root)
        if tree.exists():
            shutil.rmtree(tree, ignore_errors=True)
        _git(["worktree", "prune"], repo_root)
        if workspace.branch.startswith(EXPLORE_PREFIX) and not workspace.keep_branch:
            _git(["branch", "-D", workspace.branch], repo_root)
    if workspace.temp_dir is None:
        return
    temp_dir = workspace.temp_dir.resolve()
    if temp_dir.exists() and temp_dir != Path(tempfile.gettempdir()).resolve():
        shutil.rmtree(temp_dir, ignore_errors=True)


@contextmanager
def workspace(repo_root: Path, branch: str, worktree_root: Path, item_id: str) -> Iterator[Workspace]:
    worktree_root.mkdir(parents=True, exist_ok=True)
    tree = worktree_root / item_id
    if tree.exists():
        raise InfraError("worktree %s already exists; a previous round did not clean up" % tree)
    explore_branch = EXPLORE_PREFIX + item_id
    # Everything this round creates is created inside the try, so a failure
    # between two of them still leaves nothing behind. Creating the worktree
    # first and entering the try afterwards left it for every later round to
    # trip over.
    created = Workspace(tree=tree)
    try:
        result = _git(
            ["worktree", "add", "-b", explore_branch, str(tree), branch], repo_root, timeout=600
        )
        if result.returncode != 0:
            raise InfraError(
                "cannot create worktree for %s: %s" % (item_id, result.stdout.strip())
            )
        created = Workspace(tree=tree, branch=explore_branch)
        created = replace(created, temp_dir=Path(tempfile.mkdtemp(prefix="agent-loop-")))
        yield created
    finally:
        remove(repo_root, created, worktree_root)
