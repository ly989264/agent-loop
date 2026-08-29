import unittest

from agent_loop.backlog import Item
from agent_loop.context import MAX_CONTEXT_BYTES, ContextTooLarge, build_worker_bundle, encode
from agent_loop.schemas import WORKER_OUTPUT_SCHEMA

from support import cleanup, make_repo


def item(**overrides):
    fields = dict(
        id="an-item",
        group="g",
        statement="a statement",
        cost_class="hermetic",
        selectable=True,
        sites=("project/src/thing.py:3",),
        design_doc="docs/design.md 11.1",
        probe="exit 1",
        proof="the test fails",
    )
    fields.update(overrides)
    return Item(**fields)


class ContextTest(unittest.TestCase):
    def setUp(self):
        self.root = make_repo()
        (self.root / "project" / "src").mkdir(parents=True)
        (self.root / "project" / "src" / "thing.py").write_text(
            "\n".join("line %d" % n for n in range(1, 60)), encoding="utf-8")

    def tearDown(self):
        cleanup(self.root)

    def bundle(self, probe_output="boom", **overrides):
        return build_worker_bundle(
            item=item(**overrides),
            probe_output=probe_output,
            probe_exit_code=1,
            root=self.root,
            schema=WORKER_OUTPUT_SCHEMA,
            sha="abc123",
        )

    def test_the_bundle_carries_what_the_worker_needs(self):
        bundle = self.bundle()
        self.assertEqual(bundle["item"]["statement"], "a statement")
        self.assertEqual(bundle["probe"]["output"], "boom")
        self.assertEqual(bundle["design_doc_section"], "docs/design.md 11.1")
        self.assertEqual(bundle["output_schema"], WORKER_OUTPUT_SCHEMA)
        excerpt = bundle["sites"][0]
        self.assertEqual(excerpt["line"], 3)
        self.assertEqual(excerpt["first_line"], 1)
        self.assertIn("line 3", excerpt["text"])

    def test_a_site_excerpt_is_bounded_not_the_whole_file(self):
        excerpt = self.bundle(sites=("project/src/thing.py:30",))["sites"][0]
        self.assertEqual((excerpt["first_line"], excerpt["last_line"]), (10, 50))
        self.assertNotIn("line 9\n", excerpt["text"])

    def test_an_unreadable_site_states_its_reason(self):
        excerpt = self.bundle(sites=("project/src/absent.py:3",))["sites"][0]
        self.assertIn("cannot read cited file", excerpt["absent_reason"])

    def test_an_oversized_bundle_is_refused_not_truncated(self):
        bundle = self.bundle(probe_output="x" * (MAX_CONTEXT_BYTES + 1))
        with self.assertRaises(ContextTooLarge) as caught:
            encode(bundle)
        self.assertIn("refused rather than truncated", str(caught.exception))
        self.assertEqual(len(bundle["probe"]["output"]), MAX_CONTEXT_BYTES + 1)

    def test_a_bundle_under_the_cap_encodes(self):
        self.assertIn("a statement", encode(self.bundle()))


if __name__ == "__main__":
    unittest.main()
