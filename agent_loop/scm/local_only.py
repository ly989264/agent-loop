"""The ``local-only`` publisher: no forge at all.

The default, and what every hermetic test runs under.  The round keeps its
``explore/`` branch in the local repository and the ledger line carries no
``pr_url``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from .base import Publication, Publisher, PullRequest

REASON = "publisher 'local-only' opens no pull request; the branch stays local"


class LocalOnlyPublisher(Publisher):
    name = "local-only"

    def publish(self, *, root: Path, branch: str, base: str, title: str, body: str) -> Publication:
        return Publication(pull_request=None, reason=REASON)

    def comment(self, root: Path, pull_request: PullRequest, body: str) -> bool:
        return False

    def merge(self, root: Path, pull_request: PullRequest) -> str:
        return REASON

    def is_open(self, root: Path, url: str) -> Optional[bool]:
        return None
