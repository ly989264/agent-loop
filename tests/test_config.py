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

    def test_l1_and_l2_are_accepted_and_l3_is_not(self):
        self.assertEqual(self.load(CONFIG).level("hermetic"), "L1")
        self.assertEqual(
            self.load(CONFIG.replace("hermetic: L1", "hermetic: L2")).level("hermetic"), "L2")
        # a cost class the config says nothing about is L1, not "anything goes"
        self.assertEqual(self.load(CONFIG).level("docker-exact-50"), "L1")
        with self.assertRaises(ConfigError) as caught:
            self.load(CONFIG.replace("hermetic: L1", "hermetic: L3"))
        self.assertIn("only L1 and L2", str(caught.exception))

    def test_l3_is_the_planners_level_and_no_cost_class_may_take_it(self):
        # `levels` is keyed by cost class; `planner` is the one reserved key,
        # because L3 is a role's autonomy and not a class of work.
        self.assertEqual(self.load(CONFIG).level("planner"), "L1")
        self.assertEqual(
            self.load(CONFIG.replace("hermetic: L1", "hermetic: L1\n  planner: L3"))
                .level("planner"), "L3")
        with self.assertRaises(ConfigError) as caught:
            self.load(CONFIG.replace("hermetic: L1", "hermetic: L1\n  planner: L2"))
        self.assertIn("planner is not a cost class", str(caught.exception).replace("levels.", ""))

    def test_plan_sources_defaults_to_nothing_and_must_be_a_list_of_globs(self):
        self.assertEqual(self.load(CONFIG).plan_sources, ())
        self.assertEqual(
            self.load(CONFIG + "\nplan_sources:\n  - docs/*.md\n  - README.md\n").plan_sources,
            ("docs/*.md", "README.md"))
        with self.assertRaises(ConfigError) as caught:
            self.load(CONFIG + "\nplan_sources: docs/*.md\n")
        self.assertIn("plan_sources", str(caught.exception))

    def test_the_publisher_defaults_to_local_only_and_an_unknown_one_is_refused(self):
        self.assertEqual(self.load(CONFIG).scm, "local-only")
        self.assertEqual(self.load(CONFIG + "\nscm: github\n").scm, "github")
        with self.assertRaises(ConfigError) as caught:
            self.load(CONFIG + "\nscm: gitlab\n")
        self.assertIn("scm must be one of", str(caught.exception))

    def test_a_file_notify_target_needs_a_path(self):
        with self.assertRaises(ConfigError):
            self.load(CONFIG.replace("  - stdout", "  - file"))

    def test_continuous_caps_default_and_are_overridable(self):
        config = self.load()
        self.assertEqual(config.open_prs, 3)
        self.assertEqual(config.non_progress_rounds, 5)
        self.assertEqual(config.poll_s, 30)
        self.assertEqual(config.idle_s, 900)
        self.assertEqual(config.round_wall_s, 3600)
        overridden = self.load(CONFIG.replace(
            "caps:\n  worker:",
            "caps:\n  open_prs: 1\n  non_progress_rounds: 2\n  poll_s: 5\n"
            "  idle_s: 10\n  round_wall_s: 120\n  worker:"))
        self.assertEqual(
            (overridden.open_prs, overridden.non_progress_rounds, overridden.poll_s,
             overridden.idle_s, overridden.round_wall_s),
            (1, 2, 5, 10, 120))
        # every role-keyed budget is untouched by the new sub-keys
        self.assertEqual(overridden.budget("worker").wall_s, 60)

    def test_a_non_positive_continuous_cap_is_refused(self):
        with self.assertRaises(ConfigError) as caught:
            self.load(CONFIG.replace("caps:\n  worker:", "caps:\n  open_prs: 0\n  worker:"))
        self.assertIn("open_prs", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
