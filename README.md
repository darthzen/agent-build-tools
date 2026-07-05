# agent-build-tools

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
