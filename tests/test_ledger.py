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
            "notified": True,
        })
        ledger.append(self.path, {"ts": "t", "item": "b", "sha": "abc", "state": BLOCKED})
        lines = self.path.read_text().splitlines()
        self.assertEqual(len(lines), 2)
        first = json.loads(lines[0])
        self.assertEqual(set(first), set(ledger.FIELDS) | {"notified"})
        self.assertEqual(first["cost"], 1.9)
        self.assertEqual(first["tool_versions"], {"git": "git version 2.50.1"})
        self.assertIs(json.loads(lines[1])["notified"], False)

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
        records = [{"item": "a", "sha": "one", "state": BLOCKED, "notified": True}]
        self.assertTrue(ledger.already_notified(records, "a", BLOCKED, "one"))
        self.assertFalse(ledger.already_notified(records, "a", BLOCKED, "two"))
        self.assertFalse(ledger.already_notified(records, "a", PR_READY, "one"))
        self.assertFalse(ledger.already_notified(records, "b", BLOCKED, "one"))

    def test_an_unnotified_line_does_not_suppress_the_notification(self):
        records = [{"item": "a", "sha": "one", "state": BLOCKED, "notified": False}]
        self.assertFalse(ledger.already_notified(records, "a", BLOCKED, "one"))

    def test_tool_drift_is_a_warning_and_never_raises(self):
        records = [{"tool_versions": {"git": "git version 2.0.0", "python3": "Python 3.9.6"}}]
        warning = ledger.drift(records, {"git": "git version 2.50.1", "python3": "Python 3.9.6"})
        self.assertIn("git version 2.0.0 -> git version 2.50.1", warning)
        self.assertIsNone(ledger.drift(records, {"git": "git version 2.0.0", "python3": "Python 3.9.6"}))
        self.assertIsNone(ledger.drift([], {"git": "x"}))


if __name__ == "__main__":
    unittest.main()
