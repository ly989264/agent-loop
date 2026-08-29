"""The ``claude-code`` adapter: ``claude -p --output-format json``."""

from __future__ import annotations

import json
from typing import Any, Mapping

from ..config import Budget
from .base import Adapter, AgentResult, bounded_run, extract_json, schema_instruction


class ClaudeCodeAdapter(Adapter):
    name = "claude-code"

    def run(
        self,
        role: str,
        bundle: str,
        schema: Mapping[str, Any],
        sandbox: str,
        budget: Budget,
    ) -> AgentResult:
        self._check_sandbox(sandbox)
        argv = ["claude", "-p", "--output-format", "json"]
        if self.model:
            argv += ["--model", self.model]
        argv += ["--permission-mode", "plan" if sandbox == "read-only" else "acceptEdits"]
        prompt = "%s\n\n%s\n" % (bundle, schema_instruction(schema))
        status, returncode, tail = bounded_run(argv, stdin_text=prompt, budget=budget, cwd=self.cwd)
        if status == "timeout":
            return AgentResult("timeout", None, None, tail)
        if returncode != 0:
            return AgentResult("refused", None, None, tail)
        try:
            envelope = json.loads(tail)
        except ValueError:
            return AgentResult("malformed", None, None, tail)
        if not isinstance(envelope, dict) or "result" not in envelope:
            return AgentResult("malformed", None, None, tail)
        cost = envelope.get("total_cost_usd")
        if envelope.get("is_error"):
            return AgentResult("refused", None, cost, tail)
        parsed = extract_json(str(envelope["result"]))
        if parsed is None:
            return AgentResult("malformed", None, cost, tail)
        return AgentResult("ok", parsed, cost, tail)
