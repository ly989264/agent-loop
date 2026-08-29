"""The CLI: run's four modes, status/pause/resume/metrics."""

import contextlib
import io
import unittest

from agent_loop import cli, config as config_module, ledger, modes
from agent_loop.states import NO_ITEM

from support import cleanup, git_init, make_repo

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
    - shell:/bin/echo
caps:
  worker:
    wall_s: 60
    silence_s: 30
    max_tokens: 1000
  poll_s: 1
  idle_s: 1
notify:
  - target: file
    path: .agent-loop/notifications.log
levels:
  hermetic: L1
"""

# every probe already passes: `once`/`schedule` both end NO_ITEM, no agent spawned
BACKLOG = """
items:
  - id: an-item
    group: g
    statement: nothing to do
    cost_class: hermetic
    selectable: true
    sites: []
    design_doc: ""
    probe: "exit 0"
"""


class CliTestBase(unittest.TestCase):
    def setUp(self):
        self.root = make_repo(config=CONFIG, backlog=BACKLOG)
        git_init(self.root)
        self.config_path = self.root / ".agent-loop" / "config.yaml"

    def tearDown(self):
        cleanup(self.root)

    def run_cli(self, argv):
        with contextlib.redirect_stdout(io.StringIO()) as captured:
            code = cli.main(argv)
        return code, captured.getvalue()


class RunModesTest(CliTestBase):
    def test_once_and_schedule_both_reach_the_same_no_item_round(self):
        code, _ = self.run_cli(["run", "--config", str(self.config_path), "--mode", "once"])
        self.assertEqual(code, 0)
        code, _ = self.run_cli(["run", "--config", str(self.config_path), "--mode", "schedule"])
        self.assertEqual(code, 0)
        records = ledger.read(self.root / ".agent-loop" / "ledger.jsonl")
        self.assertEqual([record["state"] for record in records], [NO_ITEM, NO_ITEM])

    def test_until_with_no_stop_flag_is_refused_before_any_round_runs(self):
        code, output = self.run_cli(["run", "--config", str(self.config_path), "--mode", "until"])
        self.assertEqual(code, 2)
        self.assertIn("--until-prs", output)
        self.assertEqual(ledger.read(self.root / ".agent-loop" / "ledger.jsonl"), [])

    def test_until_stops_itself_on_hours(self):
        code, _ = self.run_cli(
            ["run", "--config", str(self.config_path), "--mode", "until",
             "--until-hours", "0.0003"])
        self.assertEqual(code, 0)
        records = ledger.read(self.root / ".agent-loop" / "ledger.jsonl")
        self.assertTrue(records)


class StatusPauseResumeTest(CliTestBase):
    def test_status_reports_not_paused_then_paused(self):
        code, output = self.run_cli(["status", "--config", str(self.config_path)])
        self.assertEqual(code, 0)
        self.assertIn("paused          no", output)

        code, output = self.run_cli(["pause", "--config", str(self.config_path)])
        self.assertEqual(code, 0)
        self.assertIn("paused", output)
        config = config_module.load(self.config_path)
        self.assertTrue(modes.paused(config.worktree_root))

        code, output = self.run_cli(["status", "--config", str(self.config_path)])
        self.assertIn("paused          yes", output)

        code, output = self.run_cli(["resume", "--config", str(self.config_path)])
        self.assertEqual(code, 0)
        self.assertIn("resumed", output)
        self.assertFalse(modes.paused(config.worktree_root))

    def test_a_bad_config_is_reported_not_raised_by_every_command(self):
        bad = self.root / "missing.yaml"
        for argv in (["status", "--config", str(bad)], ["pause", "--config", str(bad)],
                    ["resume", "--config", str(bad)], ["metrics", "--config", str(bad)]):
            with self.subTest(argv=argv):
                code, output = self.run_cli(argv)
                self.assertEqual(code, 2)
                self.assertIn("config error", output)


class MetricsCommandTest(CliTestBase):
    def test_metrics_prints_the_report_from_the_ledger_alone(self):
        config = config_module.load(self.config_path)
        ledger.append(config.ledger, {
            "ts": "2026-08-29T00:00:00Z", "item": None, "sha": "s", "state": NO_ITEM,
            "duration_s": 1.0,
        })
        code, output = self.run_cli(["metrics", "--config", str(self.config_path)])
        self.assertEqual(code, 0)
        self.assertIn("rounds by state", output)
        self.assertIn("NO_ITEM=1", output)
        self.assertIn("PRs", output)


if __name__ == "__main__":
    unittest.main()
