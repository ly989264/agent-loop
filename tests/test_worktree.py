import subprocess
import unittest

from agent_loop.errors import InfraError
from agent_loop.worktree import Workspace, head_sha, remove, workspace

from support import cleanup, git_init, make_repo


class WorktreeTest(unittest.TestCase):
    def setUp(self):
        self.root = make_repo()
        git_init(self.root)
        self.worktree_root = self.root / ".agent-loop" / "worktrees"

    def tearDown(self):
        cleanup(self.root)

    def branches(self):
        result = subprocess.run(["git", "branch", "--format=%(refname:short)"],
                                cwd=str(self.root), stdout=subprocess.PIPE,
                                universal_newlines=True)
        return result.stdout.split()

    def test_a_worktree_is_created_on_its_own_branch_and_removed_on_success(self):
        with workspace(self.root, "main", self.worktree_root, "an-item") as space:
            self.assertTrue((space.tree / ".agent-loop" / "backlog.yaml").exists())
            self.assertTrue(space.temp_dir.exists())
            self.assertEqual(space.branch, "explore/an-item")
            self.assertIn("explore/an-item", self.branches())
            tree, temp_dir = space.tree, space.temp_dir
        self.assertFalse(tree.exists())
        self.assertFalse(temp_dir.exists())
        self.assertNotIn("explore/an-item", self.branches())

    def test_the_configured_branch_may_be_checked_out_elsewhere(self):
        # `git worktree add <tree> <branch>` refuses a branch that is already
        # checked out, and a consumer's configured branch always is.
        with workspace(self.root, "main", self.worktree_root, "an-item"):
            pass
        with workspace(self.root, "main", self.worktree_root, "an-item") as space:
            self.assertEqual(space.branch, "explore/an-item")

    def test_the_worktree_is_removed_when_the_round_fails(self):
        with self.assertRaises(RuntimeError):
            with workspace(self.root, "main", self.worktree_root, "an-item") as space:
                tree, temp_dir = space.tree, space.temp_dir
                (tree / "scratch").write_text("dirty", encoding="utf-8")
                raise RuntimeError("the worker died mid-round")
        self.assertFalse(tree.exists())
        self.assertFalse(temp_dir.exists())
        self.assertEqual(list(self.worktree_root.iterdir()), [])

    def test_a_leftover_worktree_is_reported_not_reused(self):
        (self.worktree_root / "an-item").mkdir(parents=True)
        with self.assertRaises(InfraError):
            with workspace(self.root, "main", self.worktree_root, "an-item"):
                pass

    def test_cleanup_refuses_a_path_outside_the_worktree_root(self):
        outside = self.root / "project"
        remove(self.root, Workspace(tree=outside, temp_dir=self.root / "absent"), self.worktree_root)
        self.assertTrue(outside.exists())

    def test_an_unknown_branch_is_infra(self):
        with self.assertRaises(InfraError):
            head_sha(self.root, "no-such-branch")


if __name__ == "__main__":
    unittest.main()
