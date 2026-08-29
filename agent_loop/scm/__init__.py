"""Publisher registry and dispatch."""

from __future__ import annotations

from typing import Dict, Type

from ..errors import ConfigError
from .base import Publication, Publisher, PullRequest, pr_body, review_comment
from .github import GitHubPublisher
from .local_only import LocalOnlyPublisher

REGISTRY: Dict[str, Type[Publisher]] = {
    GitHubPublisher.name: GitHubPublisher,
    LocalOnlyPublisher.name: LocalOnlyPublisher,
}
DEFAULT = LocalOnlyPublisher.name

__all__ = [
    "DEFAULT",
    "Publication",
    "Publisher",
    "PullRequest",
    "REGISTRY",
    "build",
    "pr_body",
    "review_comment",
]


def build(name: str) -> Publisher:
    if name not in REGISTRY:
        raise ConfigError(
            "unknown scm publisher %r; known publishers are %s" % (name, sorted(REGISTRY))
        )
    return REGISTRY[name]()
