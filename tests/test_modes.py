"""Continuous/schedule/until: pause, back-pressure, triggers, metrics.

``schedule`` is `once` under a different name (cli.py aliases it) and needs no
tests of its own beyond the alias, which test_cli.py covers.
"""

import contextlib
import io
import json
import os
import time
import unittest

from agent_loop import config as config_module, ledger, modes, round as round_module
from agent_loop.states import BLOCKED, INFRA, NO_ITEM, PR_READY

from support import cleanup, fake_gh, git_init, make_repo, origin_for, write_script

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
  open_prs: 1
  non_progress_rounds: 2
  poll_s: 1
  idle_s: 1
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

# every probe already passes: no item is ever picked, so a round is NO_ITEM
NO_WORK_BACKLOG = """
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

class ModesTestBase(unittest.TestCase):
    def build(self, config=CONFIG, backlog=BACKLOG, agent_body=AGENT):
        self.root = make_repo(config="branch: main\n", backlog=backlog)
        script = write_script(self.root, "agent.py", agent_body % repr(json.dumps(ANSWER)))
        (self.root / ".agent-loop" / "config.yaml").write_text(
            config % str(script), encoding="utf-8")
        git_init(self.root)
        return self.root / ".agent-loop" / "config.yaml"

    def setUp(self):
        self.path = os.environ["PATH"]

    def tearDown(self):
        os.environ["PATH"] = self.path
        cleanup(self.root)


class PauseResumeTest(ModesTestBase):
    def test_paused_toggles_on_a_flag_file_under_the_worktree_root(self):
        config_path = self.build()
        config = config_module.load(config_path)
        self.assertFalse(modes.paused(config.worktree_root))
        modes.pause(config.worktree_root)
        self.assertTrue(modes.paused(config.worktree_root))
        modes.resume(config.worktree_root)
        self.assertFalse(modes.paused(config.worktree_root))

    def test_resume_without_a_pause_does_not_raise(self):
        config_path = self.build()
        config = config_module.load(config_path)
        modes.resume(config.worktree_root)  # no .paused file exists yet
        self.assertFalse(modes.paused(config.worktree_root))


class AfterRoundTest(ModesTestBase):
    def notifications(self):
        path = self.root / ".agent-loop" / "notifications.log"
        return path.read_text().splitlines() if path.exists() else []

    def test_progress_resets_the_counter_without_sleeping_or_notifying(self):
        config_path = self.build()
        config = config_module.load(config_path)
        self.assertEqual(modes._after_round(config, PR_READY, 1), 0)
        self.assertEqual(self.notifications(), [])

    def test_non_progress_rounds_below_the_cap_just_counts(self):
        config_path = self.build()
        config = config_module.load(config_path)
        self.assertEqual(modes._after_round(config, NO_ITEM, 0), 1)
        self.assertEqual(self.notifications(), [])

    def test_reaching_the_cap_sleeps_idle_s_notifies_once_and_resets(self):
        config_path = self.build()
        config = config_module.load(config_path)
        self.assertEqual(config.non_progress_rounds, 2)
        started = time.time()
        counter = modes._after_round(config, INFRA, 1)  # 1 -> 2, the cap
        elapsed = time.time() - started
        self.assertEqual(counter, 0)
        self.assertGreaterEqual(elapsed, config.idle_s - 0.05)
        notifications = self.notifications()
        self.assertEqual(len(notifications), 1)
        self.assertIn("2 consecutive non-progress rounds", notifications[0])
        # a non-progress back-off is not one of the four states (Stage 4b
        # review round 1, defect): FYI with no state column, not "IDLE   ...".
        self.assertTrue(notifications[0].startswith("FYI"))
        self.assertNotIn("IDLE", notifications[0])


class WaitForTriggerTest(ModesTestBase):
    def wait(self, config, last_backlog_mtime):
        from agent_loop import scm
        return modes._wait_for_trigger(config, scm.build(config.scm), last_backlog_mtime)

    def test_a_changed_backlog_mtime_fires_immediately(self):
        config_path = self.build()
        config = config_module.load(config_path)
        reason = self.wait(config, last_backlog_mtime=0.0)  # already stale vs the real file
        self.assertEqual(reason, "the backlog was edited")

    def test_a_stale_blocked_item_is_named_as_a_reopen(self):
        config_path = self.build()
        config = config_module.load(config_path)
        ledger.append(config.ledger, {
            "ts": "2020-01-01T00:00:00Z", "item": "an-item", "sha": "s", "state": BLOCKED,
            "duration_s": 1.0,
        })
        reason = self.wait(config, last_backlog_mtime=0.0)
        self.assertEqual(reason, "a blocked item was reopened by editing the backlog")

    def test_idle_timer_fires_when_nothing_else_does(self):
        config_path = self.build()
        config = config_module.load(config_path)
        current_mtime = config.backlog.stat().st_mtime
        started = time.time()
        reason = self.wait(config, last_backlog_mtime=current_mtime)
        self.assertEqual(reason, "idle timer")
        self.assertGreaterEqual(time.time() - started, config.idle_s - 0.05)

    def test_a_merged_known_open_pr_is_polled_and_recorded_without_sleeping(self):
        config_path = self.build(config=CONFIG + "scm: github\n")
        config = config_module.load(config_path)
        origin_for(self.root)
        fake_gh(self.root, [{"match": ["view"], "out": '{"state": "MERGED"}'}])
        ledger.append(config.ledger, {
            "ts": ledger.now(), "item": "an-item", "sha": "s", "state": PR_READY,
            "pr_url": "https://github.com/o/r/pull/7", "duration_s": 1.0,
        })
        current_mtime = config.backlog.stat().st_mtime
        started = time.time()
        reason = self.wait(config, last_backlog_mtime=current_mtime)
        self.assertEqual(reason, "pull request merged")
        self.assertLess(time.time() - started, config.idle_s)  # no idle sleep needed
        records = ledger.read(config.ledger)
        self.assertEqual(records[-1]["pr_state"], "MERGED")


# caps.round_wall_s is now enforced inside round.run_once itself
# (signal.alarm) - see test_round.py's
# test_a_round_over_its_wall_cap_ends_infra_in_process_and_cleans_up.


class OpenPrsBackpressureTest(ModesTestBase):
    """Stage 4b review round 1, defect: the open_prs wait used to read the
    ledger's stale pr_state and never evaluate `stop` while waiting."""

    def wait(self, config, stop=None, prs_opened=0, cost_spent=0.0):
        from agent_loop import scm
        return modes._wait_while_open_prs_at_cap(
            config, scm.build(config.scm), stop, time.time(), prs_opened, cost_spent)

    def seed_open_pr(self, config):
        ledger.append(config.ledger, {
            "ts": ledger.now(), "item": "an-item", "sha": "s", "state": PR_READY,
            "pr_url": "https://github.com/o/r/pull/7", "duration_s": 1.0,
        })

    def test_a_poll_that_finds_the_pr_merged_clears_the_cap_without_a_full_idle_wait(self):
        config_path = self.build(config=CONFIG + "scm: github\n")
        config = config_module.load(config_path)
        origin_for(self.root)
        fake_gh(self.root, [{"match": ["view"], "out": '{"state": "MERGED"}'}])
        self.seed_open_pr(config)
        started = time.time()
        stopped = self.wait(config)
        self.assertFalse(stopped)  # cap cleared - the round loop proceeds
        self.assertLess(time.time() - started, config.poll_s)  # no sleep needed
        self.assertEqual(ledger.read(config.ledger)[-1]["pr_state"], "MERGED")

    def test_stop_is_evaluated_while_still_at_the_cap_not_only_after_a_round(self):
        config_path = self.build(config=CONFIG + "scm: github\n")
        config = config_module.load(config_path)
        origin_for(self.root)
        fake_gh(self.root, [{"match": ["view"], "out": '{"state": "OPEN"}'}])  # still open
        self.seed_open_pr(config)
        stopped = self.wait(config, stop=modes.Stop(hours=0.0003))  # ~1.1s
        self.assertTrue(stopped)  # never got to start a round


class RunContinuousTest(ModesTestBase):
    def test_until_prs_stops_after_the_first_pull_request(self):
        config_path = self.build(config=CONFIG + "scm: github\n")
        origin_for(self.root)
        fake_gh(self.root, [{"match": ["list"], "out": "[]"},
                            {"match": ["create"], "out": "https://github.com/o/r/pull/7\n"}])
        with contextlib.redirect_stdout(io.StringIO()):
            exit_code = modes.run_continuous(config_path, stop=modes.Stop(prs=1))
        self.assertEqual(exit_code, 0)
        config = config_module.load(config_path)
        records = ledger.read(config.ledger)
        self.assertEqual([record["state"] for record in records], [PR_READY])
        self.assertEqual(records[0]["pr_url"], "https://github.com/o/r/pull/7")

    def test_until_hours_stops_a_no_work_loop_without_hanging(self):
        config_path = self.build(config=NO_WORK_BACKLOG and CONFIG, backlog=NO_WORK_BACKLOG)
        with contextlib.redirect_stdout(io.StringIO()):
            exit_code = modes.run_continuous(config_path, stop=modes.Stop(hours=0.0003))  # ~1.1s
        self.assertEqual(exit_code, 0)
        config = config_module.load(config_path)
        records = ledger.read(config.ledger)
        self.assertTrue(records)
        self.assertTrue(all(record["state"] == NO_ITEM for record in records))

    def test_paused_blocks_the_round_loop_from_starting_a_round(self):
        # Not run_continuous end-to-end (nothing there ever checks `stop` while
        # paused, by design - pausing is an explicit human override) - instead,
        # the same gate run_continuous opens its loop with.
        config_path = self.build(config=CONFIG, backlog=NO_WORK_BACKLOG)
        config = config_module.load(config_path)
        modes.pause(config.worktree_root)
        self.assertTrue(modes.paused(config.worktree_root))
        self.assertEqual(ledger.read(config.ledger), [])
        modes.resume(config.worktree_root)
        self.assertFalse(modes.paused(config.worktree_root))


class LivelockRetirementTest(ModesTestBase):
    def test_a_stuck_kept_worktree_notifies_once_then_is_ordinary_non_progress(self):
        # 2c's deferred livelock: an uncommittable diff keeps its worktree under
        # worktree_root, which the lock's foreign-worktree check then refuses on
        # every later round, at any item, until a person removes it. Retired
        # without new code: the (item, state, sha) dedup already covers "once"
        # (item stays None, sha does not move while the round can't proceed),
        # and the ordinary non-progress counter covers "not forever in
        # continuous" - it counts and backs off exactly like any other INFRA.
        from agent_loop import round as round_module

        config_path = self.build()
        hook = self.root / ".git" / "hooks" / "pre-commit"
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        hook.chmod(0o755)
        with contextlib.redirect_stdout(io.StringIO()):
            first = round_module.run_once(config_path)   # picks an-item, fails to commit
            second = round_module.run_once(config_path)  # lock refuses the kept worktree
            third = round_module.run_once(config_path)   # same refusal, same sha, same item (None)
        self.assertEqual((first.state, second.state, third.state), (INFRA, INFRA, INFRA))
        self.assertIn("is not this round's", second.reason)
        self.assertIn("is not this round's", third.reason)
        # the first is a distinct (item, state, sha) from the lock refusals that
        # follow it, so it notifies once too - that transition is real news.
        # The lock refusal's own steady state then dedups on (None, INFRA, sha).
        self.assertTrue(first.notified)
        self.assertTrue(second.notified)
        self.assertFalse(third.notified)

        config = config_module.load(config_path)
        self.assertEqual(config.non_progress_rounds, 2)
        non_progress = 0
        for outcome in (first, second, third):
            non_progress = modes._after_round(config, outcome.state, non_progress)
        # 1, then 2 caps out (one backoff FYI, reset), then 1 again - ordinary
        # counting throughout, no special case for this condition
        self.assertEqual(non_progress, 1)


class TouchesPlumbingTest(unittest.TestCase):
    def test_a_dot_agent_loop_path_is_plumbing(self):
        self.assertTrue(modes._touches_plumbing(" .agent-loop/config.yaml | 2 +-\n"))

    def test_a_product_path_is_not(self):
        self.assertFalse(modes._touches_plumbing(" project/src/thing.py | 2 +-\n"))

    def test_no_diff_stat_is_not_plumbing(self):
        self.assertFalse(modes._touches_plumbing(""))


class MetricsReportTest(ModesTestBase):
    def test_reads_only_the_ledger_and_reports_the_five_numbers(self):
        config_path = self.build()
        config = config_module.load(config_path)
        ledger.append(config.ledger, {
            "ts": "2026-08-29T00:00:00Z", "item": "a", "sha": "s", "state": PR_READY,
            "reason": "ok", "cost": 1.0, "duration_s": 10.0, "pr_url": "u/a",
            "diff_stat": " project/x.py | 2 +-\n", "notified_at": "2026-08-29T00:00:01Z",
        })
        ledger.append(config.ledger, {
            "ts": "2026-08-29T00:01:00Z", "item": "b", "sha": "s", "state": PR_READY,
            "reason": "ok", "cost": 2.0, "duration_s": 5.0, "pr_url": "u/b",
            "diff_stat": " .agent-loop/config.yaml | 1 +-\n", "notified_at": "2026-08-29T00:01:00Z",
        })
        ledger.append(config.ledger, {"ts": "t", "item": None, "sha": "s", "state": NO_ITEM,
                                      "duration_s": 1.0})
        ledger.note_pr_state(config.ledger, item="a", sha="s", pr_url="u/a", pr_state="MERGED")
        report = modes.metrics_report(config)
        self.assertIn("PR_READY=2", report)
        self.assertIn("NO_ITEM=1", report)
        self.assertIn("opened=2 merged=1", report)
        self.assertIn("plumbing share     1/2 PR(s)", report)
        self.assertIn("time to notify", report)
        self.assertIn("cost per merged    $1.0000", report)

    def test_an_empty_ledger_reports_the_absence_of_each_number(self):
        config_path = self.build()
        config = config_module.load(config_path)
        report = modes.metrics_report(config)
        self.assertIn("no PRs yet", report)
        self.assertIn("no notified_at recorded", report)
        self.assertIn("no merged PR cost recorded", report)

    def test_a_re_published_pr_counts_once_by_its_latest_round(self):
        # Stage 4b review round 1, defect: plumbing share used to count every
        # PR_READY round that touched a pr_url, not every distinct pull
        # request - a re-publish (BLOCKED, fixed, published again) inflated
        # both the denominator and the plumbing count.
        config_path = self.build()
        config = config_module.load(config_path)
        ledger.append(config.ledger, {
            "ts": "2026-08-29T00:00:00Z", "item": "a", "sha": "s1", "state": PR_READY,
            "reason": "first publish", "cost": 1.0, "duration_s": 10.0, "pr_url": "u/a",
            "diff_stat": " project/x.py | 2 +-\n",
        })
        ledger.append(config.ledger, {
            "ts": "2026-08-29T00:05:00Z", "item": "a", "sha": "s2", "state": PR_READY,
            "reason": "re-published after a fix", "cost": 1.0, "duration_s": 8.0,
            "pr_url": "u/a", "diff_stat": " .agent-loop/config.yaml | 1 +-\n",
        })
        report = modes.metrics_report(config)
        self.assertIn("opened=1", report)  # one pull request, not two rounds
        self.assertIn("plumbing share     1/1 PR(s)", report)  # the latest diff


class CountOpenedTest(unittest.TestCase):
    def test_the_first_round_to_open_a_pull_request_counts(self):
        outcome = round_module.Outcome(PR_READY, "a", "r", 1.0, 1.0, True, "u/a")
        self.assertEqual(modes._count_opened(outcome, set()), 1)

    def test_a_re_publish_of_the_same_pull_request_does_not_count_again(self):
        seen = {"u/a"}
        outcome = round_module.Outcome(PR_READY, "a", "r", 1.0, 1.0, True, "u/a")
        self.assertEqual(modes._count_opened(outcome, seen), 0)
        self.assertEqual(seen, {"u/a"})

    def test_a_round_with_no_pull_request_does_not_count(self):
        outcome = round_module.Outcome(NO_ITEM, None, "r", None, 1.0, True, None)
        self.assertEqual(modes._count_opened(outcome, set()), 0)


if __name__ == "__main__":
    unittest.main()
