"""One whole round in mode ``once``, with a fake shell agent: the terminal state,
the ledger line, and the notification deduplicated by (item, state, sha).
"""

import contextlib
import io
import json
import unittest

from agent_loop import ledger, round as round_module
from agent_loop.states import BLOCKED, INFRA, NO_ITEM, PR_READY

from support import cleanup, git_init, make_repo, write_script

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
    def build(self, agent_body):
        self.root = make_repo(config="branch: main\n", backlog=BACKLOG)
        script = write_script(self.root, "agent.py", agent_body)
        (self.root / ".agent-loop" / "config.yaml").write_text(
            CONFIG % script, encoding="utf-8")
        git_init(self.root)
        return self.root / ".agent-loop" / "config.yaml"

    def tearDown(self):
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

    def test_the_worktree_is_gone_after_the_round(self):
        config_path = self.build(AGENT % repr(json.dumps(ANSWER)))
        self.run_once(config_path)
        self.assertEqual(list((self.root / ".agent-loop" / "worktrees").iterdir()), [])

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

    def test_a_worker_that_blocks_is_blocked_then_skipped(self):
        blocked = dict(ANSWER, status="blocked", diff_applied=False,
                       reason="the design doc forbids it")
        config_path = self.build(BLOCKING_AGENT % repr(json.dumps(blocked)))
        first = self.run_once(config_path)
        self.assertEqual(first.state, BLOCKED)
        self.assertIn("the design doc forbids it", first.reason)

        second = self.run_once(config_path)
        self.assertEqual(second.state, NO_ITEM)
        self.assertEqual(len(self.notifications()), 2)


if __name__ == "__main__":
    unittest.main()
