"""The reviewer's output schema, and the array support it needs."""

import unittest

from agent_loop.context import FINDING_CLASSES, build_reviewer_bundle
from agent_loop.backlog import Item
from agent_loop.schemas import REVIEWER_OUTPUT_SCHEMA, validate

FINDING = {"kind": "contract", "location": "a.py:1", "claim": "c", "citation": "ROADMAP §0"}


class ReviewerSchemaTest(unittest.TestCase):
    def test_no_findings_and_well_formed_findings_are_accepted(self):
        self.assertIsNone(validate(REVIEWER_OUTPUT_SCHEMA, {"findings": []}))
        self.assertIsNone(validate(REVIEWER_OUTPUT_SCHEMA, {"findings": [FINDING]}))

    def test_a_findings_value_that_is_not_an_array_is_rejected(self):
        self.assertEqual(
            validate(REVIEWER_OUTPUT_SCHEMA, {"findings": "contract: a.py:1"}),
            "output.findings is not an array")

    def test_a_finding_of_an_unknown_kind_is_rejected_by_element(self):
        bad = dict(FINDING, kind="blocker")
        reason = validate(REVIEWER_OUTPUT_SCHEMA, {"findings": [FINDING, bad]})
        self.assertIn("output.findings[1].kind", reason)

    def test_a_finding_missing_its_citation_is_rejected(self):
        bad = {key: value for key, value in FINDING.items() if key != "citation"}
        self.assertIn("citation", validate(REVIEWER_OUTPUT_SCHEMA, {"findings": [bad]}))


class ReviewerBundleTest(unittest.TestCase):
    def test_the_bundle_carries_the_item_the_diff_and_the_finding_classes(self):
        item = Item(id="an-item", group="g", statement="s", cost_class="hermetic",
                    selectable=True, sites=(), design_doc="d")
        bundle = build_reviewer_bundle(
            item=item, diff="--- a\n+++ b\n", sha="abc", schema=REVIEWER_OUTPUT_SCHEMA)
        self.assertEqual(bundle["role"], "reviewer")
        self.assertEqual(bundle["item"]["id"], "an-item")
        self.assertIn("+++ b", bundle["diff"])
        self.assertEqual(sorted(bundle["finding_classes"]),
                         ["_rules", "contract", "defect", "suggestion"])
        self.assertEqual(bundle["finding_classes"], FINDING_CLASSES)


if __name__ == "__main__":
    unittest.main()
