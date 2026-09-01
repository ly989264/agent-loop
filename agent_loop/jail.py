"""The container jail: what a worker may reach while it runs.

ROADMAP.md §4 Stage 6.  Env stripping removes variables, not access: a worker's
``python3`` runs with the operator's ``HOME`` and can read ``~/.ssh`` and
``~/.claude`` (Stage 2b review, Stage 5a review).  A jail replaces that boundary
with an OS one - one ``docker run --rm`` whose only mount is the tree the round
is allowed to change, at a fixed path, as its working directory.

Nothing here is consumer-specific.  The image is data: a consumer that wants a
jail is responsible for an image carrying the toolchain its own commands need,
including the agent CLI its adapter starts.

What the container gets, and nothing else:

- one bind mount, read-write, at ``/workspace`` - the round's worktree, or for a
  plan run the consumer root its probes already run in;
- no docker socket, no host HOME, no second mount of any kind;
- ``PINNED`` from :mod:`agent_loop.environment` plus the credential variables the
  config names by name, passed as ``-e NAME`` so no value reaches the argv;
- ``--init``, so a killed container reaps its own children;
- a pids cap, and the memory cap the config names.
"""

from __future__ import annotations

import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Mapping, Optional, Sequence, Tuple

from .environment import PINNED, agent_environment
from .errors import ConfigError

KEYS = frozenset({"image", "credentials_env", "memory"})
# The one path a jailed command sees.  Fixed rather than configured: a consumer's
# commands run there as they run in the worktree, and a second name for the same
# directory is a thing to get wrong, not a thing to choose.
WORKDIR = "/workspace"
NAME_PREFIX = "agent-loop-"
# Generous, and a cap all the same: a fork bomb inside the jail takes the jail
# down rather than the machine.  Not configurable - no consumer asked for a
# number, and one in the kernel is not consumer-specific.
PIDS_LIMIT = 2048
KILL_TIMEOUT_S = 30


@dataclass(frozen=True)
class Jail:
    image: str
    # Host environment variables forwarded into the container by name.  The
    # narrowest credential path there is: the value never appears in the argv,
    # nothing is mounted, and a variable the host does not set is not passed.
    credentials_env: Tuple[str, ...] = ()
    memory: Optional[str] = None


def parse(value: Any) -> Optional[Jail]:
    """Read the optional ``jail`` key.  Absent means today's behaviour, unchanged."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ConfigError("jail must be a mapping")
    unknown = set(value) - KEYS
    if unknown:
        raise ConfigError("jail has unknown keys: %s" % sorted(unknown))
    image = value.get("image")
    if not isinstance(image, str) or not image.strip():
        raise ConfigError("jail needs a non-empty string 'image'")
    names = value.get("credentials_env", [])
    if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
        raise ConfigError("jail.credentials_env must be a list of environment variable names")
    memory = value.get("memory")
    if memory is not None and not isinstance(memory, str):
        raise ConfigError("jail.memory must be a string, e.g. '4g'")
    return Jail(image=image, credentials_env=tuple(names), memory=memory)


def container_name() -> str:
    """A name for one run, so the container can be killed without its client."""
    return NAME_PREFIX + uuid.uuid4().hex[:12]


def docker_argv(
    jail: Jail,
    argv: Sequence[str],
    *,
    mount: Path,
    name: str,
    workdir: str = WORKDIR,
    environ: Optional[Mapping[str, str]] = None,
) -> List[str]:
    """The full ``docker run`` argv for one jailed command."""
    source = agent_environment() if environ is None else environ
    docker = [
        "docker", "run", "--rm", "--init",
        "--name", name,
        "--workdir", workdir,
        "--volume", "%s:%s" % (Path(mount).resolve(), WORKDIR),
        "--pids-limit", str(PIDS_LIMIT),
    ]
    if jail.memory:
        docker += ["--memory", jail.memory]
    for key in sorted(PINNED):
        docker += ["--env", "%s=%s" % (key, PINNED[key])]
    for key in jail.credentials_env:
        # `--env NAME` takes the value from this process's environment, so a
        # credential is never an argument anything can read off `ps`.
        if key in source:
            docker += ["--env", key]
    return docker + [jail.image] + list(argv)


def kill(name: str) -> None:
    """Stop the container by name.

    ``docker run`` is a client: killing its process group leaves the container
    running, so the bounded runner's wall and silence caps would not bound
    anything.  This is what actually ends a jailed command.
    """
    try:
        subprocess.run(
            ["docker", "kill", name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=KILL_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


def run_command(
    jail: Jail, command: str, mount: Path, cwd: str, timeout: int
) -> Tuple[int, str]:
    """Run one shell command in the jail; the signature ``pick.run_command`` has.

    ``cwd`` is relative to the mount, exactly as a consumer's verify entry says.
    """
    name = container_name()
    workdir = WORKDIR if cwd in ("", ".") else "%s/%s" % (WORKDIR, cwd.strip("/"))
    argv = docker_argv(jail, ["sh", "-c", command], mount=mount, name=name, workdir=workdir)
    try:
        completed = subprocess.run(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
            env=agent_environment(),
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        kill(name)
        return 124, "command exceeded its %ds timeout in the jail: %s" % (timeout, command)
    except OSError as exc:
        return 127, "cannot run %s in the jail: %s" % (command, exc)
    return completed.returncode, completed.stdout
