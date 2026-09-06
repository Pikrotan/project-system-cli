# Distribution Manifest

- Template version: 1.1.0 Stable
- CLI version: 0.5.0
- Schema version: 1
- Narrative templates: 34
- Atomic object types: 12
- Blueprint modules: 13
- JSON schemas: 16
- Machine assets: packaged in `project_cli/project_system_assets/` for source and wheel installs
- SYNC PACK assets: v1 contract document and JSON schema included in release artifacts
- SYNC REQUEST assets: Bridge v1 contract document and shared-definition JSON schema included in release/package assets
- Adversarial hardening: path/symlink safety, transactional module enable rollback, target-first atomic context budgeting, 8-char random IDs, strict frontmatter parsing, symmetric blueprint conflicts

## MVP CLI commands

`init`, `new`, `validate`, `generate`, `context`, `impact`, `health`, `modules`, `enable`, `disable`, `task`, `sync` (including `sync intake`, `sync plan`, `sync verify`, and `sync finalize`), `bootstrap`, `prepare-pr`.

## Bridge v1 deterministic intake

Version 0.5.0 adds `project sync intake <REQUEST_PATH>` and `project sync intake -` for UTF-8 stdin/pipe workflows, with optional `--plan`. The strict SYNC REQUEST v1 transport rejects request-supplied `project_id`, `base_commit` and `pack_id`; shared Phase 1 validation resolves targets before exclusive immutable pack creation in `inbox/sync/`. Local binding reads project identity and HEAD, generates a collision-resistant `SYNC-YYYYMMDD-xxxxxxxx` pack ID, and preserves approval and request SHA-256 provenance. Identical request bytes at the same project/HEAD reuse the pack; another HEAD creates a new binding without replacing the earlier pack. Planning exempts only the selected pack and `.generated/**`, never the entire inbox. Reports stay under `.generated/sync/`; intake never edits canonical content, commits or pushes. See `SYNC_REQUEST_V1.md` for encoding, baseline, recovery and trust boundaries.

Bridge implementation verification before release metadata: **164 passed, 1 Windows symlink-permission skip**; installed-wheel file/stdin intake, plan, verify and dry-run finalize passed. Real SportOS file/stdin intake and planning passed in that earlier implementation check; temporary inputs/outputs were removed and the original file hashes, directory inventory and HEAD were unchanged.

CLI 0.5.0 release-metadata verification: **164 passed, 1 Windows symlink-permission skip**. Temporary installed-wheel checks passed for version/assets, file and stdin intake, same-HEAD reuse, new binding at another HEAD, planning, verification, dry-run finalization and legacy sync. Request-supplied project/base/pack identities, duplicate change IDs, traversal and malformed input were rejected with exit code `2`. No SYNC commit/push was performed; SportOS was not accessed for this release check.

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
