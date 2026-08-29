import unittest

from agent_loop import config as config_module
from agent_loop.errors import ConfigError

from support import CONFIG, cleanup, make_repo


class ConfigTest(unittest.TestCase):
    def tearDown(self):
        cleanup(self.root)

    def load(self, text=CONFIG):
        self.root = make_repo(config=text)
        return config_module.load(self.root / ".agent-loop" / "config.yaml")

    def test_every_key_is_read(self):
        config = self.load()
        self.assertEqual(config.branch, "main")
        self.assertEqual(config.root, self.root.resolve())
        self.assertEqual(config.backlog, self.root / ".agent-loop/backlog.yaml")
        self.assertEqual(config.ledger, self.root / ".agent-loop/ledger.jsonl")
        self.assertEqual(config.protected_paths, ("project/schemas", "project/catalog.json"))
        self.assertEqual(config.verify_for("hermetic").command, "true")
        self.assertEqual(config.verify_for("hermetic").cwd, "project")
        self.assertEqual(config.budget("worker").wall_s, 60)
        self.assertEqual(config.notify[0].kind, "stdout")
        self.assertEqual(config.levels, {"hermetic": "L1"})

    def test_a_list_of_agents_is_an_escalation_ladder(self):
        config = self.load(CONFIG.replace(
            "    - shell:/bin/echo", "    - claude-code:sonnet-5\n    - claude-code:opus-5"))
        ladder = config.ladder("worker")
        self.assertEqual([str(rung) for rung in ladder],
                         ["claude-code:sonnet-5", "claude-code:opus-5"])
        self.assertEqual(ladder[0].adapter, "claude-code")
        self.assertEqual(ladder[0].model, "sonnet-5")

    def test_an_unknown_key_is_refused(self):
        with self.assertRaises(ConfigError) as caught:
            self.load(CONFIG + "\nopen_pr_cap: 3\n")
        self.assertIn("open_pr_cap", str(caught.exception))

    def test_a_missing_key_is_refused(self):
        with self.assertRaises(ConfigError) as caught:
            self.load(CONFIG.replace("branch: main", ""))
        self.assertIn("branch", str(caught.exception))

    def test_only_l1_is_accepted(self):
        with self.assertRaises(ConfigError) as caught:
            self.load(CONFIG.replace("hermetic: L1", "hermetic: L2"))
        self.assertIn("only L1", str(caught.exception))

    def test_a_file_notify_target_needs_a_path(self):
        with self.assertRaises(ConfigError):
            self.load(CONFIG.replace("  - stdout", "  - file"))


if __name__ == "__main__":
    unittest.main()
