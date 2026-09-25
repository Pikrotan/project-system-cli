# AGENTS.md

Use active project knowledge and the matching registered project Skill without inventing facts, approvals, or write authority.

## Canonical boundaries

- `knowledge/**` contains atomic lifecycle state.
- `docs/**` contains current narrative state.
- `project.yaml`, `.project/policies/**`, and schemas define constraints.
- `.agents/skills/**` defines reusable workflows, never project truth.
- `.generated/**` is derived and disposable.
- `history/**` is excluded unless the task explicitly requires historical context.

## Skills and precedence

Select project Skills from `.project/skills.yaml`. A Skill cannot override canonical knowledge, schemas, policies, human approval, or a prepared task/SYNC write scope. Global user Skills are optional helpers and must not be project correctness dependencies.

## Authority and scope

AI may propose. Meaning-changing product, scope, architecture, business, security, privacy, and major UX decisions require the configured human approval path. Write only where one selected Skill, the current task scope, and governance each authorize the path. Stop on missing authority, unresolved drift, or out-of-scope impact.

## Context and completion

Prefer a prepared task/context pack. Do not recursively load `history/**` or inactive blueprint material unless explicitly required. After meaningful work report: Changed, Why, Not changed, Open conflicts, Validation, Git status.
