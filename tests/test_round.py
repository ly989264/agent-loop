"""One whole round in mode ``once``, with a fake shell agent: the terminal state,
the ledger line, and the notification deduplicated by (item, state, sha).
"""

import contextlib
import dataclasses
import io
import json
import os
import subprocess
import unittest

from agent_loop import ledger, round as round_module
from agent_loop.states import BLOCKED, INFRA, NO_ITEM, PR_READY

from support import cleanup, fake_gh, gh_calls, git_init, make_repo, origin_for, write_script

CONFIG = """
branch: main
backlog: .agent-loop/backlog.yaml
worktree_root: .agent-loop/worktrees
ledger: .agent-loop/ledger.jsonl
protected_paths:
  - project/catalog.json
verify:
  hermetic:
    cwd: project
    command: "true"
agents:
  worker:
    - shell:%s
caps:
  worker:
    wall_s: 60
    silence_s: 30
    max_tokens: 1000
notify:
  - target: file
    path: .agent-loop/notifications.log
levels:
  hermetic: L1
"""

BACKLOG = """
items:
  - id: an-item
    group: g
    statement: the probe fails while this is open
    cost_class: hermetic
    selectable: true
    sites: []
    design_doc: "docs/design.md 1"
    probe: "test -f fixed.txt"
    proof: "the probe exits 0 once the file is there"
"""

ANSWER = {
    "diff_applied": True,
    "test_path": "project/tests/test_thing.py",
    "mutation_evidence": {"reverted_command": "git stash && pytest", "observed_failure_line": "E assert"},
    "status": "done",
    "reason": "",
}

AGENT = """\
#!/usr/bin/env python3
import sys
sys.stdin.read()
open("project/fixed.txt", "w").write("fixed\\n")
print(%s)
"""

BLOCKING_AGENT = """\
#!/usr/bin/env python3
import sys
sys.stdin.read()
print(%s)
"""


class RoundTest(unittest.TestCase):
    def build(self, agent_body, config=CONFIG):
        self.root = make_repo(config="branch: main\n", backlog=BACKLOG)
        script = write_script(self.root, "agent.py", agent_body)
        (self.root / ".agent-loop" / "config.yaml").write_text(
            config % script, encoding="utf-8")
        git_init(self.root)
        return self.root / ".agent-loop" / "config.yaml"

    def setUp(self):
        self.path = os.environ["PATH"]

    def tearDown(self):
        os.environ["PATH"] = self.path
        cleanup(self.root)

    def run_once(self, config_path):
        with contextlib.redirect_stdout(io.StringIO()):
            return round_module.run_once(config_path)

    def notifications(self):
        path = self.root / ".agent-loop" / "notifications.log"
        return path.read_text().splitlines() if path.exists() else []

    def test_a_fixed_probe_and_a_clean_diff_is_pr_ready_and_notifies_once(self):
        config_path = self.build(AGENT % repr(json.dumps(ANSWER)))
        first = self.run_once(config_path)
        self.assertEqual(first.state, PR_READY)
        self.assertTrue(first.notified)
        self.assertEqual(len(self.notifications()), 1)

        # PR_READY keeps explore/an-item for inspection, and a round on the
        # same item refuses to overwrite it; clear it so this second round
        # reaches the same state and exercises the notification dedup.
        subprocess.run(["git", "branch", "-D", "explore/an-item"], cwd=str(self.root),
                       check=True, stdout=subprocess.DEVNULL)
        second = self.run_once(config_path)
        self.assertEqual(second.state, PR_READY)
        self.assertFalse(second.notified)
        self.assertEqual(len(self.notifications()), 1)

        records = ledger.read(self.root / ".agent-loop" / "ledger.jsonl")
        self.assertEqual([record["state"] for record in records], [PR_READY, PR_READY])
        self.assertNotIn("notified", records[0])
        self.assertEqual(records[0]["item"], "an-item")
        self.assertIsInstance(records[0]["duration_s"], float)
        self.assertIn("git", records[0]["tool_versions"])

    def test_the_worktree_is_gone_after_the_round_and_the_branch_stays_on_pr_ready(self):
        config_path = self.build(AGENT % repr(json.dumps(ANSWER)))
        self.run_once(config_path)
        self.assertEqual(list((self.root / ".agent-loop" / "worktrees").iterdir()), [])
        branches = subprocess.run(["git", "branch", "--format=%(refname:short)"],
                                  cwd=str(self.root), stdout=subprocess.PIPE,
                                  universal_newlines=True).stdout.split()
        self.assertIn("explore/an-item", branches)
        # the worker's uncommitted diff is on the branch, not lost with the tree
        shown = subprocess.run(["git", "show", "--stat", "--format=%s", "explore/an-item"],
                               cwd=str(self.root), stdout=subprocess.PIPE,
                               universal_newlines=True).stdout
        self.assertIn("agent-loop: an-item", shown)
        self.assertIn("project/fixed.txt", shown)
        again = self.run_once(config_path)
        self.assertEqual(again.state, INFRA)
        self.assertIn("explore/an-item", again.reason)

    def test_a_commit_that_fails_is_infra_with_the_cost_and_keeps_the_diff(self):
        # A repository hook (or commit.gpgsign) can refuse the commit that puts
        # the round's diff on explore/<item>.  The round must not then delete
        # the branch and worktree that hold the only copy of that diff, and the
        # worker's cost is spent whether or not the commit succeeded.
        config_path = self.build(AGENT % repr(json.dumps(ANSWER)))
        hook = self.root / ".git" / "hooks" / "pre-commit"
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_text("#!/bin/sh\necho 'hook says no'\nexit 1\n", encoding="utf-8")
        hook.chmod(0o755)
        original = round_module.invoke_with_one_repair
        round_module.invoke_with_one_repair = lambda *a, **k: dataclasses.replace(
            original(*a, **k), cost=1.5)
        try:
            outcome = self.run_once(config_path)
        finally:
            round_module.invoke_with_one_repair = original
        self.assertEqual(outcome.state, INFRA)
        self.assertEqual(outcome.cost, 1.5)
        self.assertIn("hook says no", outcome.reason)
        record = ledger.read(self.root / ".agent-loop" / "ledger.jsonl")[-1]
        self.assertEqual((record["state"], record["cost"]), (INFRA, 1.5))
        branches = subprocess.run(["git", "branch", "--format=%(refname:short)"],
                                  cwd=str(self.root), stdout=subprocess.PIPE,
                                  universal_newlines=True).stdout.split()
        self.assertIn("explore/an-item", branches)
        tree = self.root / ".agent-loop" / "worktrees" / "an-item"
        self.assertTrue((tree / "project" / "fixed.txt").exists())

    def test_a_held_lock_ends_the_round_infra_before_anything_is_picked(self):
        config_path = self.build(AGENT % repr(json.dumps(ANSWER)))
        worktrees = self.root / ".agent-loop" / "worktrees"
        worktrees.mkdir(parents=True, exist_ok=True)
        (worktrees / ".lock").write_text(
            json.dumps({"pid": os.getpid(), "since": "2026-08-29T00:00:00Z"}), encoding="utf-8")
        outcome = self.run_once(config_path)
        self.assertEqual(outcome.state, INFRA)
        self.assertIn("another round holds", outcome.reason)
        self.assertEqual([record["state"] for record in
                          ledger.read(self.root / ".agent-loop" / "ledger.jsonl")], [INFRA])
        self.assertEqual([path.name for path in worktrees.iterdir()], [".lock"])
        self.assertNotIn("explore/an-item", self.branches())

    def test_an_unexpected_failure_still_ends_in_a_state_with_a_line(self):
        config_path = self.build(AGENT % repr(json.dumps(ANSWER)))
        original = round_module.build

        def explode(*args, **kwargs):
            raise RuntimeError("something nobody predicted")

        round_module.build = explode
        try:
            outcome = self.run_once(config_path)
        finally:
            round_module.build = original
        self.assertEqual(outcome.state, INFRA)
        self.assertIn("something nobody predicted", outcome.reason)
        self.assertEqual(len(self.notifications()), 1)
        records = ledger.read(self.root / ".agent-loop" / "ledger.jsonl")
        self.assertEqual([record["state"] for record in records], [INFRA])
        self.assertEqual(list((self.root / ".agent-loop" / "worktrees").iterdir()), [])

    def branches(self):
        return subprocess.run(["git", "branch", "--format=%(refname:short)"],
                              cwd=str(self.root), stdout=subprocess.PIPE,
                              universal_newlines=True).stdout.split()

    def test_a_blocked_verify_keeps_the_branch_so_the_diff_can_be_read(self):
        # The worker answered `done` and the cost-class command said no: the
        # diff is what has to be judged, so it survives the round.
        config_path = self.build(AGENT % repr(json.dumps(ANSWER)),
                                 config=CONFIG.replace('command: "true"', 'command: "false"'))
        outcome = self.run_once(config_path)
        self.assertEqual(outcome.state, BLOCKED)
        self.assertIn("verify command", outcome.reason)
        self.assertIn("explore/an-item", self.branches())
        shown = subprocess.run(["git", "show", "--stat", "--format=%s", "explore/an-item"],
                               cwd=str(self.root), stdout=subprocess.PIPE,
                               universal_newlines=True).stdout
        self.assertIn("agent-loop: an-item", shown)
        self.assertIn("project/fixed.txt", shown)
        self.assertEqual(list((self.root / ".agent-loop" / "worktrees").iterdir()), [])

    def test_a_pr_ready_round_publishes_before_cleanup_and_records_the_url(self):
        # The push has to happen while explore/an-item still exists, and once
        # origin holds it the local branch is no longer the only copy of the
        # diff - so cleanup takes it, and a later round on the item can run.
        config_path = self.build(AGENT % repr(json.dumps(ANSWER)), config=CONFIG + "scm: github\n")
        origin = origin_for(self.root)
        fake_gh(self.root, [{"match": ["list"], "out": "[]"},
                            {"match": ["create"], "out": "https://github.com/o/r/pull/7\n"}])
        outcome = self.run_once(config_path)
        self.assertEqual(outcome.state, PR_READY)
        self.assertEqual(outcome.pr_url, "https://github.com/o/r/pull/7")
        record = ledger.read(self.root / ".agent-loop" / "ledger.jsonl")[-1]
        self.assertEqual(record["pr_url"], "https://github.com/o/r/pull/7")
        self.assertIn("https://github.com/o/r/pull/7", self.notifications()[0])
        self.assertIn("explore/an-item", self.remote_branches(origin))
        self.assertNotIn("explore/an-item", self.branches())
        body = [call for call in gh_calls(self.root) if call["argv"][1] == "create"][0]["stdin"]
        self.assertIn("project/fixed.txt", body)  # the diff explanation is real

    def remote_branches(self, origin):
        return subprocess.run(["git", "branch", "--format=%(refname:short)"],
                              cwd=str(origin), stdout=subprocess.PIPE,
                              universal_newlines=True).stdout.split()

    def test_a_tool_version_change_is_a_warning_on_the_round_s_line(self):
        blocked = dict(ANSWER, status="blocked", diff_applied=False, reason="not mine to fix")
        config_path = self.build(BLOCKING_AGENT % repr(json.dumps(blocked)))
        self.run_once(config_path)
        original = round_module.ledger.tool_versions
        round_module.ledger.tool_versions = lambda adapters=(): {"git": "git version 99.0"}
        try:
            second = self.run_once(config_path)
        finally:
            round_module.ledger.tool_versions = original
        self.assertEqual(second.state, NO_ITEM)  # the drift changes no state
        records = ledger.read(self.root / ".agent-loop" / "ledger.jsonl")
        self.assertIsNone(records[0]["warning"])
        self.assertIn("git version", records[1]["warning"])
        self.assertIn("-> git version 99.0", records[1]["warning"])
        self.assertEqual(len(self.notifications()), 2)  # and notifies nothing extra

    def test_a_worker_that_blocks_is_blocked_then_skipped(self):
        blocked = dict(ANSWER, status="blocked", diff_applied=False,
                       reason="the design doc forbids it")
        config_path = self.build(BLOCKING_AGENT % repr(json.dumps(blocked)))
        first = self.run_once(config_path)
        self.assertEqual(first.state, BLOCKED)
        self.assertIn("the design doc forbids it", first.reason)
        # nothing was applied, so there is no diff to keep and the branch goes
        self.assertNotIn("explore/an-item", self.branches())

        second = self.run_once(config_path)
        self.assertEqual(second.state, NO_ITEM)
        self.assertEqual(len(self.notifications()), 2)


if __name__ == "__main__":
    unittest.main()
