"""What the jail's `docker run` argv is, and is not."""

from __future__ import annotations

import os
import unittest
from pathlib import Path

from agent_loop import jail as jail_module
from agent_loop.environment import PINNED
from agent_loop.errors import ConfigError


def flags(argv, flag):
    return [argv[index + 1] for index, token in enumerate(argv) if token == flag]


class DockerArgvTest(unittest.TestCase):
    def setUp(self):
        self.jail = jail_module.Jail(image="jail:local", credentials_env=("A_KEY",), memory="4g")
        self.argv = jail_module.docker_argv(
            self.jail,
            ["claude", "-p"],
            mount=Path("/tmp"),
            name="agent-loop-abc",
            environ={"A_KEY": "secret", "HOME": "/Users/someone"},
        )

    def test_it_is_a_docker_run_of_the_configured_image_with_the_command_last(self):
        self.assertEqual(self.argv[:3], ["docker", "run", "--rm"])
        self.assertEqual(self.argv[-3:], ["jail:local", "claude", "-p"])
        self.assertIn("--init", self.argv)
        self.assertEqual(flags(self.argv, "--name"), ["agent-loop-abc"])
        self.assertEqual(flags(self.argv, "--memory"), ["4g"])
        self.assertEqual(flags(self.argv, "--pids-limit"), [str(jail_module.PIDS_LIMIT)])

    def test_exactly_one_mount_and_it_is_the_tree_at_the_working_directory(self):
        self.assertEqual(
            flags(self.argv, "--volume"), ["%s:/workspace" % Path("/tmp").resolve()]
        )
        self.assertEqual(flags(self.argv, "--workdir"), ["/workspace"])
        for flag in ("-v", "--mount", "--volumes-from", "--privileged", "--user"):
            self.assertNotIn(flag, self.argv)

    def test_no_docker_socket_and_no_host_home_reach_the_container(self):
        joined = " ".join(self.argv)
        self.assertNotIn("docker.sock", joined)
        self.assertNotIn("/Users/someone", joined)
        self.assertNotIn(os.path.expanduser("~"), " ".join(flags(self.argv, "--volume")))

    def test_the_environment_is_the_pinned_settings_and_the_named_credentials(self):
        # `--env NAME` (no value) takes it from the client's own environment, so
        # nothing secret is an argument; PINNED carries values and no secret.
        env = flags(self.argv, "--env")
        self.assertEqual(
            sorted(env), sorted(["%s=%s" % item for item in PINNED.items()] + ["A_KEY"])
        )
        self.assertNotIn("secret", " ".join(self.argv))

    def test_a_credential_the_host_does_not_set_is_not_passed(self):
        argv = jail_module.docker_argv(
            self.jail, ["true"], mount=Path("/tmp"), name="n", environ={}
        )
        self.assertNotIn("A_KEY", flags(argv, "--env"))

    def test_a_relative_cwd_becomes_a_directory_under_the_one_mount(self):
        for cwd, expected in ((".", "/workspace"), ("", "/workspace"), ("project", "/workspace/project")):
            argv = jail_module.docker_argv(
                self.jail, ["true"], mount=Path("/tmp"), name="n",
                workdir=jail_module.WORKDIR if cwd in ("", ".") else "/workspace/" + cwd,
                environ={},
            )
            self.assertEqual(flags(argv, "--workdir"), [expected])

    def test_each_container_gets_its_own_name(self):
        self.assertNotEqual(jail_module.container_name(), jail_module.container_name())
        self.assertTrue(jail_module.container_name().startswith(jail_module.NAME_PREFIX))


class ParseTest(unittest.TestCase):
    def test_absent_is_none_and_a_bare_image_is_enough(self):
        self.assertIsNone(jail_module.parse(None))
        jail = jail_module.parse({"image": "x:1"})
        self.assertEqual((jail.image, jail.credentials_env, jail.memory), ("x:1", (), None))

    def test_the_shapes_it_refuses(self):
        for value, expected in (
            ("x:1", "mapping"),
            ({"image": ""}, "image"),
            ({"image": "x", "credentials_env": "A"}, "credentials_env"),
            ({"image": "x", "memory": 4}, "memory"),
        ):
            with self.assertRaises(ConfigError) as caught:
                jail_module.parse(value)
            self.assertIn(expected, str(caught.exception))


if __name__ == "__main__":
    unittest.main()
