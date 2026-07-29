# agent-build-tools
[![FOSSA Status](https://app.fossa.com/api/projects/git%2Bgithub.com%2Fdarthzen%2Fagent-build-tools.svg?type=shield)](https://app.fossa.com/projects/git%2Bgithub.com%2Fdarthzen%2Fagent-build-tools?ref=badge_shield)


GitHub-Issues-native ticketing toolkit for agent-driven software builds. A
ticket IS a GitHub Issue — open = to-do, closed = done, no shadow task files.
The toolkit owns selection, context assembly, verification, and the whole
git/PR ceremony so the executing model only writes code.

Extracted from the [Niffler](https://github.com/darthzen/niffler) build after
it ran that project's plan end to end; generalized so any repo can adopt it by
dropping in one config file. Current consumers: Niffler (SwiftUI app) and
[nemoclaw-ops-agent](https://github.com/darthzen/nemoclaw-ops-agent)
(Kubernetes/agent infrastructure) — two very different stacks, same workflow.

## Model

- **Serial flow:** `new` → `next` → `start` → `show` → implement → `finish`
  (verify, branch, commit, push, PR, squash-merge, close issue).
- **Parallel flow (one orchestrator, N worker worktrees):** `capacity` →
  `batch` → `worktree-add` → workers `submit` → orchestrator `land`
  (serialized squash-merge behind a file lock, rebase-and-retry on conflict).
- **`doctor [--fix]`** reconciles issues ↔ commits ↔ board ↔ branches.

## Adopting in a repo

1. Add this repo as a submodule (or copy the script):
   `git submodule add https://github.com/darthzen/agent-build-tools build-plan/tools`
2. `cp build-plan/tools/config.example.yaml build-plan/config.yaml` and edit —
   the `tooling:` section carries everything project-specific (runnable
   acceptance commands, per-command cwd, test-path template, optional extra
   build gate, §-citation doc).
3. Create tickets with
   `python3 build-plan/tools/agent-tools.py new "Title" --milestone M0 --area infra ...`
   — never hand-write issue bodies; the tooling parses what it emits.

Requires: `gh` (authenticated), `git`, Python 3 with PyYAML.

## Ticket format

Issue title `T-012 · Do the thing`; body header carries
`**Milestone:** … | **Assignee:** agent|human | **Depends on:** T-…` and a
`**Files** — create: […]; modify: […]` line, then `## Context / ## Steps /
## Acceptance`. Acceptance bullets containing backtick-quoted runnable
commands are executed by `verify`/`finish`; anything else is a human checklist.

Conventions that make agent execution safe: tickets declare their files and
agents may touch nothing else; `human`-labeled tickets are dependencies, never
picked up; acceptance must pass before any commit; the tooling owns git.

## Session provenance (Entire)

`finish` and `land` merge with `gh pr merge --squash --delete-branch`. GitHub
builds a **new** commit server-side and deletes the branch, so any
[Entire](https://entire.io) checkpoint that the post-commit hook attached to the
local task-branch commit is orphaned — it points at a SHA that never reaches
`main`, and the agent session that wrote the code loses its link to the merged
work.

Enable the opt-in `entire:` block in `config.yaml` and the toolkit re-anchors the
session onto the squash commit after the merge:

```yaml
entire:
  enabled: true
  agent: claude-code   # passed to `entire session attach --agent`
  amend: false         # link via the metadata ref only — no history rewrite on main
```

- **Serial (`finish`):** captures the active session before the git ceremony and
  runs `entire session attach <sid>` once `main` is updated.
- **Parallel (`submit` → `land`):** `submit` records the worker's session id as an
  `Entire-Session:` trailer in the PR body; `land` reads it back and re-anchors
  after the merge, **before** tearing down the worktree (and its session store).

With `amend: false` the attach links through Entire's metadata ref and never
rewrites or force-pushes `main`. The whole path is best-effort — an Entire
failure prints a note but never blocks a land. Omit the block entirely (the
default) and the toolkit stays Entire-unaware. Requires the `entire` CLI on PATH
and `entire enable` already run in the target repo.


## License
[![FOSSA Status](https://app.fossa.com/api/projects/git%2Bgithub.com%2Fdarthzen%2Fagent-build-tools.svg?type=large)](https://app.fossa.com/projects/git%2Bgithub.com%2Fdarthzen%2Fagent-build-tools?ref=badge_large)