"""Drills: the whole CLI path, end to end, for the shapes §4 Stage 4a names.

Each drill drives ``agent_loop.cli.main`` against a throwaway consumer
repository with a fake agent behind the ``shell`` adapter, and a fake ``gh``
where a forge is needed.  They are not unit tests of one function: where a unit
test already covers a behaviour, the drill runs the whole path once and asserts
what an operator would see - the exit code, the ledger, the notification log,
and what is left on disk afterwards.

Every drill here was watched to fail under a one-line mutation of the kernel
before it was committed; the mutation is named in its docstring.
"""

import contextlib
import io
import json
import os
import signal
import subprocess
import time
import unittest

from agent_loop import cli, ledger
from agent_loop.states import BLOCKED, INFRA, NO_ITEM, PR_READY

from support import cleanup, fake_gh, git_init, make_repo, write_script


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
    - shell:__AGENT__
caps:
  poll_s: 1
  idle_s: 1
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


ONE_ITEM = """
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


TWO_ITEMS = ONE_ITEM + """\
  - id: another-item
    group: g
    statement: a second open item
    cost_class: hermetic
    selectable: true
    sites: []
    design_doc: "docs/design.md 2"
    probe: "test -f other.txt"
    proof: "the probe exits 0 once the other file is there"
"""


DONE = {
    "diff_applied": True,
    "test_path": "project/tests/test_thing.py",
    "mutation_evidence": {"reverted_command": "git stash && pytest",
                          "observed_failure_line": "E assert"},
    "status": "done",
    "reason": "",
}


BLOCKED_ANSWER = dict(DONE, status="blocked", diff_applied=False,
                      reason="the design doc forbids it")


FIXING_AGENT = """\
#!/usr/bin/env python3
import sys
sys.stdin.read()
open("project/fixed.txt", "w").write("fixed\\n")
print(__ANSWER__)
"""


TALKING_AGENT = """\
#!/usr/bin/env python3
import sys
sys.stdin.read()
print(__ANSWER__)
"""

# Records every call, then answers something that is not a JSON object at all.


GARBLED_AGENT = """\
#!/usr/bin/env python3
import sys
bundle = sys.stdin.read()
with open("__ROOT__/agent_calls.txt", "a") as handle:
    handle.write("%d\\n" % len(bundle))
print("I have applied the fix, trust me.")
"""

# Writes its own pid and a grandchild's, then outlives caps.worker.wall_s.


def answering(template, answer):
    return template.replace("__ANSWER__", repr(json.dumps(answer)))


class DrillCase(unittest.TestCase):
    """A temp consumer repository, a fake agent on it, and the real CLI."""

    def setUp(self):
        self.path = os.environ["PATH"]
        self.root = None
        self.stray_pids = []

    def tearDown(self):
        os.environ["PATH"] = self.path
        for pid in self.stray_pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        if self.root is not None:
            cleanup(self.root)

    def consumer(self, agent_body, config=CONFIG, backlog=ONE_ITEM):
        """A repository, a fake agent in it, and the config the CLI is given.

        ``__ROOT__`` in the agent's body becomes the repository's path, so an
        agent can write where the round's worktree cleanup will not reach.
        """
        self.root = make_repo(config="branch: main\n", backlog=backlog)
        agent = write_script(self.root, "agent.py", agent_body.replace("__ROOT__", str(self.root)))
        self.config_path = self.root / ".agent-loop" / "config.yaml"
        self.config_path.write_text(config.replace("__AGENT__", str(agent)), encoding="utf-8")
        git_init(self.root)
        return self.config_path

    def rewrite_agent(self, agent_body):
        write_script(self.root, "agent.py", agent_body)

    def run_cli(self, argv):
        with contextlib.redirect_stdout(io.StringIO()) as captured:
            code = cli.main(argv)
        return code, captured.getvalue()

    def once(self):
        return self.run_cli(["run", "--config", str(self.config_path), "--mode", "once"])

    def records(self):
        return ledger.read(self.root / ".agent-loop" / "ledger.jsonl")

    def states(self):
        return [record["state"] for record in self.records()]

    def notifications(self):
        path = self.root / ".agent-loop" / "notifications.log"
        return path.read_text().splitlines() if path.exists() else []

    def git(self, *argv):
        return subprocess.run(["git"] + list(argv), cwd=str(self.root),
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              universal_newlines=True).stdout

    def branches(self):
        return self.git("branch", "--format=%(refname:short)").split()

    def worktree_root_entries(self):
        root = self.root / ".agent-loop" / "worktrees"
        return sorted(path.name for path in root.iterdir()) if root.exists() else []


class RepeatDispatchDrill(DrillCase):
    """Drill 1 - running `run --mode once` again on an unchanged tree after a
    PR_READY dispatches nothing: no second notification for the same
    (item, state, sha), no second commit, no second branch.

    Watched to fail with `ledger.already_notified` returning False.
    """

    def test_a_second_and_third_run_on_an_unchanged_tree_dispatch_nothing(self):
        self.consumer(answering(FIXING_AGENT, DONE))
        code, _ = self.once()
        self.assertEqual((code, self.states()), (0, [PR_READY]))
        self.assertEqual(len(self.notifications()), 1)
        # local-only opens no pull request, so the diff stays on explore/an-item
        self.assertEqual([name for name in self.branches() if name.startswith("explore/")],
                         ["explore/an-item"])
        commits = self.git("rev-list", "--count", "explore/an-item").strip()

        second_code, _ = self.once()
        third_code, _ = self.once()
        # the branch this item's diff is already on is what stops the repeat
        self.assertEqual((second_code, third_code), (2, 2))
        self.assertEqual(self.states(), [PR_READY, INFRA, INFRA])
        self.assertIn("explore/an-item", self.records()[1]["reason"])

        # one INFRA notification for the pair: (item, state, sha) is the same
        self.assertEqual(len(self.notifications()), 2)
        self.assertFalse(self.records()[2].get("notified_at"))
        self.assertEqual([name for name in self.branches() if name.startswith("explore/")],
                         ["explore/an-item"])
        self.assertEqual(self.git("rev-list", "--count", "explore/an-item").strip(), commits)
        self.assertEqual(self.worktree_root_entries(), [])


class MalformedWorkerDrill(DrillCase):
    """Drill 2 - a worker that answers garbage gets exactly one repair and then
    the round is INFRA, with nothing left behind.

    Watched to fail with `invoke_with_one_repair` returning `first` before the
    repair round-trip: the adapter is then called once, not twice.
    """

    def test_garbage_twice_is_one_repair_then_infra_and_nothing_is_left(self):
        self.consumer(GARBLED_AGENT)
        calls = self.root / "agent_calls.txt"

        code, _ = self.once()
        self.assertEqual(code, 2)
        self.assertEqual(self.states(), [INFRA])
        self.assertIn("worker returned malformed", self.records()[0]["reason"])
        # one repair, and only one: the adapter ran exactly twice
        self.assertEqual(len(calls.read_text().splitlines()), 2)
        self.assertEqual(len(self.notifications()), 1)
        self.assertEqual(self.worktree_root_entries(), [])
        self.assertEqual([name for name in self.branches() if name.startswith("explore/")], [])


class BlockedItemDrill(DrillCase):
    """Drill 3 - a BLOCKED item notifies once and is skipped at that sha; the
    round after it picks the next failing item, then NO_ITEM.  Committing a
    backlog edit moves the sha and re-admits it; touching the file does not.

    Watched to fail with `pick.run_probes` ignoring its skip set: the second
    run then picks the blocked item again instead of `another-item`.
    """

    def test_a_blocked_item_notifies_once_is_skipped_and_returns_when_the_sha_moves(self):
        self.consumer(answering(TALKING_AGENT, BLOCKED_ANSWER), backlog=TWO_ITEMS)
        first_sha = self.git("rev-parse", "main").strip()

        code, _ = self.once()
        self.assertEqual((code, self.records()[0]["item"]), (1, "an-item"))
        code, _ = self.once()
        self.assertEqual((code, self.records()[1]["item"]), (1, "another-item"))
        code, _ = self.once()
        self.assertEqual((code, self.records()[2]["item"]), (0, None))
        self.assertEqual(self.states(), [BLOCKED, BLOCKED, NO_ITEM])
        self.assertEqual(len(self.notifications()), 3)

        # an edit that only moves the mtime is a continuous-mode trigger, not a
        # re-admission: `pick` skips what is BLOCKED at *this sha*
        backlog = self.root / ".agent-loop" / "backlog.yaml"
        backlog.write_text(backlog.read_text() + "# touched\n", encoding="utf-8")
        code, _ = self.once()
        self.assertEqual((code, self.states()[-1]), (0, NO_ITEM))

        self.git("add", "-A")
        self.git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", "edit the backlog")
        self.assertNotEqual(self.git("rev-parse", "main").strip(), first_sha)
        code, _ = self.once()
        self.assertEqual((code, self.records()[-1]["item"]), (1, "an-item"))
        self.assertEqual(self.states()[-1], BLOCKED)
        # the fourth round said nothing new: (None, NO_ITEM, sha) was already a line
        self.assertEqual(len(self.notifications()), 4)
        # and the blocked round at the first sha happened once, not twice
        blocked_at_first = [record for record in self.records()
                            if record["state"] == BLOCKED and record["sha"] == first_sha
                            and record["item"] == "an-item"]
        self.assertEqual(len(blocked_at_first), 1)


if __name__ == "__main__":
    unittest.main()
