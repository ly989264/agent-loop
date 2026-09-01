"""Adapter registry and dispatch."""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Optional, Sequence, Type

from .. import jail as jail_module
from ..config import AgentSpec
from ..errors import ConfigError
from .base import Adapter, AgentResult, allowed_tools, invoke_with_one_repair
from .claude_code import ClaudeCodeAdapter
from .codex import CodexAdapter
from .shell import ShellAdapter

REGISTRY: Dict[str, Type[Adapter]] = {
    ClaudeCodeAdapter.name: ClaudeCodeAdapter,
    CodexAdapter.name: CodexAdapter,
    ShellAdapter.name: ShellAdapter,
}

__all__ = ["Adapter", "AgentResult", "REGISTRY", "allowed_tools", "build", "invoke_with_one_repair"]


def build(
    spec: AgentSpec,
    cwd: Optional[Path] = None,
    allowed_tools: Sequence[str] = (),
    jail: Optional[jail_module.Jail] = None,
) -> Adapter:
    if spec.adapter not in REGISTRY:
        raise ConfigError(
            "unknown adapter %r; known adapters are %s"
            % (spec.adapter, sorted(REGISTRY))
        )
    return REGISTRY[spec.adapter](
        model=spec.model, cwd=cwd, allowed_tools=allowed_tools, jail=jail
    )
