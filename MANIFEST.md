# Distribution Manifest

- Template version: 1.1.0 Stable
- CLI version: 0.4.0
- Schema version: 1
- Narrative templates: 34
- Atomic object types: 12
- Blueprint modules: 13
- JSON schemas: 15
- Machine assets: packaged in `project_cli/project_system_assets/` for source and wheel installs
- SYNC PACK assets: v1 contract document and JSON schema included in release artifacts
- Adversarial hardening: path/symlink safety, transactional module enable rollback, target-first atomic context budgeting, 8-char random IDs, strict frontmatter parsing, symmetric blueprint conflicts

## MVP CLI commands

`init`, `new`, `validate`, `generate`, `context`, `impact`, `health`, `modules`, `enable`, `disable`, `task`, `sync` (including `sync plan`, `sync verify`, and `sync finalize`), `bootstrap`, `prepare-pr`.

## Phase 1 deterministic SYNC

Version 0.2.0 adds the versioned SYNC PACK v1 contract and deterministic `project sync plan <pack>` planning. Planning validates project, Git base, approval, and target identity; resolves an allowed write set; records proposals and unresolved items without canonical writes; and emits hash-bound, idempotent artifacts only under `.generated/sync/`. Legacy `project sync <OBJECT-ID>` remains supported.

## Phase 2 deterministic SYNC verification

Version 0.3.0 adds `project sync verify <PACK_PATH|PACK_ID>`. Verification binds the original pack, `plan.json`, and `manifest.json` to their recorded integrity data; rechecks project ID, Git base commit, planning baseline, and allowed write scope across tracked, staged, unstaged, untracked, deleted, renamed, and ignored-file changes; runs `validate → generate → validate`; and writes deterministic verification and diff-summary reports under `.generated/sync/<pack_id>/`. Atomic lifecycle output is structural evidence only: human semantic review remains required. Exit codes distinguish integrity/preflight (`3`), scope (`4`), and canonical validation (`5`) failures.

## Phase 3 deterministic SYNC finalization

Version 0.4.0 adds dry-run `project sync finalize <PACK_PATH|PACK_ID>` plus separately explicit `--commit`, `--push`, and `--message` controls. Finalization binds an integrity-checked successful verification to the exact canonical working-tree fingerprint, rejects stale post-verification edits, performs fail-closed Git preflight, stages only verified canonical paths, never commits `.generated/**`, preserves the prior index on pre-commit failure, and records idempotent `verified → prepared → committed → pushed` state under `.generated/sync/<pack_id>/`. Exit codes `6` and `7` distinguish commit and push failures. It does not perform semantic edits or claim semantic approval.

## Deliberately not automated in v1.1

No autonomous approvals, no direct LLM API calls, no live Figma/Google Docs/Sheets sync, no vector DB, no universal semantic code analyzer, no automatic merge to main.

## Security / governance boundary

Local validation proves schema/graph/filesystem invariants only. It cannot prove that a human personally supplied approval metadata. Enforced HITL requires repository-hosting controls such as protected branches and required human reviews. Knowledge frontmatter rejects aliases, duplicate keys, oversized metadata, and unsafe Python YAML constructors.

## Stable verification baseline

- Unit + adversarial regression suite: **104 passed, 1 platform-permission skip, 0 failed**.
- Source-tree end-to-end flow: **passed**.
- Wheel build: **passed**; packaged assets verified inside wheel.
- Installed console entry point outside source checkout: `project --version → init → validate`: **passed**.
- Clean target installation from wheel (dependencies supplied by host): `init → new → validate → generate → enable backend → enable payments → validate`: **passed**.

## External review status

- External adversarial review round 3 reported **26 passed, 0 failed** and recommended promotion of RC2 to Stable.
- Stable promotion was also re-verified locally with **26 passed, 0 failed**.
