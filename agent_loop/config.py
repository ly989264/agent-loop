"""Loader for a consumer repository's ``.agent-loop/config.yaml``.

The kernel is generic; every project-specific fact lives in this file.  Only the
keys below exist: anything else is a configuration error, so a typo is reported
rather than silently ignored.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

import yaml

from . import scm as scm_module
from .errors import ConfigError

KEYS = frozenset(
    {
        "branch",
        "backlog",
        "worktree_root",
        "ledger",
        "protected_paths",
        "verify",
        "agents",
        "caps",
        "notify",
        "levels",
        "scm",
        "plan_sources",
    }
)
ROLES = ("planner", "worker", "reviewer", "diagnoser")
# `levels` is keyed by cost class, and the planner is not one: it is a role.
# L3 is that role's level - ROADMAP.md §3 "the loop also admits backlog items" -
# so it lives under this reserved key, which no cost class may use.
PLANNER = "planner"
# Continuous-mode caps that live beside the per-role budgets in `caps`, keyed by
# name instead of role so a typo'd role name still trips "unknown role", not a
# silently-ignored cap.
CAP_DEFAULTS: Dict[str, int] = {
    "open_prs": 3,
    "non_progress_rounds": 5,
    "poll_s": 30,
    "idle_s": 900,
    "round_wall_s": 3600,
}


@dataclass(frozen=True)
class Budget:
    wall_s: int
    silence_s: int
    max_tokens: int


@dataclass(frozen=True)
class AgentSpec:
    """One rung of a role's escalation ladder, e.g. ``claude-code:opus-5``."""

    adapter: str
    model: Optional[str] = None

    @classmethod
    def parse(cls, text: str) -> "AgentSpec":
        if not isinstance(text, str) or not text.strip():
            raise ConfigError("an agent entry must be a non-empty string")
        adapter, _, model = text.partition(":")
        return cls(adapter=adapter.strip(), model=model.strip() or None)

    def __str__(self) -> str:
        return self.adapter if self.model is None else "%s:%s" % (self.adapter, self.model)


@dataclass(frozen=True)
class VerifyCommand:
    """A shell command plus the directory, relative to the tree, to run it in."""

    command: str
    cwd: str = "."


@dataclass(frozen=True)
class NotifyTarget:
    kind: str
    path: Optional[str] = None


@dataclass(frozen=True)
class Config:
    root: Path
    branch: str
    backlog: Path
    worktree_root: Path
    ledger: Path
    protected_paths: Tuple[str, ...]
    verify: Mapping[str, VerifyCommand]
    agents: Mapping[str, Tuple[AgentSpec, ...]]
    caps: Mapping[str, Budget]
    notify: Tuple[NotifyTarget, ...]
    levels: Mapping[str, str] = field(default_factory=dict)
    scm: str = scm_module.DEFAULT
    # File globs `agent-loop plan` may read into the planner's bundle, read-only.
    plan_sources: Tuple[str, ...] = ()
    # Continuous mode; §3 "Modes and back-pressure". Defaults in CAP_DEFAULTS.
    open_prs: int = CAP_DEFAULTS["open_prs"]
    non_progress_rounds: int = CAP_DEFAULTS["non_progress_rounds"]
    poll_s: int = CAP_DEFAULTS["poll_s"]
    idle_s: int = CAP_DEFAULTS["idle_s"]
    round_wall_s: int = CAP_DEFAULTS["round_wall_s"]

    def ladder(self, role: str) -> Tuple[AgentSpec, ...]:
        if role not in self.agents:
            raise ConfigError("no agent configured for role %r" % role)
        return self.agents[role]

    def budget(self, role: str) -> Budget:
        if role not in self.caps:
            raise ConfigError("no caps configured for role %r" % role)
        return self.caps[role]

    def level(self, cost_class: str) -> str:
        """The autonomy level for this cost class; L1 unless the config says otherwise."""
        return self.levels.get(cost_class, "L1")

    def verify_for(self, cost_class: str) -> VerifyCommand:
        if cost_class not in self.verify:
            raise ConfigError("no verify command configured for cost class %r" % cost_class)
        return self.verify[cost_class]


def _require(document: Mapping[str, Any], key: str) -> Any:
    if key not in document:
        raise ConfigError("config is missing required key %r" % key)
    return document[key]


def _verify_command(name: str, value: Any) -> VerifyCommand:
    if isinstance(value, str):
        return VerifyCommand(command=value)
    if isinstance(value, dict):
        unknown = set(value) - {"command", "cwd"}
        if unknown:
            raise ConfigError("verify.%s has unknown keys: %s" % (name, sorted(unknown)))
        if not isinstance(value.get("command"), str):
            raise ConfigError("verify.%s needs a string 'command'" % name)
        return VerifyCommand(command=value["command"], cwd=str(value.get("cwd", ".")))
    raise ConfigError("verify.%s must be a string or a mapping" % name)


def _notify_target(value: Any) -> NotifyTarget:
    if isinstance(value, str):
        kind, path = value, None
    elif isinstance(value, dict):
        unknown = set(value) - {"target", "path"}
        if unknown:
            raise ConfigError("notify target has unknown keys: %s" % sorted(unknown))
        kind = value.get("target")
        path = value.get("path")
    else:
        raise ConfigError("a notify target must be a string or a mapping")
    if kind not in {"stdout", "file", "macos"}:
        raise ConfigError("unknown notify target %r" % (kind,))
    if kind == "file" and not path:
        raise ConfigError("notify target 'file' needs a 'path'")
    return NotifyTarget(kind=kind, path=path)


def load(path: os.PathLike) -> Config:
    config_path = Path(path).expanduser().resolve()
    try:
        document = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError("cannot read config %s: %s" % (config_path, exc)) from exc
    except yaml.YAMLError as exc:
        raise ConfigError("config %s is not valid YAML: %s" % (config_path, exc)) from exc
    if not isinstance(document, dict):
        raise ConfigError("config %s must be a mapping" % config_path)
    unknown = set(document) - KEYS
    if unknown:
        raise ConfigError("config has unknown keys: %s" % sorted(unknown))

    # A consumer's config lives at <root>/.agent-loop/config.yaml; every other
    # path in it is relative to <root>.
    root = config_path.parent.parent

    verify_document = _require(document, "verify")
    if not isinstance(verify_document, dict) or not verify_document:
        raise ConfigError("verify must be a non-empty mapping of cost class to command")
    verify = {name: _verify_command(name, value) for name, value in verify_document.items()}

    agents_document = _require(document, "agents")
    if not isinstance(agents_document, dict) or not agents_document:
        raise ConfigError("agents must be a non-empty mapping of role to agent(s)")
    agents: Dict[str, Tuple[AgentSpec, ...]] = {}
    for role, value in agents_document.items():
        if role not in ROLES:
            raise ConfigError("unknown agent role %r" % role)
        rungs: List[Any] = value if isinstance(value, list) else [value]
        if not rungs:
            raise ConfigError("role %r has an empty escalation ladder" % role)
        agents[role] = tuple(AgentSpec.parse(rung) for rung in rungs)

    caps_document = _require(document, "caps")
    if not isinstance(caps_document, dict) or not caps_document:
        raise ConfigError("caps must be a non-empty mapping of role to budget")
    caps: Dict[str, Budget] = {}
    cap_ints: Dict[str, int] = dict(CAP_DEFAULTS)
    for name, value in caps_document.items():
        if name in CAP_DEFAULTS:
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ConfigError("caps.%s must be a positive integer" % name)
            cap_ints[name] = value
            continue
        if name not in ROLES:
            raise ConfigError("unknown role %r in caps" % name)
        if not isinstance(value, dict) or set(value) != {"wall_s", "silence_s", "max_tokens"}:
            raise ConfigError("caps.%s needs exactly wall_s, silence_s and max_tokens" % name)
        caps[name] = Budget(
            wall_s=int(value["wall_s"]),
            silence_s=int(value["silence_s"]),
            max_tokens=int(value["max_tokens"]),
        )

    protected = _require(document, "protected_paths")
    if not isinstance(protected, list) or not all(isinstance(item, str) for item in protected):
        raise ConfigError("protected_paths must be a list of strings")

    notify_document = _require(document, "notify")
    if not isinstance(notify_document, list) or not notify_document:
        raise ConfigError("notify must be a non-empty list of targets")

    publisher = document.get("scm", scm_module.DEFAULT)
    if publisher not in scm_module.REGISTRY:
        raise ConfigError(
            "scm must be one of %s" % sorted(scm_module.REGISTRY)
        )

    levels_document = document.get("levels", {})
    if not isinstance(levels_document, dict):
        raise ConfigError("levels must be a mapping of cost class to level")
    for cost_class, level in levels_document.items():
        if level not in {"L1", "L2", "L3"}:
            raise ConfigError("levels.%s must be L1, L2 or L3" % cost_class)
        if level == "L3" and cost_class != PLANNER:
            raise ConfigError(
                "levels.%s is L3; L3 is levels.%s, and only L1 and L2 are implemented "
                "for a cost class" % (cost_class, PLANNER)
            )
    if levels_document.get(PLANNER) == "L2":
        raise ConfigError("levels.%s must be L1 or L3; %s is not a cost class" % (PLANNER, PLANNER))

    plan_sources = document.get("plan_sources", [])
    if not isinstance(plan_sources, list) or not all(
        isinstance(pattern, str) for pattern in plan_sources
    ):
        raise ConfigError("plan_sources must be a list of file globs")

    return Config(
        root=root,
        branch=str(_require(document, "branch")),
        backlog=root / str(_require(document, "backlog")),
        worktree_root=root / str(_require(document, "worktree_root")),
        ledger=root / str(_require(document, "ledger")),
        protected_paths=tuple(protected),
        verify=verify,
        agents=agents,
        caps=caps,
        notify=tuple(_notify_target(target) for target in notify_document),
        levels=dict(levels_document),
        scm=str(publisher),
        plan_sources=tuple(plan_sources),
        open_prs=cap_ints["open_prs"],
        non_progress_rounds=cap_ints["non_progress_rounds"],
        poll_s=cap_ints["poll_s"],
        idle_s=cap_ints["idle_s"],
        round_wall_s=cap_ints["round_wall_s"],
    )
