import json
import unittest

from agent_loop import ledger
from agent_loop.states import BLOCKED, PR_READY

from support import cleanup, make_repo


class LedgerTest(unittest.TestCase):
    def setUp(self):
        self.root = make_repo()
        self.path = self.root / ".agent-loop" / "ledger.jsonl"

    def tearDown(self):
        cleanup(self.root)

    def test_a_round_appends_exactly_one_line_with_the_recorded_fields(self):
        ledger.append(self.path, {
            "ts": "2026-08-29T00:00:00Z", "item": "an-item", "sha": "abc",
            "state": PR_READY, "reason": "verified", "cost": 1.9,
            "duration_s": 38.0, "tool_versions": {"git": "git version 2.50.1"},
        })
        ledger.append(self.path, {"ts": "t", "item": "b", "sha": "abc", "state": BLOCKED})
        lines = self.path.read_text().splitlines()
        self.assertEqual(len(lines), 2)
        first = json.loads(lines[0])
        self.assertEqual(set(first), set(ledger.FIELDS))
        self.assertEqual(first["cost"], 1.9)
        self.assertEqual(first["tool_versions"], {"git": "git version 2.50.1"})
        self.assertEqual(json.loads(lines[1])["item"], "b")

    def test_reading_skips_blank_and_unparseable_lines(self):
        self.path.write_text('{"item": "a"}\n\nnot json\n{"item": "b"}\n', encoding="utf-8")
        self.assertEqual([record["item"] for record in ledger.read(self.path)], ["a", "b"])

    def test_an_absent_ledger_reads_as_no_rounds(self):
        self.assertEqual(ledger.read(self.root / "absent.jsonl"), [])

    def test_blocked_items_are_remembered_per_sha(self):
        records = [
            {"item": "a", "sha": "one", "state": BLOCKED},
            {"item": "b", "sha": "two", "state": BLOCKED},
            {"item": "c", "sha": "one", "state": PR_READY},
        ]
        self.assertEqual(ledger.blocked_at(records, "one"), {"a"})
        self.assertEqual(ledger.blocked_at(records, "two"), {"b"})

    def test_notification_dedup_is_keyed_on_item_state_and_sha(self):
        records = [{"item": "a", "sha": "one", "state": BLOCKED}]
        self.assertTrue(ledger.already_notified(records, "a", BLOCKED, "one"))
        self.assertFalse(ledger.already_notified(records, "a", BLOCKED, "two"))
        self.assertFalse(ledger.already_notified(records, "a", PR_READY, "one"))
        self.assertFalse(ledger.already_notified(records, "b", BLOCKED, "one"))

    def test_tool_version_drift_is_a_warning_naming_what_changed(self):
        first = {"git": "git version 2.50.1", "claude": "2.1.251"}
        second = {"git": "git version 2.50.1", "claude": "2.2.0"}
        records = [{"tool_versions": first}]
        self.assertIsNone(ledger.drift([], second))
        self.assertIsNone(ledger.drift(records, dict(first)))
        warning = ledger.drift(records, second)
        self.assertIn("claude 2.1.251 -> 2.2.0", warning)
        self.assertNotIn("git", warning)
        # a tool that appears or disappears is named too
        self.assertIn("codex absent -> 1.0", ledger.drift(records, dict(first, codex="1.0")))

    def test_drift_compares_against_the_last_round_that_recorded_versions(self):
        records = [{"tool_versions": {"git": "old"}}, {"tool_versions": None}, {}]
        self.assertIsNone(ledger.drift(records, {"git": "old"}))
        self.assertIn("git old -> new", ledger.drift(records, {"git": "new"}))

    def test_a_round_with_no_item_dedups_on_the_empty_item(self):
        self.assertTrue(ledger.already_notified(
            [{"item": None, "sha": "one", "state": "NO_ITEM"}], None, "NO_ITEM", "one"))


if __name__ == "__main__":
    unittest.main()
