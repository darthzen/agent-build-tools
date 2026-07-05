# Agent-ready issue template

Every ticket follows this shape. It exists so deepseek-coder-v2:16b never has to infer or improvise. If a section can't be filled concretely, the task is too big — split it.

Each issue body (rendered from `tasks.yaml`) contains:

```markdown
## Context
One or two sentences: what this delivers and why. Link the spec section, e.g. "Schema §4 Item core".

## Dependencies
- T-0xx (must be closed first)
- none

## Files
Create:
- `Sources/NifflerCore/Models/Item.swift`
Modify:
- (exact paths, or "none")
Do NOT touch anything else.

## Steps
1. Concrete, ordered instructions. Name types, properties, and signatures explicitly.
2. ...

## Acceptance
Runnable checks. The task is done only when all pass.
- [ ] `swift build` succeeds
- [ ] `swift test --filter ItemTests` passes
- [ ] Item has properties: id, collectionId, name, maker, model, modelYear?, manufactureYear?, isReissue, ...

## Out of scope / guardrails
- Don't add persistence here (that's T-0xx).
- No new dependencies.

## Definition of done
Project builds, all acceptance checks pass, change is limited to the listed files, committed as `[T-0xx] <title>` with `Closes #<n>`.
```

## Authoring rules

- **One concern per ticket** — roughly one file or one tightly-scoped change. If acceptance needs more than ~4 checks, split.
- **Name everything** — exact type names, property names, function signatures, file paths. No "etc.", no "and so on".
- **Acceptance must be executable** — a build command and/or a named test. Prefer a test the agent can run with `swift test --filter <Name>`.
- **State the guardrails** — what NOT to touch, so an autonomous run can't sprawl.
- **Mark `assignee`** — `agent` for pure Swift/test/code work; `human` for Xcode project, signing, secrets, App Store, or anything needing a GUI or credentials.
- **Declare `depends_on`** — so the runner only ever picks unblocked work.
