# agent-loop

A generic exploration-loop kernel. It picks one open item from a consumer
repository's backlog — the first whose probe fails — asks one agent to close it in
a throwaway worktree, verifies the result, writes one ledger line and sends one
notification. The kernel is generic; every project-specific fact lives in the
consumer's `.agent-loop/config.yaml` and `.agent-loop/backlog.yaml`.

This is Stage 2a of the exploration-loop roadmap: mode `once`, no GitHub, no
autonomy levels above L1, no Docker handling.

## The round

```
1  pick       run every selectable item's probe, in file order (file order is
              priority); choose the first failing one; skip items recorded
              BLOCKED at the current sha        no failing probe -> NO_ITEM
2  worktree   git worktree add -b explore/<item-id>
              <worktree_root>/<item-id> <branch>
3  worker     stripped environment, bounded bundle (backlog entry, probe output,
              cited site excerpts, design-doc section, output schema); exactly
              one repair round-trip on a malformed answer
                                                 malformed/timeout/refused -> INFRA
4  verify     the probe now exits 0; the cost class's verify command passes; no
              protected path is among the paths the round touched - committed,
              modified, added or untracked, against the sha it started from
                                                 any of the three fails -> BLOCKED
5  ledger     one JSONL line: ts, item, sha, state, reason, cost, duration_s,
              tool_versions (drift is a warning, never a gate)
6  notify     one notification per (item, state, sha), to every configured target
7  cleanup    the worktree, its explore/ branch and the round's temp dir, on
              every exit path
```

Terminal states are exactly four: `PR_READY`, `BLOCKED`, `NO_ITEM`, `INFRA`.
Nothing else in the kernel notifies.

## Install and run

```bash
python3 -m pip install -e .

agent-loop run --config <consumer>/.agent-loop/config.yaml --mode once
agent-loop status --config <consumer>/.agent-loop/config.yaml
```

`run` exits 0 for `PR_READY` and `NO_ITEM`, 1 for `BLOCKED`, 2 for `INFRA`.
`once` is the only mode.

## Config keys

`examples/valkey_scale_lab.config.yaml` is a filled-in example. Every path is
relative to the repository root, which is the parent of the `.agent-loop`
directory the config sits in. An unknown key is an error, so a typo is reported
rather than ignored.

| Key | Meaning |
|---|---|
| `branch` | the branch a round works from |
| `backlog` | path to the backlog YAML |
| `worktree_root` | where round worktrees are created and removed |
| `ledger` | append-only JSONL, one line per round |
| `protected_paths` | a diff touching one of these is BLOCKED |
| `verify` | per cost class: `command`, and the `cwd` its commands run in — the class's probes run there too |
| `agents` | per role (`planner`/`worker`/`reviewer`/`diagnoser`): `adapter[:model]`, a list being an escalation ladder |
| `caps` | per role: `wall_s`, `silence_s`, `max_tokens` |
| `notify` | `stdout`, `macos`, or `{target: file, path: ...}` |
| `levels` | per cost class; `L1` (a person merges) or `L2` (the loop may merge) |
| `scm` | `github` (push, open or update the PR, one review comment, squash-merge) or `local-only` (the default: no forge) |

A cost class with no `verify` entry cannot be verified, so a round that picks an
item of that class ends `INFRA` rather than guessing.

## Adapters

`run(role, bundle, schema, sandbox, budget) -> AgentResult(status, json, cost, raw_tail)`,
`status` one of `ok`, `malformed`, `timeout`, `refused`, `sandbox` one of
`read-only`, `worktree-write`.

- `claude-code` — `claude -p --output-format json --model <model>`
- `codex` — `codex exec --output-schema <schema>`
- `shell:<program>` — the bundle on stdin, one JSON object on stdout

Environment stripping, the bounded bundle that refuses rather than truncates, and
the single repair round-trip are kernel-side, so every adapter inherits them.
Stage 2a runs the first rung of a role's ladder; escalation is parsed, not yet
exercised.

## Tests

```bash
PYTHONPATH=.:tests python3 -m unittest discover -s tests
```

Hermetic: no network, no real agent, no Docker.
