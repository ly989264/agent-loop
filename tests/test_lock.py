"""The round lock: one round at a time under one worktree root."""

import json
import os
import subprocess
import unittest

from agent_loop import lock
from agent_loop.errors import InfraError

from support import cleanup, git_init, make_repo


class LockTest(unittest.TestCase):
    def setUp(self):
        self.root = make_repo()
        git_init(self.root)
        self.worktree_root = self.root / ".agent-loop" / "worktrees"
        self.path = self.worktree_root / lock.LOCK_NAME

    def tearDown(self):
        cleanup(self.root)

    def hold(self):
        return lock.hold(self.root, self.worktree_root)

    def test_the_lock_is_taken_and_released_on_every_exit_path(self):
        with self.hold() as path:
            self.assertTrue(path.exists())
            self.assertEqual(json.loads(path.read_text())["pid"], os.getpid())
        self.assertFalse(self.path.exists())
        with self.assertRaises(RuntimeError):
            with self.hold():
                raise RuntimeError("the round died mid-flight")
        self.assertFalse(self.path.exists())

    def test_a_second_round_refuses_while_a_live_round_holds_it(self):
        with self.hold():
            with self.assertRaises(InfraError) as caught:
                with self.hold():
                    self.fail("the second round must not run")
            # the refused round leaves the holder's lock alone
            self.assertTrue(self.path.exists())
        self.assertIn("another round holds", str(caught.exception))
        self.assertIn(str(os.getpid()), str(caught.exception))
        self.assertFalse(self.path.exists())

    def test_a_lock_left_by_a_dead_round_is_taken_over(self):
        self.worktree_root.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"pid": 2 ** 22, "since": "2026-08-29T00:00:00Z"}),
                             encoding="utf-8")
        with self.hold() as path:
            self.assertEqual(json.loads(path.read_text())["pid"], os.getpid())

    def test_a_foreign_worktree_under_the_root_refuses_the_round(self):
        subprocess.run(["git", "worktree", "add", "-b", "someone-else",
                        str(self.worktree_root / "by-hand"), "main"],
                       cwd=str(self.root), stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, check=True)
        with self.assertRaises(InfraError) as caught:
            with self.hold():
                self.fail("the round must not run beside a foreign worktree")
        self.assertIn("by-hand", str(caught.exception))
        self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()
