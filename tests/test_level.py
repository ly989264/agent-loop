"""The L1/L2 decision table, and a DECIDE that nobody answered."""

import unittest

from agent_loop import level, notify

CONTRACT = {"kind": "contract", "location": "a.py:1", "claim": "c", "citation": "§0"}
DEFECT = {"kind": "defect", "location": "a.py:1", "claim": "c", "citation": "the case"}
SUGGESTION = {"kind": "suggestion", "location": "a.py:1", "claim": "c", "citation": ""}
PROTECTED = ["project/schemas/run.json"]


class DecisionTableTest(unittest.TestCase):
    def check(self, expected_merge, expected_decision, *arguments):
        decision = level.decide(*arguments)
        self.assertEqual((decision.merge, decision.decision),
                         (expected_merge, expected_decision), decision.reason)
        return decision

    def test_l1_never_merges_whatever_the_reviewer_found(self):
        for findings in ([], [SUGGESTION], [DEFECT], [CONTRACT]):
            decision = self.check(False, level.FYI, "L1", findings, [], True)
            self.assertIn("a person merges", decision.reason)

    def test_l2_merges_a_clean_diff_with_no_contract_or_defect_finding(self):
        self.check(True, level.FYI, "L2", [], [], True)
        self.check(True, level.FYI, "L2", [SUGGESTION, SUGGESTION], [], True)

    def test_l2_holds_a_contract_or_defect_finding_as_a_decide(self):
        decision = self.check(False, level.DECIDE, "L2", [SUGGESTION, DEFECT], [], True)
        self.assertIn("1 defect finding(s)", decision.reason)
        self.assertIn("merge anyway?", decision.reason)
        self.assertIn("contract", self.check(
            False, level.DECIDE, "L2", [CONTRACT], [], True).reason)

    def test_a_protected_path_holds_the_merge_at_l2(self):
        decision = self.check(False, level.DECIDE, "L2", [], PROTECTED, True)
        self.assertIn("project/schemas/run.json", decision.reason)

    def test_a_diff_the_verify_step_flagged_holds_the_merge_at_l2(self):
        decision = self.check(False, level.DECIDE, "L2", [], [], False)
        self.assertIn("verify step flagged", decision.reason)

    def test_a_protected_path_holds_at_l1_too_and_stays_a_person_s_merge(self):
        # Invariant 8 is the floor: neither level merges this diff.
        self.assertFalse(level.decide("L1", [], PROTECTED, True).merge)
        self.assertFalse(level.decide("L2", [], PROTECTED, True).merge)


class DecideNotificationTest(unittest.TestCase):
    def test_a_decide_line_is_one_line_carrying_the_item_the_link_and_the_question(self):
        decision = level.decide("L2", [DEFECT], [], True)
        text = notify.line("an-item", "PR_READY", "abcdef1234567",
                           "https://github.com/o/r/pull/7; %s" % decision.reason,
                           decision.decision)
        self.assertEqual(len(text.splitlines()), 1)
        self.assertTrue(text.startswith("DECIDE "))
        self.assertIn("an-item", text)
        self.assertIn("https://github.com/o/r/pull/7", text)
        self.assertIn("merge anyway?", text)

    def test_an_fyi_line_says_fyi_and_a_round_with_no_question_says_neither(self):
        self.assertTrue(
            notify.line("an-item", "PR_READY", "abc", "url", level.FYI).startswith("FYI "))
        plain = notify.line("an-item", "INFRA", "abc", "the lock is held")
        self.assertFalse(plain.startswith("FYI"))
        self.assertFalse(plain.startswith("DECIDE"))


class ExpiryTest(unittest.TestCase):
    NOW = 1_800_000_000.0

    def record(self, **overrides):
        base = {"item": "an-item", "ts": "2026-08-29T00:00:00Z", "state": "PR_READY",
                "pr_url": "https://github.com/o/r/pull/7", "decision": level.DECIDE}
        base.update(overrides)
        return base

    def test_a_decide_past_its_expiry_and_still_open_blocks_the_item(self):
        reason = level.expired([self.record()], "an-item", self.NOW, lambda url: True)
        self.assertIn("pull/7", reason)
        self.assertIn("waiting on the operator", reason)

    def test_a_decide_that_was_answered_or_is_young_or_is_an_fyi_blocks_nothing(self):
        fresh_now = level._seconds("2026-08-29T00:00:00Z") + level.EXPIRY_S - 1
        self.assertIsNone(level.expired([self.record()], "an-item", fresh_now, lambda url: True))
        self.assertIsNone(level.expired([self.record()], "an-item", self.NOW, lambda url: False))
        self.assertIsNone(level.expired(
            [self.record(decision=level.FYI)], "an-item", self.NOW, lambda url: True))
        self.assertIsNone(level.expired([self.record()], "other", self.NOW, lambda url: True))
        self.assertIsNone(level.expired(
            [self.record(pr_url=None)], "an-item", self.NOW, lambda url: True))

    def test_a_timestamp_that_cannot_be_read_never_expires(self):
        self.assertIsNone(level.expired(
            [self.record(ts="yesterday")], "an-item", self.NOW, lambda url: True))


if __name__ == "__main__":
    unittest.main()
