"""The publishers: what `github` runs, and what `local-only` does instead."""

import os
import subprocess
import unittest

from agent_loop import scm
from agent_loop.errors import ConfigError, InfraError

from support import cleanup, fake_gh, gh_calls, git_init, make_repo, origin_for

CREATED = "https://github.com/o/r/pull/7\n"
LISTED = '[{"url": "https://github.com/o/r/pull/7"}]'


class PublisherRegistryTest(unittest.TestCase):
    def test_the_two_publishers_are_registered_and_local_only_is_the_default(self):
        self.assertEqual(sorted(scm.REGISTRY), ["github", "local-only"])
        self.assertEqual(scm.DEFAULT, "local-only")

    def test_an_unknown_publisher_is_refused(self):
        with self.assertRaises(ConfigError):
            scm.build("gitlab")


class GitHubPublisherTest(unittest.TestCase):
    def setUp(self):
        self.path = os.environ["PATH"]
        self.root = make_repo()
        git_init(self.root)
        origin_for(self.root)
        subprocess.run(["git", "branch", "explore/an-item"], cwd=str(self.root), check=True)
        self.publisher = scm.build("github")

    def tearDown(self):
        os.environ["PATH"] = self.path
        cleanup(self.root)

    def publish(self, replies):
        fake_gh(self.root, replies)
        return self.publisher.publish(
            root=self.root, branch="explore/an-item", base="main",
            title="agent-loop: an-item", body="the body")

    def test_a_new_branch_is_pushed_and_gets_a_pull_request(self):
        publication = self.publish([{"match": ["list"], "out": "[]"},
                                    {"match": ["create"], "out": CREATED}])
        self.assertEqual(publication.pull_request.url, "https://github.com/o/r/pull/7")
        self.assertTrue(publication.pull_request.created)
        calls = gh_calls(self.root)
        self.assertEqual(
            calls[0]["argv"],
            ["pr", "list", "--head", "explore/an-item", "--base", "main",
             "--state", "open", "--json", "url"])
        self.assertEqual(
            calls[1]["argv"],
            ["pr", "create", "--base", "main", "--head", "explore/an-item",
             "--title", "agent-loop: an-item", "--body-file", "-"])
        self.assertEqual(calls[1]["stdin"], "the body")
        # the branch really reached origin, before any cleanup
        self.assertIn("explore/an-item", _branches(self.root.parent / (self.root.name + "-origin.git")))

    def test_a_branch_that_already_has_a_pull_request_is_updated_not_duplicated(self):
        publication = self.publish([{"match": ["list"], "out": LISTED}])
        self.assertEqual(publication.pull_request.url, "https://github.com/o/r/pull/7")
        self.assertFalse(publication.pull_request.created)
        argvs = [call["argv"] for call in gh_calls(self.root)]
        self.assertEqual(
            argvs[1],
            ["pr", "edit", "https://github.com/o/r/pull/7",
             "--title", "agent-loop: an-item", "--body-file", "-"])
        self.assertNotIn("create", [argv[1] for argv in argvs])

    def test_a_failing_gh_is_infrastructure_not_a_silent_success(self):
        with self.assertRaises(InfraError):
            self.publish([{"match": ["list"], "out": "boom", "code": 1}])

    def test_state_reads_the_raw_gh_state_and_is_open_is_derived_from_it(self):
        fake_gh(self.root, [{"match": ["view"], "out": '{"state": "MERGED"}'}])
        self.assertEqual(self.publisher.state(self.root, "https://github.com/o/r/pull/7"),
                         "MERGED")
        self.assertFalse(self.publisher.is_open(self.root, "https://github.com/o/r/pull/7"))

    def test_state_is_none_when_gh_cannot_answer(self):
        fake_gh(self.root, [{"match": ["view"], "out": "boom", "code": 1}])
        self.assertIsNone(self.publisher.state(self.root, "https://github.com/o/r/pull/7"))
        self.assertIsNone(self.publisher.is_open(self.root, "https://github.com/o/r/pull/7"))

    def test_a_comment_is_one_gh_call_and_reads_nothing_back_off_the_forge(self):
        # Invariant 4: no loop state on GitHub. The publisher posts what it is
        # given and never inspects the comments to decide whether to.
        fake_gh(self.root, [])
        pull = scm.PullRequest(url="https://github.com/o/r/pull/7", created=True)
        body = scm.review_comment([{"kind": "defect", "location": "a.py:1",
                                    "claim": "c", "citation": "cite"}])
        self.publisher.comment(self.root, pull, body)
        calls = gh_calls(self.root)
        self.assertEqual([call["argv"][:3] for call in calls],
                         [["pr", "comment", "https://github.com/o/r/pull/7"]])
        self.assertIn("**defect** at `a.py:1`", calls[0]["stdin"])
        self.assertNotIn("<!--", calls[0]["stdin"])


class LocalOnlyPublisherTest(unittest.TestCase):
    def test_it_opens_nothing_and_says_so(self):
        publisher = scm.build("local-only")
        publication = publisher.publish(
            root=None, branch="explore/x", base="main", title="t", body="b")
        self.assertIsNone(publication.pull_request)
        self.assertIn("no pull request", publication.reason)
        self.assertIsNone(publisher.is_open(None, "u"))
        self.assertIsNone(publisher.state(None, "u"))


class BodyTest(unittest.TestCase):
    def test_the_body_carries_the_item_the_reason_the_evidence_and_the_diffstat(self):
        body = scm.pr_body(
            item_id="an-item", statement="the statement",
            worker_reason="probe passes", 
            evidence={"reverted_command": "git stash", "observed_failure_line": "E assert"},
            ledger_line={"ts": "2026-08-29T00:00:00Z", "item": "an-item", "sha": "abc",
                         "state": "PR_READY", "cost": 1.25, "duration_s": 10.0},
            diff_stat=" a.py | 2 +-\n 1 file changed")
        for expected in ("## an-item", "the statement", "probe passes", "git stash",
                         "E assert", "a.py | 2 +-", "cost: 1.25", "sha: abc"):
            self.assertIn(expected, body)
        self.assertEqual(body, scm.pr_body(
            item_id="an-item", statement="the statement", worker_reason="probe passes",
            evidence={"reverted_command": "git stash", "observed_failure_line": "E assert"},
            ledger_line={"ts": "2026-08-29T00:00:00Z", "item": "an-item", "sha": "abc",
                         "state": "PR_READY", "cost": 1.25, "duration_s": 10.0},
            diff_stat=" a.py | 2 +-\n 1 file changed"))


def _branches(repo):
    return subprocess.run(["git", "branch", "--format=%(refname:short)"], cwd=str(repo),
                          stdout=subprocess.PIPE, universal_newlines=True).stdout.split()


if __name__ == "__main__":
    unittest.main()
