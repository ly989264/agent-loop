"""Shared fixtures: a throwaway consumer repository with a config and a backlog."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import textwrap
from pathlib import Path

CONFIG = """
branch: main
backlog: .agent-loop/backlog.yaml
worktree_root: .agent-loop/worktrees
ledger: .agent-loop/ledger.jsonl
protected_paths:
  - project/schemas
  - project/catalog.json
verify:
  hermetic:
    cwd: project
    command: "true"
agents:
  worker:
    - shell:/bin/echo
caps:
  worker:
    wall_s: 60
    silence_s: 30
    max_tokens: 1000
notify:
  - stdout
levels:
  hermetic: L1
"""

BACKLOG = """
items:
  - id: first
    group: g
    statement: first item
    cost_class: hermetic
    selectable: true
    sites: []
    design_doc: "docs/design.md 1"
    probe: "exit 0"
  - id: second
    group: g
    statement: second item
    cost_class: hermetic
    selectable: true
    sites: []
    design_doc: "docs/design.md 2"
    probe: "exit 3"
  - id: third
    group: g
    statement: third item
    cost_class: hermetic
    selectable: true
    sites: []
    design_doc: "docs/design.md 3"
    probe: "exit 4"
  - id: not-selectable
    group: g
    statement: needs a fleet
    cost_class: needs-fleet
    selectable: false
    sites: []
    design_doc: ""
    probe: "exit 5"
  - id: no-probe
    group: g
    statement: has no probe
    cost_class: hermetic
    selectable: true
    sites: []
    design_doc: ""
"""


def make_repo(config: str = CONFIG, backlog: str = BACKLOG) -> Path:
    root = Path(tempfile.mkdtemp(prefix="agent-loop-test-")).resolve()
    (root / ".agent-loop").mkdir()
    (root / "project").mkdir()
    # git tracks no empty directory, so a worktree needs a file to keep project/
    (root / "project" / ".keep").write_text("", encoding="utf-8")
    (root / ".agent-loop" / "config.yaml").write_text(textwrap.dedent(config), encoding="utf-8")
    (root / ".agent-loop" / "backlog.yaml").write_text(textwrap.dedent(backlog), encoding="utf-8")
    return root


def git_init(root: Path) -> None:
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    for argv in (
        ["init", "-b", "main"],
        ["add", "-A"],
        ["commit", "-m", "initial"],
    ):
        subprocess.run(["git"] + argv, cwd=str(root), env=env,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)


def write_script(root: Path, name: str, body: str) -> Path:
    path = root / name
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    path.chmod(0o755)
    return path


def cleanup(root: Path) -> None:
    shutil.rmtree(root, ignore_errors=True)
