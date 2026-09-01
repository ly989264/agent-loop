"""The ``claude-code`` adapter: ``claude -p --output-format stream-json``.

``--output-format json`` prints nothing until the agent is finished, so the
silence cap could only ever kill a healthy worker (measured: 300 s of empty
output on the first real round).  ``stream-json`` emits one line per event -
init, each assistant turn, the final ``result`` envelope - so silence means
silence, and the answer is the last line whose ``type`` is ``result``.
"""

from __future__ import annotations

import json
from typing import Any, Mapping

from ..config import Budget
from .base import Adapter, AgentResult, extract_json, schema_instruction


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
        argv = ["claude", "-p", "--output-format", "stream-json", "--verbose"]
        if self.model:
            argv += ["--model", self.model]
        argv += ["--permission-mode", "plan" if sandbox == "read-only" else "acceptEdits"]
        if sandbox == "worktree-write" and self.allowed_tools:
            # acceptEdits grants edits and denies every Bash command in -p mode,
            # so the commands the round itself will run are named here.
            argv += ["--allowedTools", ",".join(self.allowed_tools)]
        prompt = "%s\n\n%s\n" % (bundle, schema_instruction(schema))
        status, returncode, tail = self.bounded(argv, stdin_text=prompt, budget=budget)
        if status == "timeout":
            return AgentResult("timeout", None, None, tail)
        if returncode != 0:
            return AgentResult("refused", None, None, tail)
        envelope = _result_envelope(tail)
        if envelope is None:
            return AgentResult("malformed", None, None, tail)
        cost = envelope.get("total_cost_usd")
        if envelope.get("is_error"):
            return AgentResult("refused", None, cost, tail)
        parsed = extract_json(str(envelope["result"]))
        if parsed is None:
            return AgentResult("malformed", None, cost, tail)
        return AgentResult("ok", parsed, cost, tail)


def _result_envelope(tail: str):
    """The last ``type: result`` event in the streamed output, or None."""
    for line in reversed(tail.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("type") == "result" and "result" in event:
            return event
    return None
