# agent-loop

A generic exploration-loop kernel. It picks one open item from a consumer
repository's backlog — the first whose probe fails — asks one agent to close it in
a throwaway worktree, verifies the result, writes one ledger line and sends one
notification. The kernel is generic; every project-specific fact lives in the
consumer's `.agent-loop/config.yaml` and `.agent-loop/backlog.yaml`.

This is Stage 5b of the exploration-loop roadmap: the `plan` command and the L3
planner role on top of Stage 4b's modes, back-pressure, pause/resume and
ledger-only metrics. Autonomy levels are L1 and L2 for a cost class (Stage 3)
and L3 for the planner, enabled on no consumer; no Docker handling.

## The round

```
1  pick       run every selectable item's probe, in file order (file order is
              priority); choose the first failing one; skip items recorded
              BLOCKED at the current sha        no failing probe -> NO_ITEM
2  worktree   git worktree add -b explore/<item-id>
              <worktree_root>/<item-id> <branch>; an explore/<item-id> a
              previous round left behind is a result nobody has taken yet, so
              the round says so and stops       -> BLOCKED
3  worker     stripped environment, bounded bundle (backlog entry, probe output,
              cited site excerpts, design-doc section, output schema); exactly
              one repair round-trip on a malformed answer
                                                 malformed/timeout/refused -> INFRA
4  verify     the probe now exits 0; the cost class's verify command passes; no
              protected path is among the paths the round touched - committed,
              modified, added or untracked, against the sha it started from
                                                 any of the three fails -> BLOCKED
5  publish    push explore/<item-id>, open its pull request or update the one it
              already has, with a body the kernel writes from this round's own
              record; local-only opens nothing
6  review     the reviewer agent reads the item, the diff and §0's finding
              classes and answers findings; they become one pull-request
              comment - the ledger's review_posted, not a marker on GitHub, is
              what stops a second - and are not fed back to the worker
7  merge      L1 leaves the pull request open; L2 squash-merges it when the
              reviewer returned no contract or defect finding and nothing is
              held.  A protected path, or a diff verify flagged, never merges
              at any level: the round says DECIDE instead, and a DECIDE nobody
              answered in 24 h makes the next round on that item BLOCKED
8  ledger     one JSONL line: ts, item, sha, state, reason, cost, duration_s,
              tool_versions (drift is a warning, never a gate), pr_url,
              decision, review_posted, diff_stat, pr_state, notified_at
9  notify     one notification per (item, state, sha), to every configured
              target, prefixed FYI or DECIDE where the round has a question
10 cleanup    the worktree, the round's temp dir, and the explore/ branch once
              origin holds a copy of it - on every exit path
```

Terminal states are exactly four: `PR_READY`, `BLOCKED`, `NO_ITEM`, `INFRA`.
Nothing else in the kernel notifies.

## Install and run

```bash
python3 -m pip install -e .

agent-loop run --config <consumer>/.agent-loop/config.yaml --mode once
agent-loop status --config <consumer>/.agent-loop/config.yaml
```

`run` exits 0 for `PR_READY` and `NO_ITEM`, 1 for `BLOCKED`, 2 for `INFRA`; a
driven mode (`continuous`/`until`) exits 0 when it stops itself.

## Modes

`once` is a single round, unchanged since Stage 2. `continuous` runs a round,
then waits (polling `caps.poll_s`, no daemon) for a trigger before running the
next: the backlog file's mtime changing, a pull request the ledger still shows
open turning out to be merged or closed (`gh pr view`, only when `scm: github`
is configured), a BLOCKED item whose ledger line predates the backlog's last
edit ("reopened" by editing it), or the `caps.idle_s` idle timer. `schedule`
is `once` under cron's name - nothing else is different, so it is the same
code path. `until` is `continuous` with one or more stop conditions -
`--until-prs N`, `--until-hours H`, `--until-cost C` - the loop stops itself
the moment any one is met:

```bash
agent-loop run --config <consumer>/.agent-loop/config.yaml --mode continuous
agent-loop run --config <consumer>/.agent-loop/config.yaml --mode until --until-prs 3
```

Each round of a driven mode calls `round.run_once` directly, in-process - no
subprocess, no daemon. `caps.round_wall_s` is enforced inside `run_once`
itself (a `signal.alarm`): a round still going at the cap raises the same
`InfraError` any other one would, so it takes the exact path a normal failure
does - the worktree it started is removed by the existing cleanup, one
ledger line is written, one notification goes out, deduplicated the same as
always.

## Back-pressure

Two more `caps` keys, read only by `continuous`/`until`: `open_prs` (default
3) - a round is not started while the ledger shows that many `PR_READY` items
whose pull requests are still open; `non_progress_rounds` (default 5) - after
that many consecutive `NO_ITEM`/`INFRA` rounds, the loop sleeps `caps.idle_s`,
sends one `FYI` naming the count, and resets. Neither is a fifth terminal
state or a new notification kind.

## Pause and resume

```bash
agent-loop pause --config <consumer>/.agent-loop/config.yaml
agent-loop resume --config <consumer>/.agent-loop/config.yaml
```

`pause` drops a flag file under `worktree_root`; `continuous`/`until` check it
before every round and idle (polling `caps.poll_s`) rather than starting one
while it is there. `resume` removes it. `status` shows which state it is in.

## Metrics

```bash
agent-loop metrics --config <consumer>/.agent-loop/config.yaml
```

Text, read from the ledger alone - no chart, no new file: rounds by state;
pull requests opened and merged (merged is known once a round's own L2 merge
succeeds, or a `continuous`/`until` trigger poll later observes it - either
way the ledger line carries `pr_state`); the plumbing share (pull requests
whose diff touched `.agent-loop/`, read back off the `diff_stat` each `PR_READY`
line already carries, against every other pull request); the median time from
a round's terminal state to its notification (`ts` vs. the same line's
`notified_at`); and cost per merged pull request.

## Planning

```bash
agent-loop plan --config <consumer>/.agent-loop/config.yaml
```

The planner role reads consumer data only — the backlog's ids, statements and
cost classes, the ledger's last 20 lines, and the files any `plan_sources`
globs name (8 KB each, marked when cut) — and proposes at most five backlog
items of `{id, statement, cost_class, sites, probe, proof, rationale}`, through
the same adapter path every other role uses: stripped environment, a bounded
bundle that refuses rather than truncates, one repair on a malformed answer.
Admission is invariant 2 and nothing else: the kernel runs each proposal's
probe itself, from the cost class's own verify `cwd`, and a probe that exits 0
is **rejected** — a proposal whose id or statement is already in the backlog is
rejected as a duplicate before its probe is even spent. Everything judged,
admitted and rejected alike, is written to `<worktree_root>/proposals.yaml`
with the exit code and output tail behind each verdict, and one `FYI` says how
many of each. A plan run is not a round — no item, no sha, none of the four
terminal states — so it writes no ledger line; `proposals.yaml` is its record.
Only `levels: {planner: L3}` also appends the admitted ones to
`backlog.yaml` itself.

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
| `caps` | per role: `wall_s`, `silence_s`, `max_tokens`; plus five continuous-mode keys keyed by name, not role: `poll_s` (default 30), `idle_s` (900), `open_prs` (3), `non_progress_rounds` (5), `round_wall_s` (3600) |
| `notify` | `stdout`, `macos`, or `{target: file, path: ...}` |
| `levels` | per cost class; `L1` (a person merges) or `L2` (the loop may merge). `planner` is the one reserved key and is not a cost class: `L3` there lets `agent-loop plan` append admitted proposals to the backlog |
| `plan_sources` | file globs `agent-loop plan` reads into the planner's bundle, read-only; absent means the backlog and ledger alone |
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
