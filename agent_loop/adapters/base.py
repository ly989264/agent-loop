"""The adapter contract, and the three things every adapter inherits.

``run(role, bundle, schema, sandbox, budget) -> AgentResult``.  Environment
stripping, the bounded bundle and the single repair round-trip are kernel-side, so
an adapter is only a way of starting a process and reading its answer.

The bounded process runner is ported from valkey_scale_lab
``.github/milestone-loop/agent.py:invoke``: its own process group, a wall-clock
cap, a silence cap, and a bounded tail kept for the ledger and the notification.
"""

from __future__ import annotations

import json
import os
import selectors
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from ..config import Budget
from ..environment import agent_environment
from ..schemas import validate

SANDBOXES = ("read-only", "worktree-write")
TAIL_LINES = 200
TAIL_LINE_BYTES = 64_000  # a streamed result envelope with modelUsage passes 4 KB
REPAIR_ECHO_BYTES = 8_192


@dataclass(frozen=True)
class AgentResult:
    status: str  # ok | malformed | timeout | refused
    json: Optional[Dict[str, Any]]
    cost: Optional[float]
    raw_tail: str


def _terminate(process: "subprocess.Popen") -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        process.poll()  # reap a child that has already exited
        return
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        process.wait()


def bounded_run(
    argv: Sequence[str],
    *,
    stdin_text: str,
    budget: Budget,
    cwd: Path,
) -> Tuple[str, int, str]:
    """Run ``argv`` under the budget.  Returns (status, returncode, raw_tail).

    Output is read a chunk at a time from the raw descriptor, never with
    ``readline``: a child that writes an unterminated line and then goes quiet
    blocks a line read, and both caps would sleep through the whole silence.
    """
    try:
        process = subprocess.Popen(
            list(argv),
            cwd=str(cwd),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=agent_environment(),
            start_new_session=True,
        )
    except OSError as exc:
        return "refused", -1, "cannot start %s: %s" % (argv[0], exc)
    started = time.monotonic()
    last_event = started
    lines: List[str] = []
    pending = b""

    def keep(text: str) -> None:
        lines.append(text.rstrip()[:TAIL_LINE_BYTES])
        if len(lines) > TAIL_LINES:
            del lines[:-TAIL_LINES]

    def tail() -> str:
        return "\n".join(lines + ([pending.decode("utf-8", "replace")] if pending else []))

    selector = selectors.DefaultSelector()
    try:
        try:
            process.stdin.write(stdin_text.encode("utf-8"))
            process.stdin.close()
        except OSError as exc:
            # An agent binary that exits before reading its bundle breaks the
            # pipe; that is a refusal to answer, not a crash of the round.
            _terminate(process)
            return "refused", -1, "%s did not read its bundle: %s" % (argv[0], exc)
        descriptor = process.stdout.fileno()
        selector.register(descriptor, selectors.EVENT_READ)
        while True:
            now = time.monotonic()
            if now - started > budget.wall_s or now - last_event > budget.silence_s:
                _terminate(process)
                return "timeout", -1, tail()
            if not selector.select(timeout=0.5):
                if process.poll() is not None:
                    break
                continue
            chunk = os.read(descriptor, 65_536)
            if not chunk:
                break
            last_event = time.monotonic()
            pending += chunk
            while b"\n" in pending:
                line, _, pending = pending.partition(b"\n")
                keep(line.decode("utf-8", "replace"))
        while process.poll() is None:
            if time.monotonic() - started > budget.wall_s:
                _terminate(process)
                return "timeout", -1, tail()
            time.sleep(0.05)
    except BaseException:
        _terminate(process)
        raise
    finally:
        selector.close()
        for stream in (process.stdin, process.stdout):
            if stream is not None and not stream.closed:
                stream.close()
    return "ok", process.returncode, tail()


def extract_json(text: str) -> Optional[Dict[str, Any]]:
    """Read one JSON object out of an agent's final message."""
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = candidate.split("\n", 1)[-1]
        if candidate.endswith("```"):
            candidate = candidate[: -len("```")]
        candidate = candidate.strip()
    first, last = candidate.find("{"), candidate.rfind("}")
    if first < 0 or last <= first:
        return None
    try:
        parsed = json.loads(candidate[first : last + 1])
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


class Adapter:
    """Base class; ``name`` is what the config's agent string selects."""

    name = ""

    def __init__(self, model: Optional[str] = None, cwd: Optional[Path] = None) -> None:
        self.model = model
        self.cwd = Path(cwd) if cwd is not None else Path.cwd()

    def run(
        self,
        role: str,
        bundle: str,
        schema: Mapping[str, Any],
        sandbox: str,
        budget: Budget,
    ) -> AgentResult:
        raise NotImplementedError

    @staticmethod
    def _check_sandbox(sandbox: str) -> None:
        if sandbox not in SANDBOXES:
            raise ValueError("sandbox must be one of %s" % (SANDBOXES,))


def schema_instruction(schema: Mapping[str, Any]) -> str:
    return (
        "Reply with one JSON object and nothing else. It must validate against "
        "this JSON schema:\n" + json.dumps(schema, indent=2, sort_keys=True)
    )


def _validated(result: AgentResult, schema: Mapping[str, Any]) -> AgentResult:
    """An ``ok`` answer that does not fit the schema is malformed, not an answer."""
    if result.status != "ok":
        return result
    error = validate(schema, result.json)
    if error is None:
        return result
    return AgentResult("malformed", None, result.cost, result.raw_tail + "\nschema: " + error)


def invoke_with_one_repair(
    adapter: Adapter,
    *,
    role: str,
    bundle: str,
    schema: Mapping[str, Any],
    sandbox: str,
    budget: Budget,
) -> AgentResult:
    """Run the adapter; on ``malformed``, allow exactly one repair round-trip."""
    first = _validated(adapter.run(role, bundle, schema, sandbox, budget), schema)
    if first.status != "malformed":
        return first
    repair = (
        "\n\nYour previous final output was rejected as malformed. Correct only the "
        "protocol error and reply with the JSON object alone.\nPrevious output "
        "(bounded):\n" + first.raw_tail[-REPAIR_ECHO_BYTES:]
    )
    return _validated(adapter.run(role, bundle + repair, schema, sandbox, budget), schema)
