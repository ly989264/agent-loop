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

    def test_a_worktree_is_created_at_the_branch_and_removed_on_success(self):
        with workspace(self.root, "main", self.worktree_root, "an-item") as space:
            self.assertTrue((space.tree / ".agent-loop" / "backlog.yaml").exists())
            self.assertTrue(space.temp_dir.exists())
            tree, temp_dir = space.tree, space.temp_dir
        self.assertFalse(tree.exists())
        self.assertFalse(temp_dir.exists())

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
