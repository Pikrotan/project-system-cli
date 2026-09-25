# AGENTS.md

## Purpose
Use the repository's canonical project knowledge without reading the entire tree or inventing approvals.

## Sources of truth
1. Active atomic objects in `knowledge/` for lifecycle facts.
2. Active narrative docs in `docs/` for system explanation and intent.
3. `project.yaml` and `.project/policies/*.yaml` for machine configuration and enforceable policy.
4. Code/schema for implementation facts.
5. Figma for visual truth where referenced by screen/flow objects.
6. `.generated/` is derived and never canonical.

## Context loading
Prefer a prepared task/context pack. Otherwise use `project context`. Do not recursively load `history/` or inactive blueprint material unless the task explicitly requires historical or blueprint context.

## Operating modes
- DISCUSS: no repository changes.
- RESEARCH: research/inbox changes only unless separately approved.
- SYNC: semantic edits only inside the prepared allowed write set.
- IMPLEMENT: code plus necessary approved documentation changes.
- REVIEW: do not modify unless separately requested.

## Authority
AI may propose. Meaning-changing product, scope, architecture, business, security, privacy, and major UX decisions require the configured human approval path. Pure implementation choices may be made only when they do not change observable intent or architecture.

## Uncertainty and drift
Do not silently guess. Flag unresolved questions, out-of-scope impacts, or drift. Current active state wins over historical material.

## Completion
After meaningful work report: Changed, Why, Not changed, Open conflicts, Validation, Git status.
