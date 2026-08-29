import unittest

from agent_loop import backlog, config as config_module, pick

from support import BACKLOG, cleanup, make_repo


class PickTest(unittest.TestCase):
    def setUp(self):
        self.root = make_repo()
        self.config = config_module.load(self.root / ".agent-loop" / "config.yaml")
        self.items = backlog.load(self.config.backlog)

    def tearDown(self):
        cleanup(self.root)

    def test_the_first_failing_probe_in_file_order_is_chosen(self):
        selection = pick.pick(self.config, self.items, self.root)
        self.assertIsNotNone(selection)
        self.assertEqual(selection.item.id, "second")
        self.assertEqual(selection.probe.exit_code, 3)

    def test_items_without_a_probe_and_unselectable_items_are_skipped(self):
        runs = pick.run_probes(self.config, self.items, self.root)
        self.assertEqual([run.item.id for run in runs], ["first", "second", "third"])

    def test_an_item_blocked_at_this_sha_is_skipped(self):
        selection = pick.pick(self.config, self.items, self.root, skip_ids={"second"})
        self.assertEqual(selection.item.id, "third")
        runs = pick.run_probes(self.config, self.items, self.root, skip_ids={"second"})
        self.assertNotIn("second", [run.item.id for run in runs])

    def test_no_failing_probe_selects_nothing(self):
        items = backlog.load(self.config.backlog)
        passing = tuple(
            item for item in items if item.id == "first"
        )
        self.assertIsNone(pick.pick(self.config, passing, self.root))

    def test_probes_run_in_the_cost_class_working_directory(self):
        text = BACKLOG.replace('probe: "exit 3"', 'probe: "test \\"$(basename \\"$PWD\\")\\" = project"')
        cleanup(self.root)
        self.root = make_repo(backlog=text)
        config = config_module.load(self.root / ".agent-loop" / "config.yaml")
        items = backlog.load(config.backlog)
        selection = pick.pick(config, items, self.root)
        self.assertEqual(selection.item.id, "third")


if __name__ == "__main__":
    unittest.main()
