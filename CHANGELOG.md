# Changelog

## CLI 0.1.3 — 2026-09-04
- Establish `.md` with YAML frontmatter and Markdown body as the canonical atomic object format through one shared object loader.
- Use the shared loader for validation, generation, graph/context operations, task and sync targeting, and ID collision detection.
- Reject unsupported atomic-like `.yaml` and `.yml` files and non-empty knowledge layers with zero recognized objects.
- Report recognized object counts by type during validation.
- Block generation before writing derived output when validation contains BLOCKING or ERROR issues.
- Standardize collision-resistant IDs as `<TYPE>-YYYYMMDD-<8 lowercase hex chars>` across runtime generation, reference matching, and all object schemas.
- Add regression coverage for object discovery, unsupported formats, empty-recognition protection, generation gating, targeting, counts, and ID collisions.

## v1.1.0 Stable — 2026-08-31
- Promoted RC2 to Stable after three adversarial review rounds and local re-verification.
- No runtime logic changes from RC2; Stable freezes the verified RC2 implementation.
- Regression/adversarial suite baseline: 26 passed, 0 failed.
- Wheel-install and packaged-resource smoke flows verified outside the source checkout.
- Project Template release version is v1.1.0; CLI remains independently versioned at 0.1.2.

## v1.1 RC2
- Treat blueprint conflicts symmetrically; added regression coverage for one-sided declarations.
- Harden knowledge frontmatter: strict SafeLoader, duplicate-key rejection, no YAML anchors/aliases, object/frontmatter size limits.
- Make context budget semantics observable with `char_budget` and `actual_chars`; token budgets remain explicit estimates.
- Improve module rollback cleanup for directories created during a failed enable transaction.
- Add disposition for Gemini RC1 adversarial review.
- Package schemas/blueprints/templates/policies with the CLI so wheel installations are self-contained; verified through wheel build and clean target-install smoke flow.

## v1.1 Final Architecture

- Atomic knowledge objects replace manually maintained central decision/requirement indexes.
- Collision-resistant IDs replace sequential IDs.
- Machine-readable dependency graph and impact policies.
- Generated views are disposable and non-canonical.
- Context/task packs provide an AI-neutral interface.
- Blueprints are activated on demand and live in the distribution, not every concrete project.
- CLI and schemas are versioned independently from instantiated projects.
- Human approval boundaries are separated from deterministic validation.
- Narrative documentation reduced to 34 possible living docs.

## v1.1 RC1 — adversarial hardening

- Hardened blueprint materialization against path traversal and symlink writes.
- Added rollback for partially committed module files and atomic project.yaml writes.
- Context packs now prioritize the target object, never truncate objects, report omissions, and fail explicitly if the target itself cannot fit the budget.
- Expanded random ID suffix from 4 to 8 characters and added exclusive object-file creation.
- Added explicit local-validator HITL limitation messaging.
- Enforced schema version 1 and object type/directory consistency.
- Added adversarial regression tests for filesystem safety, YAML safety, context budgets, schema versions, and rollback.
