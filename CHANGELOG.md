# Changelog

## CLI 0.2.0 — 2026-09-05
- Add the versioned SYNC PACK schema v1 contract for immutable, human-approved synchronization input artifacts.
- Add deterministic `project sync plan <pack>` planning for explicit pack paths and packs stored in the stable `inbox/sync/` project directory.
- Validate pack `project_id`, exact Git `base_commit`, approval metadata, referenced target IDs, expected targets, and the existing canonical project before planning.
- Resolve existing slugged atomic object filenames by internal ID and produce an explicit allowed write set covering only approved object targets and applicable narrative documents.
- Keep `proposal` and `unresolved` changes visible in planning context without granting canonical writes.
- Bind plans to the exact pack bytes with SHA-256 and produce equivalent plan artifacts for an unchanged pack at the same HEAD.
- Reject unsafe/path-traversing targets, duplicate pack and change IDs, missing targets, stale base commits, and changed content reusing an already planned pack ID.
- Preserve legacy `project sync <OBJECT-ID>` behavior alongside the new `project sync plan <pack>` syntax.

## CLI 0.1.4 — 2026-09-04
- Accept canonical atomic object filenames in both `ID.md` and `ID-slug.md` forms.
- Validate that the filename ID prefix is well-formed and matches the object's internal `id`, while keeping the optional slug outside object identity and lookup.
- Keep context, task/sync targeting, graph generation, derived indexes, and ID collision detection keyed by internal ID regardless of filename slug.
- Add regression coverage for both valid filename forms, mismatched and malformed filenames, duplicate internal IDs under different slugs, and slug-transparent object consumers.

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
