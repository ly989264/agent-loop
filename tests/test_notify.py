import contextlib
import io
import unittest

from agent_loop import config as config_module, notify
from agent_loop.config import NotifyTarget
from agent_loop.states import BLOCKED

from support import CONFIG, cleanup, make_repo


class NotifyTest(unittest.TestCase):
    def setUp(self):
        self.root = make_repo(config=CONFIG.replace(
            "  - stdout", "  - stdout\n  - target: file\n    path: .agent-loop/notifications.log"))
        self.config = config_module.load(self.root / ".agent-loop" / "config.yaml")

    def tearDown(self):
        cleanup(self.root)

    def test_one_notification_reaches_every_configured_target(self):
        with contextlib.redirect_stdout(io.StringIO()) as captured:
            text = notify.notify(self.config, item="an-item", state=BLOCKED,
                                 sha="0123456789abcdef", reason="probe still fails")
        self.assertEqual(captured.getvalue().splitlines(), [text])
        self.assertIn("BLOCKED", text)
        self.assertIn("an-item", text)
        self.assertIn("probe still fails", text)
        log = (self.root / ".agent-loop" / "notifications.log").read_text().splitlines()
        self.assertEqual(log, [text])

    def test_the_file_target_appends(self):
        targets = [NotifyTarget(kind="file", path=".agent-loop/notifications.log")]
        notify.notify(self.config, item="a", state=BLOCKED, sha="x", reason="one", targets=targets)
        notify.notify(self.config, item="a", state=BLOCKED, sha="x", reason="two", targets=targets)
        log = (self.root / ".agent-loop" / "notifications.log").read_text().splitlines()
        self.assertEqual(len(log), 2)

    def test_a_stateless_line_names_the_item_state_and_sha(self):
        line = notify.line(None, BLOCKED, "0123456789abcdef0", "no item")
        self.assertIn("-", line)
        self.assertIn("0123456789ab", line)
        self.assertNotIn("0123456789abcdef0", line)


if __name__ == "__main__":
    unittest.main()
