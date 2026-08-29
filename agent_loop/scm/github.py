"""The ``github`` publisher: ``git push`` plus ``gh``, and read-only otherwise.

It writes exactly three things - the branch, the pull request, one comment -
and merges only when the level logic says to.  No labels, no control issues,
no markers beyond the one that keeps the review comment from being posted
twice (invariant 4: no loop state on GitHub except the pull request itself).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from ..errors import InfraError
from .base import Publication, Publisher, PullRequest, REVIEW_MARKER, run


class GitHubPublisher(Publisher):
    name = "github"

    def publish(
        self, *, root: Path, branch: str, base: str, title: str, body: str
    ) -> Publication:
        code, output = run(["git", "push", "--force-with-lease", "origin", branch], cwd=root)
        if code != 0:
            raise InfraError("cannot push %s: %s" % (branch, output.strip()[-800:]))
        existing = self._open_pull_request(root, branch, base)
        if existing is not None:
            # The branch already has a pull request: update it rather than
            # opening a second one for the same item.
            code, output = run(
                ["gh", "pr", "edit", existing.url, "--title", title, "--body-file", "-"],
                cwd=root,
                input_text=body,
            )
            if code != 0:
                raise InfraError("cannot update %s: %s" % (existing.url, output.strip()[-800:]))
            return Publication(pull_request=existing, reason="updated an existing pull request")
        code, output = run(
            ["gh", "pr", "create", "--base", base, "--head", branch,
             "--title", title, "--body-file", "-"],
            cwd=root,
            input_text=body,
        )
        if code != 0:
            raise InfraError("cannot open a pull request: %s" % output.strip()[-800:])
        url = next(
            (line.strip() for line in reversed(output.splitlines())
             if line.strip().startswith("http")),
            "",
        )
        if not url:
            raise InfraError("gh pr create printed no URL: %s" % output.strip()[-800:])
        return Publication(pull_request=PullRequest(url=url, created=True), reason="opened")

    def _open_pull_request(self, root: Path, branch: str, base: str) -> Optional[PullRequest]:
        code, output = run(
            ["gh", "pr", "list", "--head", branch, "--base", base,
             "--state", "open", "--json", "url"],
            cwd=root,
        )
        if code != 0:
            raise InfraError("cannot list pull requests for %s: %s" % (branch, output.strip()[-800:]))
        try:
            listed = json.loads(output or "[]")
        except ValueError:
            raise InfraError("gh pr list did not answer JSON: %s" % output.strip()[-800:])
        if not listed:
            return None
        return PullRequest(url=str(listed[0]["url"]), created=False)

    def comment(self, root: Path, pull_request: PullRequest, body: str) -> bool:
        code, output = run(
            ["gh", "pr", "view", pull_request.url, "--json", "comments"], cwd=root
        )
        if code != 0:
            raise InfraError("cannot read comments on %s: %s" % (pull_request.url, output.strip()[-800:]))
        try:
            comments = json.loads(output or "{}").get("comments") or []
        except ValueError:
            raise InfraError("gh pr view did not answer JSON: %s" % output.strip()[-800:])
        if any(REVIEW_MARKER in str(comment.get("body", "")) for comment in comments):
            return False
        code, output = run(
            ["gh", "pr", "comment", pull_request.url, "--body-file", "-"],
            cwd=root,
            input_text=body,
        )
        if code != 0:
            raise InfraError("cannot comment on %s: %s" % (pull_request.url, output.strip()[-800:]))
        return True

    def merge(self, root: Path, pull_request: PullRequest) -> str:
        code, output = run(["gh", "pr", "merge", pull_request.url, "--squash"], cwd=root)
        return "" if code == 0 else output.strip()[-800:]

    def is_open(self, root: Path, url: str) -> Optional[bool]:
        code, output = run(["gh", "pr", "view", url, "--json", "state"], cwd=root)
        if code != 0:
            return None
        try:
            return json.loads(output or "{}").get("state") == "OPEN"
        except ValueError:
            return None
