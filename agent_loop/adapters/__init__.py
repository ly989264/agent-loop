"""Adapter registry and dispatch."""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Optional, Type

from ..config import AgentSpec
from ..errors import ConfigError
from .base import Adapter, AgentResult, invoke_with_one_repair
from .claude_code import ClaudeCodeAdapter
from .codex import CodexAdapter
from .shell import ShellAdapter

REGISTRY: Dict[str, Type[Adapter]] = {
    ClaudeCodeAdapter.name: ClaudeCodeAdapter,
    CodexAdapter.name: CodexAdapter,
    ShellAdapter.name: ShellAdapter,
}

__all__ = ["Adapter", "AgentResult", "REGISTRY", "build", "invoke_with_one_repair"]


def build(spec: AgentSpec, cwd: Optional[Path] = None) -> Adapter:
    if spec.adapter not in REGISTRY:
        raise ConfigError(
            "unknown adapter %r; known adapters are %s"
            % (spec.adapter, sorted(REGISTRY))
        )
    return REGISTRY[spec.adapter](model=spec.model, cwd=cwd)
