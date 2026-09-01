"""The ``shell`` adapter: bundle on stdin, one JSON object on stdout.

Configured as ``shell:<program>`` - how a self-hosted model plugs in.  The role
and sandbox reach the program as arguments, since it has no other channel.
"""

from __future__ import annotations

from typing import Any, Mapping

from ..config import Budget
from .base import Adapter, AgentResult, extract_json, schema_instruction


class ShellAdapter(Adapter):
    name = "shell"

    def run(
        self,
        role: str,
        bundle: str,
        schema: Mapping[str, Any],
        sandbox: str,
        budget: Budget,
    ) -> AgentResult:
        self._check_sandbox(sandbox)
        if not self.model:
            return AgentResult("refused", None, None, "shell adapter needs shell:<program>")
        argv = [self.model, role, sandbox]
        stdin_text = "%s\n\n%s\n" % (bundle, schema_instruction(schema))
        status, returncode, tail = self.bounded(argv, stdin_text=stdin_text, budget=budget)
        if status == "timeout":
            return AgentResult("timeout", None, None, tail)
        if returncode != 0:
            return AgentResult("refused", None, None, tail)
        parsed = extract_json(tail)
        if parsed is None:
            return AgentResult("malformed", None, None, tail)
        return AgentResult("ok", parsed, None, tail)
