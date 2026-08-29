"""Shared fixtures: a throwaway consumer repository with a config and a backlog."""

from __future__ import annotations

import json
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


FAKE_GH = """\
#!/usr/bin/env python3
import json, os, sys
argv = sys.argv[1:]
stdin = sys.stdin.read() if "--body-file" in argv else ""
with open(os.environ["GH_RECORD"], "a") as handle:
    handle.write(json.dumps({"argv": argv, "stdin": stdin}) + "\\n")
for reply in json.loads(open(os.environ["GH_REPLIES"]).read()):
    if all(token in argv for token in reply["match"]):
        sys.stdout.write(reply.get("out", ""))
        sys.exit(reply.get("code", 0))
sys.exit(0)
"""


def fake_gh(root: Path, replies) -> None:
    """Put a recording ``gh`` on PATH and say what it answers.

    ``replies`` is a list of {"match": [argv tokens], "out": str, "code": int};
    the first whose tokens are all present answers.  Calls land in
    ``root/gh_calls.jsonl`` as one JSON object each.
    """
    binaries = root / "bin"
    binaries.mkdir(exist_ok=True)
    write_script(binaries, "gh", FAKE_GH)
    os.environ["PATH"] = "%s%s%s" % (binaries, os.pathsep, os.environ["PATH"])
    os.environ["GH_RECORD"] = str(root / "gh_calls.jsonl")
    os.environ["GH_REPLIES"] = str(root / "gh_replies.json")
    (root / "gh_replies.json").write_text(json.dumps(replies), encoding="utf-8")


def gh_calls(root: Path):
    path = root / "gh_calls.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def origin_for(root: Path) -> Path:
    """A bare repository the round can really push to."""
    bare = root.parent / (root.name + "-origin.git")
    subprocess.run(["git", "init", "--bare", "-b", "main", str(bare)],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    subprocess.run(["git", "remote", "add", "origin", str(bare)], cwd=str(root),
                   stdout=subprocess.DEVNULL, check=True)
    subprocess.run(["git", "push", "-q", "origin", "main"], cwd=str(root),
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    return bare


def cleanup(root: Path) -> None:
    shutil.rmtree(root, ignore_errors=True)
    shutil.rmtree(root.parent / (root.name + "-origin.git"), ignore_errors=True)
