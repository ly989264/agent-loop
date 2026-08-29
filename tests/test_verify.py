"""The verify step, and in particular what counts as touching a protected path."""

import os
import subprocess
import unittest

from agent_loop import config as config_module
from agent_loop.backlog import Item
from agent_loop.verify import failing_lines, verify

from support import CONFIG, cleanup, git_init, make_repo, write_script

# What `./gate suite product.unit` prints: a row per check, then a summary.
# The last 800 characters of this are the six passing rows and `Status: FAIL`.
GATE_OUTPUT = "\n".join(
    ["gate run gate-20260829T095947Z-2f7ea13b", ""]
    + ["product.unit.check_%02d%sPASS  0.4s" % (n, " " * 24) for n in range(1, 13)]
    + ["product.unit.docker_runtime_contract    FAIL  3.1s"]
    + ["product.unit.check_%02d%sPASS  0.4s" % (n, " " * 24) for n in range(13, 25)]
    + ["", "25 checks, 24/25 passed", "Status: FAIL", ""]
)


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

    def test_a_failing_verify_command_names_the_check_that_failed(self):
        script = write_script(self.root, "suite.sh",
                              "#!/bin/sh\ncat ../suite.out\nexit 1\n")
        (self.root / "suite.out").write_text(GATE_OUTPUT, encoding="utf-8")
        (self.root / ".agent-loop" / "config.yaml").write_text(
            CONFIG.replace('command: "true"', 'command: "../%s"' % script.name),
            encoding="utf-8")
        loaded = config_module.load(self.root / ".agent-loop" / "config.yaml")
        outcome = verify(loaded, item(), self.root, self.base_sha)
        self.assertFalse(outcome.ok)
        self.assertIn("product.unit.docker_runtime_contract", outcome.reason)
        self.assertIn("Status: FAIL", outcome.reason)
        self.assertNotIn("PASS", outcome.reason)

    def test_failing_lines_are_bounded_and_fall_back_to_the_tail(self):
        many = "\n".join("check_%03d FAIL" % n for n in range(100))
        kept = failing_lines(many).splitlines()
        self.assertEqual(len(kept), 41)
        self.assertIn("60 earlier failing lines", kept[0])
        self.assertEqual(kept[-1], "check_099 FAIL")
        self.assertEqual(failing_lines("nothing marked here"), "nothing marked here")

    def test_an_unprotected_change_passes(self):
        (self.root / "project" / "thing.py").write_text("x = 1\n", encoding="utf-8")
        outcome = verify(self.config, item(), self.root, self.base_sha)
        self.assertTrue(outcome.ok, outcome.reason)


if __name__ == "__main__":
    unittest.main()
