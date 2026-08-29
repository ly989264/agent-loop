import unittest

from agent_loop.environment import BLOCKED_PREFIXES, agent_environment, is_blocked


class EnvironmentTest(unittest.TestCase):
    def test_credential_bearing_names_are_stripped(self):
        source = {
            "GH_TOKEN": "x",
            "GITHUB_TOKEN": "x",
            "AWS_SECRET_ACCESS_KEY": "x",
            "AZURE_CLIENT_SECRET": "x",
            "GOOGLE_APPLICATION_CREDENTIALS": "x",
            "VALKEY_REAL_OPT_IN": "1",
            "MILESTONE_LEASE_ID": "x",
            "ACTIONS_ID_TOKEN_REQUEST_URL": "x",
            "VSLAB_M2_RUN_ID": "x",
            "SSH_AUTH_SOCK": "/tmp/agent.sock",
            "PATH": "/usr/bin",
        }
        stripped = agent_environment(source)
        for name in source:
            if name == "PATH":
                continue
            self.assertNotIn(name, stripped, name)
        self.assertEqual(stripped["PATH"], "/usr/bin")

    def test_git_is_pinned_so_no_credential_helper_can_run(self):
        stripped = agent_environment({"PATH": "/usr/bin"})
        self.assertEqual(stripped["GIT_CONFIG_GLOBAL"], "/dev/null")
        self.assertEqual(stripped["GIT_CONFIG_VALUE_0"], "")
        self.assertEqual(stripped["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(stripped["GIT_ASKPASS"], "/usr/bin/false")

    def test_every_blocked_prefix_is_recognised(self):
        for prefix in BLOCKED_PREFIXES:
            self.assertTrue(is_blocked(prefix + "ANYTHING"))
        self.assertFalse(is_blocked("HOME"))


if __name__ == "__main__":
    unittest.main()
