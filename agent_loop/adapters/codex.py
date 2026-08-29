"""The ``codex`` adapter: ``codex exec --output-schema``."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any, Mapping

from ..config import Budget
from .base import Adapter, AgentResult, bounded_run, extract_json


class CodexAdapter(Adapter):
    name = "codex"

    def run(
        self,
        role: str,
        bundle: str,
        schema: Mapping[str, Any],
        sandbox: str,
        budget: Budget,
    ) -> AgentResult:
        self._check_sandbox(sandbox)
        workspace = Path(tempfile.mkdtemp(prefix="agent-loop-codex-"))
        schema_path = workspace / "schema.json"
        message_path = workspace / "last-message.txt"
        schema_path.write_text(json.dumps(schema, indent=2, sort_keys=True), encoding="utf-8")
        argv = [
            "codex",
            "exec",
            "--ephemeral",
            "--ignore-user-config",
            "--sandbox",
            "read-only" if sandbox == "read-only" else "workspace-write",
            "--cd",
            str(self.cwd),
            "--output-schema",
            str(schema_path),
            "--output-last-message",
            str(message_path),
        ]
        if self.model:
            argv += ["--model", self.model]
        argv += ["-"]
        status, returncode, tail = bounded_run(argv, stdin_text=bundle, budget=budget, cwd=self.cwd)
        if status == "timeout":
            return AgentResult("timeout", None, None, tail)
        if returncode != 0:
            return AgentResult("refused", None, None, tail)
        try:
            message = message_path.read_text(encoding="utf-8")
        except OSError:
            return AgentResult("malformed", None, None, tail)
        parsed = extract_json(message)
        if parsed is None:
            return AgentResult("malformed", None, None, tail)
        return AgentResult("ok", parsed, None, tail)
