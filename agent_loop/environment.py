"""Per-role environment stripping, inherited by every adapter.

Ported from valkey_scale_lab ``.github/milestone-loop/agent.py:_agent_environment``:
the same blocklist shape (credential-bearing prefixes plus ``SSH_AUTH_SOCK``) and
the same pinned git settings, so an agent cannot reach a forge, a cloud account or
the operator's git credentials from inside a round.  The list holds forge and
cloud names only; a consumer's own variable names are that consumer's, and no
kernel-side list may carry them.
"""

from __future__ import annotations

import os
from typing import Dict, Mapping, Optional

BLOCKED_PREFIXES = (
    "GH_",
    "GITHUB_",
    "AWS_",
    "AZURE_",
    "GOOGLE_",
    "ACTIONS_ID_TOKEN_",
)
BLOCKED_NAMES = frozenset({"SSH_AUTH_SOCK"})

PINNED = {
    "NO_COLOR": "1",
    "PYTHONDONTWRITEBYTECODE": "1",
    "GIT_CONFIG_COUNT": "2",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_KEY_0": "credential.helper",
    "GIT_CONFIG_VALUE_0": "",
    "GIT_CONFIG_KEY_1": "credential.interactive",
    "GIT_CONFIG_VALUE_1": "false",
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_ASKPASS": "/usr/bin/false",
}


def is_blocked(name: str) -> bool:
    return name.startswith(BLOCKED_PREFIXES) or name in BLOCKED_NAMES


def agent_environment(source: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    """Return the environment an adapter's subprocess may see."""
    base = os.environ if source is None else source
    allowed = {key: value for key, value in base.items() if not is_blocked(key)}
    allowed.update(PINNED)
    return allowed
