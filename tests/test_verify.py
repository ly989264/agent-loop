"""The verify step, and in particular what counts as touching a protected path."""

import os
import subprocess
import unittest

from agent_loop import config as config_module
from agent_loop.backlog import Item
from agent_loop.verify import verify

from support import CONFIG, cleanup, git_init, make_repo


def item(probe="true"):
    return Item(id="an-item", group="g", statement="s", cost_class="hermetic",
                selectable=True, sites=(), design_doc="", probe=probe)


class VerifyTest(unittest.TestCase):
    def setUp(self):
        self.root = make_repo()
        git_init(self.root)
        self.config = config_module.load(self.root / ".agent-loop" / "config.yaml")
        self.base_sha = self.git("rev-parse", "HEAD").strip()

    def tearDown(self):
        cleanup(self.root)

    def git(self, *argv):
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
                   GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
        return subprocess.run(["git"] + list(argv), cwd=str(self.root), env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              universal_newlines=True).stdout

    def test_a_clean_round_passes(self):
        outcome = verify(self.config, item(), self.root, self.base_sha)
        self.assertTrue(outcome.ok, outcome.reason)

    def test_a_probe_that_still_fails_is_blocked(self):
        outcome = verify(self.config, item(probe="exit 7"), self.root, self.base_sha)
        self.assertFalse(outcome.ok)
        self.assertIn("probe still fails", outcome.reason)

    def test_a_modified_protected_file_is_blocked(self):
        (self.root / "project" / "catalog.json").write_text("{}", encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-m", "add catalog")
        (self.root / "project" / "catalog.json").write_text("{\"x\": 1}", encoding="utf-8")
        outcome = verify(self.config, item(), self.root, self.git("rev-parse", "HEAD").strip())
        self.assertFalse(outcome.ok)
        self.assertIn("project/catalog.json", outcome.reason)

    def test_a_newly_created_protected_file_is_blocked(self):
        # `git diff --name-only` alone never sees this one.
        (self.root / "project" / "schemas").mkdir()
        (self.root / "project" / "schemas" / "new.json").write_text("{}", encoding="utf-8")
        outcome = verify(self.config, item(), self.root, self.base_sha)
        self.assertFalse(outcome.ok)
        self.assertIn("project/schemas/new.json", outcome.reason)

    def test_a_committed_protected_change_is_blocked(self):
        (self.root / "project" / "catalog.json").write_text("{}", encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-m", "the worker committed its work")
        outcome = verify(self.config, item(), self.root, self.base_sha)
        self.assertFalse(outcome.ok)
        self.assertIn("project/catalog.json", outcome.reason)

    def test_an_unprotected_change_passes(self):
        (self.root / "project" / "thing.py").write_text("x = 1\n", encoding="utf-8")
        outcome = verify(self.config, item(), self.root, self.base_sha)
        self.assertTrue(outcome.ok, outcome.reason)


if __name__ == "__main__":
    unittest.main()
