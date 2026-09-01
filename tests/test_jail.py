"""What the jail's `docker run` argv is, and is not."""

from __future__ import annotations

import contextlib
import io
import json
import os
import unittest
from pathlib import Path

from agent_loop import config as config_module, jail as jail_module, plan, round as round_module
from agent_loop.adapters import build
from agent_loop.config import AgentSpec, Budget
from agent_loop.environment import PINNED
from agent_loop.errors import ConfigError
from agent_loop.schemas import WORKER_OUTPUT_SCHEMA

from support import cleanup, git_init, make_repo, write_script


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


ANSWER = {
    "diff_applied": True,
    "test_path": "project/tests/test_thing.py",
    "mutation_evidence": {"reverted_command": "git stash && pytest",
                          "observed_failure_line": "E assert"},
    "status": "done",
    "reason": "",
}

# A `docker` that records its argv, then does inside the "container" what the
# jailed command would have done outside it.  No image is pulled and no daemon
# is spoken to, so the test is hermetic.
FAKE_DOCKER = """\
#!/usr/bin/env python3
import json, os, sys
argv = sys.argv[1:]
with open(os.environ["JAIL_RECORD"], "a") as handle:
    handle.write(json.dumps(argv) + "\\n")
if argv[0] == "kill":
    sys.exit(0)
sys.stdin.read()
open("project/fixed.txt", "w").write("fixed\\n")
print(%s)
"""

SLEEPING_DOCKER = """\
#!/usr/bin/env python3
import json, os, sys, time
argv = sys.argv[1:]
with open(os.environ["JAIL_RECORD"], "a") as handle:
    handle.write(json.dumps(argv) + "\\n")
if argv[0] == "kill":
    sys.exit(0)
sys.stdin.read()
time.sleep(120)
"""

JAIL_CONFIG = """
branch: main
backlog: .agent-loop/backlog.yaml
worktree_root: .agent-loop/worktrees
ledger: .agent-loop/ledger.jsonl
protected_paths:
  - project/catalog.json
verify:
  hermetic:
    cwd: project
    command: "true"
agents:
  worker:
    - shell:%s
caps:
  worker:
    wall_s: 60
    silence_s: 30
    max_tokens: 1000
notify:
  - target: file
    path: .agent-loop/notifications.log
levels:
  hermetic: L1
jail:
  image: jail:local
"""

JAIL_BACKLOG = """
items:
  - id: an-item
    group: g
    statement: the probe fails while this is open
    cost_class: hermetic
    selectable: true
    sites: []
    design_doc: "docs/design.md 1"
    probe: "test -f fixed.txt"
    proof: "the probe exits 0 once the file is there"
"""


class FakeDockerTest(unittest.TestCase):
    """Base: a `docker` on PATH that records what it was asked to run."""

    docker = FAKE_DOCKER % repr(json.dumps(ANSWER))

    def setUp(self):
        self.path = os.environ["PATH"]
        self.root = make_repo(config="branch: main\n", backlog=JAIL_BACKLOG)
        (self.root / "bin").mkdir()
        write_script(self.root, "bin/docker", self.docker)
        os.environ["PATH"] = "%s:%s" % (self.root / "bin", self.path)
        self.record = self.root / "docker-argv.jsonl"
        os.environ["JAIL_RECORD"] = str(self.record)

    def tearDown(self):
        os.environ["PATH"] = self.path
        os.environ.pop("JAIL_RECORD", None)
        cleanup(self.root)

    def recorded(self):
        return [json.loads(line) for line in self.record.read_text().splitlines()]


class JailedWorkerRoundTest(FakeDockerTest):
    def test_a_jailed_round_runs_the_worker_in_a_container_on_its_worktree_alone(self):
        script = write_script(self.root, "agent.py", "#!/bin/sh\ncat >/dev/null\n")
        (self.root / ".agent-loop" / "config.yaml").write_text(
            JAIL_CONFIG % script, encoding="utf-8")
        git_init(self.root)
        with contextlib.redirect_stdout(io.StringIO()):
            outcome = round_module.run_once(self.root / ".agent-loop" / "config.yaml")
        self.assertEqual(outcome.state, "PR_READY", outcome.reason)
        run = self.recorded()[0]
        worktree = (self.root / ".agent-loop" / "worktrees" / "an-item").resolve()
        self.assertEqual(
            [run[index + 1] for index, token in enumerate(run) if token == "--volume"],
            ["%s:/workspace" % worktree],
        )
        # the shell adapter's own argv, unchanged, after the image
        self.assertEqual(run[run.index("jail:local") + 1:], [str(script), "worker", "worktree-write"])

    def test_without_the_key_nothing_is_containerised(self):
        script = write_script(
            self.root, "agent.py",
            "#!/usr/bin/env python3\nimport sys\nsys.stdin.read()\n"
            'open("project/fixed.txt", "w").write("fixed\\n")\n'
            "print(%s)\n" % repr(json.dumps(ANSWER)),
        )
        (self.root / ".agent-loop" / "config.yaml").write_text(
            JAIL_CONFIG.replace("jail:\n  image: jail:local\n", "") % script, encoding="utf-8")
        git_init(self.root)
        with contextlib.redirect_stdout(io.StringIO()):
            outcome = round_module.run_once(self.root / ".agent-loop" / "config.yaml")
        self.assertEqual(outcome.state, "PR_READY", outcome.reason)
        self.assertFalse(self.record.exists())


class KillPathTest(FakeDockerTest):
    docker = SLEEPING_DOCKER

    def test_a_timed_out_jailed_command_is_killed_by_name_not_only_as_a_client(self):
        # `docker run` is a client; killing its process group would leave the
        # container running and the caps would bound nothing.
        script = write_script(self.root, "agent.py", "#!/bin/sh\ncat >/dev/null\n")
        adapter = build(
            AgentSpec.parse("shell:%s" % script),
            cwd=self.root,
            jail=jail_module.Jail(image="jail:local"),
        )
        result = adapter.run(
            "worker", "bundle", WORKER_OUTPUT_SCHEMA, "worktree-write",
            Budget(wall_s=30, silence_s=1, max_tokens=10),
        )
        self.assertEqual(result.status, "timeout")
        run, killed = self.recorded()
        name = run[run.index("--name") + 1]
        self.assertEqual(killed, ["kill", name])


class JailedProbeTest(FakeDockerTest):
    def test_a_plan_run_probe_goes_through_the_jail_at_the_verify_cwd(self):
        exit_code, output = jail_module.run_command(
            jail_module.Jail(image="jail:local"), "exit 3", self.root, "project", timeout=30
        )
        run = self.recorded()[0]
        self.assertEqual(
            [run[index + 1] for index, token in enumerate(run) if token == "--workdir"],
            ["/workspace/project"],
        )
        self.assertEqual(run[run.index("jail:local") + 1:], ["sh", "-c", "exit 3"])


    def test_a_plan_run_probes_a_proposal_inside_the_jail(self):
        # A proposal's probe is model-authored shell; the backlog's own probes
        # and verify's commands are the operator's data and stay host-side.
        (self.root / ".agent-loop" / "config.yaml").write_text(
            JAIL_CONFIG % "/bin/true", encoding="utf-8")
        config = config_module.load(self.root / ".agent-loop" / "config.yaml")
        judged = plan.admit(config, [], [
            {"id": "proposed", "statement": "s", "cost_class": "hermetic",
             "probe": "exit 3", "proof": "p", "sites": [], "rationale": "r"}])
        self.assertEqual(self.recorded()[0][-3:], ["sh", "-c", "exit 3"])
        self.assertEqual(judged[0]["probe_observed"]["cwd"], "project")

    def test_without_the_key_a_plan_run_probe_stays_host_side(self):
        (self.root / ".agent-loop" / "config.yaml").write_text(
            JAIL_CONFIG.replace("jail:\n  image: jail:local\n", "") % "/bin/true",
            encoding="utf-8")
        config = config_module.load(self.root / ".agent-loop" / "config.yaml")
        plan.admit(config, [], [
            {"id": "proposed", "statement": "s", "cost_class": "hermetic",
             "probe": "exit 3", "proof": "p", "sites": [], "rationale": "r"}])
        self.assertFalse(self.record.exists())


if __name__ == "__main__":
    unittest.main()
