import json
import unittest

from agent_loop.adapters import REGISTRY, build, invoke_with_one_repair
from agent_loop.config import AgentSpec, Budget
from agent_loop.errors import ConfigError
from agent_loop.schemas import WORKER_OUTPUT_SCHEMA

from support import cleanup, make_repo, write_script

ANSWER = {
    "diff_applied": True,
    "test_path": "tests/unit/test_thing.py",
    "mutation_evidence": {
        "reverted_command": "git stash && pytest tests/unit/test_thing.py",
        "observed_failure_line": "assert 0 == 1",
    },
    "status": "done",
    "reason": "",
}

COUNTING_SCRIPT = """\
#!/usr/bin/env python3
import json, os, sys
sys.stdin.read()
counter = os.path.join(os.path.dirname(os.path.abspath(__file__)), "calls")
with open(counter, "a") as handle:
    handle.write(sys.argv[1] + " " + sys.argv[2] + "\\n")
with open(counter) as handle:
    calls = len(handle.read().splitlines())
if calls <= %d:
    print("I am not JSON at all")
else:
    print(%s)
"""


class AdapterDispatchTest(unittest.TestCase):
    def test_the_three_adapters_are_registered(self):
        self.assertEqual(sorted(REGISTRY), ["claude-code", "codex", "shell"])

    def test_a_spec_selects_its_adapter_and_model(self):
        adapter = build(AgentSpec.parse("claude-code:opus-5"))
        self.assertEqual(adapter.name, "claude-code")
        self.assertEqual(adapter.model, "opus-5")

    def test_an_unknown_adapter_is_refused(self):
        with self.assertRaises(ConfigError):
            build(AgentSpec.parse("gemini"))


class ShellAdapterTest(unittest.TestCase):
    def setUp(self):
        self.root = make_repo()
        self.budget = Budget(wall_s=30, silence_s=15, max_tokens=1000)

    def tearDown(self):
        cleanup(self.root)

    def adapter(self, malformed_calls=0):
        script = write_script(
            self.root, "fake_agent.py",
            COUNTING_SCRIPT % (malformed_calls, repr(json.dumps(ANSWER))))
        return build(AgentSpec.parse("shell:%s" % script), cwd=self.root)

    def calls(self):
        return (self.root / "calls").read_text().splitlines()

    def test_the_bundle_goes_in_and_json_comes_back(self):
        result = self.adapter().run("worker", "bundle", WORKER_OUTPUT_SCHEMA, "worktree-write", self.budget)
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.json, ANSWER)
        self.assertEqual(self.calls(), ["worker worktree-write"])

    def test_a_non_zero_exit_is_refused(self):
        script = write_script(self.root, "refuse.py",
                              "#!/usr/bin/env python3\nimport sys\nsys.stdin.read()\nsys.exit(4)\n")
        adapter = build(AgentSpec.parse("shell:%s" % script), cwd=self.root)
        result = adapter.run("worker", "b", WORKER_OUTPUT_SCHEMA, "read-only", self.budget)
        self.assertEqual(result.status, "refused")

    def test_a_silent_agent_times_out(self):
        script = write_script(self.root, "sleep.py",
                              "#!/usr/bin/env python3\nimport sys, time\nsys.stdin.read()\ntime.sleep(30)\n")
        adapter = build(AgentSpec.parse("shell:%s" % script), cwd=self.root)
        result = adapter.run("worker", "b", WORKER_OUTPUT_SCHEMA, "read-only",
                             Budget(wall_s=2, silence_s=2, max_tokens=10))
        self.assertEqual(result.status, "timeout")

    def test_an_unknown_sandbox_is_a_programming_error(self):
        with self.assertRaises(ValueError):
            self.adapter().run("worker", "b", WORKER_OUTPUT_SCHEMA, "anything", self.budget)


class OneRepairTest(unittest.TestCase):
    def setUp(self):
        self.root = make_repo()
        self.budget = Budget(wall_s=30, silence_s=15, max_tokens=1000)

    def tearDown(self):
        cleanup(self.root)

    def adapter(self, malformed_calls):
        script = write_script(
            self.root, "fake_agent.py",
            COUNTING_SCRIPT % (malformed_calls, repr(json.dumps(ANSWER))))
        return build(AgentSpec.parse("shell:%s" % script), cwd=self.root)

    def invoke(self, adapter):
        return invoke_with_one_repair(
            adapter, role="worker", bundle="bundle", schema=WORKER_OUTPUT_SCHEMA,
            sandbox="worktree-write", budget=self.budget)

    def calls(self):
        return (self.root / "calls").read_text().splitlines()

    def test_a_malformed_answer_gets_exactly_one_repair(self):
        result = self.invoke(self.adapter(malformed_calls=1))
        self.assertEqual(result.status, "ok")
        self.assertEqual(len(self.calls()), 2)

    def test_a_second_malformed_answer_is_not_repaired_again(self):
        result = self.invoke(self.adapter(malformed_calls=99))
        self.assertEqual(result.status, "malformed")
        self.assertEqual(len(self.calls()), 2)

    def test_a_well_formed_answer_is_not_repaired(self):
        result = self.invoke(self.adapter(malformed_calls=0))
        self.assertEqual(result.status, "ok")
        self.assertEqual(len(self.calls()), 1)

    def test_valid_json_that_misses_the_schema_is_malformed(self):
        script = write_script(
            self.root, "wrong.py",
            "#!/usr/bin/env python3\nimport sys\nsys.stdin.read()\nprint('{\"status\": \"done\"}')\n")
        adapter = build(AgentSpec.parse("shell:%s" % script), cwd=self.root)
        result = self.invoke(adapter)
        self.assertEqual(result.status, "malformed")
        self.assertIn("missing required key", result.raw_tail)


if __name__ == "__main__":
    unittest.main()
