"""``agent-loop plan``: the planner role, and admission of what it proposes.

The planner is a fake `shell:` adapter - a script that prints one JSON object -
so every case here is hermetic: no network, no real agent, no Docker.
"""

import io
import json
import textwrap
import unittest
from contextlib import redirect_stdout

import yaml

from agent_loop import backlog, ledger, plan
from agent_loop.cli import main

from support import CONFIG, cleanup, make_repo, write_script

PLANNER_CONFIG = CONFIG.replace(
    "agents:\n  worker:", "agents:\n  planner:\n    - shell:PLANNER\n  worker:"
).replace(
    "caps:\n  worker:",
    "caps:\n  planner:\n    wall_s: 60\n    silence_s: 30\n    max_tokens: 1000\n  worker:",
)

PROPOSAL = {
    "id": "a-proposed-item",
    "statement": "the reader retries on an error reply",
    "cost_class": "hermetic",
    "sites": ["project/src/thing.py:3"],
    "probe": "exit 7",
    "proof": "the probe exits 0 once the reply is classified",
    "rationale": "the ledger shows two rounds blocked on it",
}

FAKE_PLANNER = """\
#!/usr/bin/env python3
import os, sys
with open(os.environ["PLANNER_RECORD"], "a") as handle:
    handle.write("=== call argv=%s\\n%s" % (" ".join(sys.argv[1:]), sys.stdin.read()))
sys.stdout.write(open(os.environ["PLANNER_REPLY"]).read())
"""


class PlanTest(unittest.TestCase):
    def setUp(self):
        self.root = make_repo(config=PLANNER_CONFIG)
        self.planner = write_script(self.root, "fake_planner.py", FAKE_PLANNER)
        self.config_path = self.root / ".agent-loop" / "config.yaml"
        self.rewrite_config()
        self.reply_path = self.root / "reply.json"
        import os
        os.environ["PLANNER_RECORD"] = str(self.root / "planner_stdin.txt")
        os.environ["PLANNER_REPLY"] = str(self.reply_path)

    def tearDown(self):
        cleanup(self.root)

    def rewrite_config(self, extra=""):
        self.config_path.write_text(
            textwrap.dedent(PLANNER_CONFIG).replace("shell:PLANNER", "shell:%s" % self.planner)
            + extra,
            encoding="utf-8",
        )

    def reply(self, *proposals, raw=None):
        self.reply_path.write_text(
            raw if raw is not None else json.dumps({"proposals": list(proposals)}),
            encoding="utf-8",
        )

    def run_plan(self):
        captured = io.StringIO()
        with redirect_stdout(captured):
            outcome = plan.run_plan(self.config_path)
        return outcome, captured.getvalue()

    def proposals_file(self):
        return yaml.safe_load(
            (self.root / ".agent-loop" / "worktrees" / "proposals.yaml").read_text()
        )

    # ---- the bundle and the schema -------------------------------------

    def test_the_planner_is_asked_with_consumer_data_and_its_schema(self):
        (self.root / "docs").mkdir()
        (self.root / "docs" / "open.md").write_text("an open question", encoding="utf-8")
        self.rewrite_config("\nplan_sources:\n  - docs/*.md\n")
        self.reply(PROPOSAL)
        outcome, _ = self.run_plan()
        self.assertTrue(outcome.ok)
        sent = (self.root / "planner_stdin.txt").read_text()
        self.assertIn('"role": "planner"', sent)
        self.assertIn("an open question", sent)
        self.assertIn("first item", sent)  # the backlog's statements
        self.assertIn("rationale", sent)  # the output schema
        # the adapter is given the role and the read-only sandbox, as every
        # other role's invocation is
        self.assertIn("=== call argv=planner read-only", sent)
        self.assertEqual(sent.count("=== call"), 1)

    def test_a_malformed_answer_gets_exactly_one_repair(self):
        self.reply(raw="not json at all")
        outcome, output = self.run_plan()
        self.assertFalse(outcome.ok)
        self.assertIn("planner returned malformed", outcome.reason)
        # one repair: the planner was asked twice, the second time with the
        # repair echo, and no third time
        sent = (self.root / "planner_stdin.txt").read_text()
        self.assertEqual(sent.count("=== call"), 2)
        self.assertIn("rejected as malformed", sent)
        self.assertEqual(output.count("FYI"), 1)

    def test_a_repaired_answer_is_accepted(self):
        script = self.root / "repairing.py"
        script.write_text(textwrap.dedent("""\
            #!/usr/bin/env python3
            import json, os, sys
            path = os.environ["PLANNER_RECORD"]
            text = sys.stdin.read()
            first = not os.path.exists(path)
            open(path, "a").write(text)
            sys.stdout.write("garbage" if first else open(os.environ["PLANNER_REPLY"]).read())
            """), encoding="utf-8")
        script.chmod(0o755)
        self.planner = script
        self.rewrite_config()
        self.reply(PROPOSAL)
        outcome, _ = self.run_plan()
        self.assertTrue(outcome.ok)
        self.assertEqual((outcome.admitted, outcome.rejected), (1, 0))

    # ---- admission ------------------------------------------------------

    def test_a_failing_probe_is_admitted_to_proposals_yaml_with_its_output(self):
        self.reply(dict(PROPOSAL, probe="echo why-it-fails; exit 7"))
        outcome, output = self.run_plan()
        self.assertEqual((outcome.admitted, outcome.rejected), (1, 0))
        entry = self.proposals_file()["proposals"][0]
        self.assertTrue(entry["admitted"])
        self.assertIsNone(entry["rejection"])
        self.assertEqual(entry["probe_observed"]["exit_code"], 7)
        self.assertIn("why-it-fails", entry["probe_observed"]["output_tail"])
        self.assertEqual(entry["probe_observed"]["cwd"], "project")
        self.assertIn("1 admitted, 0 rejected", output)

    def test_a_passing_probe_is_rejected_and_recorded_with_its_output(self):
        self.reply(dict(PROPOSAL, probe="echo already-closed; exit 0"))
        outcome, _ = self.run_plan()
        self.assertEqual((outcome.admitted, outcome.rejected), (0, 1))
        entry = self.proposals_file()["proposals"][0]
        self.assertFalse(entry["admitted"])
        self.assertIn("probe exits 0", entry["rejection"])
        self.assertEqual(entry["probe_observed"]["exit_code"], 0)
        self.assertIn("already-closed", entry["probe_observed"]["output_tail"])

    def test_a_duplicate_id_or_statement_is_rejected_without_spending_a_probe(self):
        self.reply(
            dict(PROPOSAL, id="second"),
            dict(PROPOSAL, id="fresh", statement="  Second   Item  "),
            dict(PROPOSAL, id="also-fresh", probe="exit 1"),
            dict(PROPOSAL, id="also-fresh-2", probe="exit 1"),
        )
        outcome, _ = self.run_plan()
        entries = self.proposals_file()["proposals"]
        self.assertIn("duplicates an existing backlog item", entries[0]["rejection"])
        self.assertNotIn("probe_observed", entries[0])
        self.assertIn("duplicates an existing backlog item", entries[1]["rejection"])
        self.assertTrue(entries[2]["admitted"])
        # a planner that proposes the same statement twice: the second is a
        # duplicate of the first, which admission has already taken
        self.assertIn("duplicates an existing backlog item", entries[3]["rejection"])
        self.assertEqual((outcome.admitted, outcome.rejected), (1, 3))

    def test_a_cost_class_with_no_verify_entry_is_rejected(self):
        self.reply(dict(PROPOSAL, cost_class="needs-fleet"))
        outcome, _ = self.run_plan()
        self.assertEqual((outcome.admitted, outcome.rejected), (0, 1))
        self.assertIn("no verify entry", self.proposals_file()["proposals"][0]["rejection"])

    # ---- what a plan run is, and is not ---------------------------------

    def test_exactly_one_fyi_and_no_ledger_line(self):
        self.reply(PROPOSAL, dict(PROPOSAL, id="another", statement="another", probe="exit 0"))
        _, output = self.run_plan()
        self.assertEqual(output.splitlines(),
                         [line for line in output.splitlines() if line.startswith("FYI")])
        self.assertEqual(len(output.strip().splitlines()), 1)
        # a plan run is not a round: no item, no sha, none of the four states,
        # so nothing is written to the round ledger
        self.assertEqual(ledger.read(self.root / ".agent-loop" / "ledger.jsonl"), [])

    def test_the_backlog_is_untouched_below_l3(self):
        before = (self.root / ".agent-loop" / "backlog.yaml").read_text()
        self.reply(PROPOSAL)
        outcome, _ = self.run_plan()
        self.assertEqual(outcome.admitted, 1)
        self.assertEqual((self.root / ".agent-loop" / "backlog.yaml").read_text(), before)

    # ---- L3 ------------------------------------------------------------

    def enable_l3(self):
        self.config_path.write_text(
            self.config_path.read_text().replace(
                "levels:\n  hermetic: L1", "levels:\n  hermetic: L1\n  planner: L3"),
            encoding="utf-8")

    def test_l3_appends_admitted_items_to_a_backlog_the_loader_re_reads(self):
        self.enable_l3()
        backlog_path = self.root / ".agent-loop" / "backlog.yaml"
        before = backlog_path.read_text()
        self.reply(
            dict(PROPOSAL, probe="exit 7"),
            dict(PROPOSAL, id="rejected-one", statement="closed already", probe="exit 0"),
        )
        outcome, output = self.run_plan()
        self.assertEqual(outcome.appended, ("a-proposed-item",))
        self.assertIn("appended to", output)
        # every existing entry survives byte for byte, and the file still loads
        after = backlog_path.read_text()
        self.assertTrue(after.startswith(before))
        items = backlog.load(backlog_path)
        self.assertEqual([item.id for item in items][-1], "a-proposed-item")
        appended = items[-1]
        self.assertEqual(appended.statement, PROPOSAL["statement"])
        self.assertEqual(appended.cost_class, "hermetic")
        self.assertEqual(appended.probe, "exit 7")
        self.assertEqual(appended.proof, PROPOSAL["proof"])
        self.assertEqual(appended.sites, ("project/src/thing.py:3",))
        self.assertTrue(appended.selectable)
        self.assertIn("probe observed exit 7", appended.notes)
        # the rejected one is not in the backlog at any level
        self.assertNotIn("rejected-one", [item.id for item in items])

    def test_l3_bootstraps_an_empty_backlog_past_its_flow_sequence(self):
        # `items: []` is a flow sequence: an indented `- ` written after it is
        # a syntax error, not a continuation, and the file would never load
        # again - which is the one case L3 exists for, an empty backlog.
        self.enable_l3()
        backlog_path = self.root / ".agent-loop" / "backlog.yaml"
        backlog_path.write_text(
            "# a backlog with nothing in it yet\n"
            "# - this comment is not a sequence entry\n"
            "items: []\n", encoding="utf-8")
        self.reply(dict(PROPOSAL, probe="exit 7"))
        outcome, _ = self.run_plan()
        self.assertEqual(outcome.appended, ("a-proposed-item",))
        text = backlog_path.read_text()
        self.assertIn("# - this comment is not a sequence entry", text)
        items = backlog.load(backlog_path)
        self.assertEqual([item.id for item in items], ["a-proposed-item"])

    def test_l3_refuses_a_backlog_shape_it_cannot_append_to(self):
        self.enable_l3()
        backlog_path = self.root / ".agent-loop" / "backlog.yaml"
        flow = ("items: [{id: only, group: g, statement: s, cost_class: hermetic, "
                "selectable: true, sites: [], design_doc: ''}]\n")
        backlog_path.write_text(flow, encoding="utf-8")
        self.reply(dict(PROPOSAL, probe="exit 7"))
        outcome, output = self.run_plan()
        self.assertEqual((outcome.admitted, outcome.appended), (1, ()))
        self.assertIn("not appended", output)
        self.assertEqual(backlog_path.read_text(), flow)

    def test_l3_appends_nothing_when_nothing_was_admitted(self):
        self.enable_l3()
        backlog_path = self.root / ".agent-loop" / "backlog.yaml"
        before = backlog_path.read_text()
        self.reply(dict(PROPOSAL, probe="exit 0"))
        outcome, _ = self.run_plan()
        self.assertEqual((outcome.admitted, outcome.appended), (0, ()))
        self.assertEqual(backlog_path.read_text(), before)

    def test_the_cli_exposes_plan_and_its_exit_codes(self):
        self.reply(PROPOSAL)
        captured = io.StringIO()
        with redirect_stdout(captured):
            self.assertEqual(main(["plan", "--config", str(self.config_path)]), 0)
            self.reply(raw="not json")
            self.assertEqual(main(["plan", "--config", str(self.config_path)]), 2)


if __name__ == "__main__":
    unittest.main()
