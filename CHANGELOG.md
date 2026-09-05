# Changelog

## CLI 0.4.0 — 2026-09-05
- Add `project sync finalize <PACK_PATH|PACK_ID>` as a dry-run preparation stage by default, without staging, committing, or pushing.
- Add separately explicit `--commit`, `--push`, and safely argumentized `--message` controls; `--push` never creates a commit implicitly.
- Bind finalization to an integrity-checked successful verification and a deterministic fingerprint of the exact verified canonical working-tree state.
- Reject canonical edits made after verification as stale and require `project sync verify` to be run again before finalization.
- Stage only the exact verified canonical pathspecs, compare staged objects and paths with the verified state, and exclude `.generated/**` from commits.
- Perform fail-closed Git preflight for repository, branch, HEAD, upstream, remote, detached HEAD, in-progress merge/rebase/cherry-pick, conflicts, staged/unstaged/untracked scope, and ignored-file drift.
- Record the deterministic state machine `verified → prepared → committed → pushed`, including commit SHA/message/paths and push outcome, in integrity-protected finalization reports.
- Make repeated `--commit` and `--push` idempotent when Git history still proves the pack-to-commit relationship, reporting `already_committed` and `already_synchronized` without duplicate side effects.
- Preserve working-tree content on failure and restore the prior Git index after pre-commit staging or commit failure; never use destructive rollback or force push.
- Use exit code `6` for staging/commit failures and `7` for push precondition or transport failures, while retaining Phase 2 integrity/scope/validation exit codes.
- Keep human semantic approval explicit: deterministic verification does not prove semantic correctness or approver identity, and `--commit` is only technical authorization to record the verified state.
- Preserve `project sync plan`, `project sync verify`, and legacy `project sync <OBJECT-ID>` behavior.

## CLI 0.3.0 — 2026-09-05
- Add deterministic `project sync verify <PACK_PATH|PACK_ID>` verification while preserving `project sync plan <pack>` and legacy `project sync <OBJECT-ID>` behavior.
- Bind verification to the exact SHA-256-protected `plan.json` and `manifest.json`, original pack bytes, project ID, and unchanged Git base commit.
- Require a clean planning baseline and record fingerprints for pre-existing ignored files outside `.generated/**` so later ignored-file drift is detectable.
- Verify the complete staged, unstaged, tracked, untracked, deleted, and renamed change set against the plan's validated `allowed_write_set`; reject divergent staged/unstaged content for one path and unsafe path or symlink escapes.
- Run the deterministic `validate → generate → validate` pipeline after scope checks and ensure generation writes only under `.generated/**`.
- Emit `verification.json`, `verification.md`, and `diff-summary.md` with validation results, object counts, Git scope evidence, unresolved reminders, and a deterministic atomic lifecycle summary.
- Preserve the human semantic-review boundary: verification certifies integrity, scope, and machine-checkable invariants, but never claims semantic correctness or performs semantic editing.
- Use distinct exit codes: `3` for integrity/preflight failures, `4` for allowed-write-scope violations, and `5` for canonical validation failures.

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
