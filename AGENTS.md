# Project guidance

- Keep the implementation small, direct, and easy to understand.
- Prefer the simplest design that satisfies the functional, security, performance, and maintainability requirements.
- Avoid introducing abstractions, layers, dependencies, or configuration unless they solve a concrete problem.
- Prefer existing project patterns over new patterns.
- Avoid unrelated refactoring, unless it is necessary for correctness, safety, or maintainability.
- For non-trivial changes, clarify the problem and acceptance criteria before implementation.
- Choose verification depth according to the risk and scope of the change.
- When simplicity conflicts with correctness, security, or explicit requirements, prioritize those requirements and explain the trade-off.

## Agent skills

### Issue tracker

Issues and specifications live in the GitHub repository configured in `docs/agents/issue-tracker.md`; use `gh` for issue operations.

### Triage labels

Use the canonical labels recorded in `docs/agents/triage-labels.md`.

### Domain docs

This is a single-context repository. Read `CONTEXT.md` and relevant ADRs when present; see `docs/agents/domain.md`.
