# Distribution Manifest

- Template version: 1.1.0 Stable
- CLI version: 0.9.0
- Schema version: 1
- Narrative templates: 34
- Atomic object types: 12
- Blueprint modules: 13
- JSON schemas: 17
- Machine assets: packaged in `project_cli/project_system_assets/` for source and wheel installs
- SYNC PACK assets: v1 contract document and JSON schema included in release artifacts
- SYNC REQUEST assets: Bridge v1 contract document and shared-definition JSON schema included in release/package assets
- SYNC pull assets: Bridge v2 contract, transport runtime and optional policy/provenance schemas included in package assets
- Terminal SYNC assets: durable binding v1 contract/schema, Git-admin completed store, migration runtime and crash-safe terminalization included in package assets
- Automatic pickup assets: bounded-cycle contract and OS-neutral pickup runtime included in release/package assets
- Foreground watcher assets: persistent runtime, watcher contract, lock/state/event/backoff implementation included in release/package assets
- Adversarial hardening: path/symlink safety, transactional module enable rollback, target-first atomic context budgeting, 8-char random IDs, strict frontmatter parsing, symmetric blueprint conflicts

## MVP CLI commands

`init`, `new`, `validate`, `generate`, `context`, `impact`, `health`, `modules`, `enable`, `disable`, `task`, `sync` (including persistent `sync watch`, bounded `sync watch --once`, `sync pull`, `sync intake`, `sync plan`, `sync verify`, `sync finalize`, and `sync migrate-bindings`), `bootstrap`, `prepare-pr`.

## Persistent Foreground Watcher — Stage 2A

Version 0.9.0 adds `project sync watch`, a foreground loop over the unchanged 0.8 bounded pickup core. A separate OS-level project/worktree lock prevents concurrent persistent watchers; the existing intake lock continues to coordinate pack creation with manual pull/intake. Disposable atomic state and sanitized JSONL events live under `.generated/sync/auto/`. The default interval is 120 seconds, configurable from 60 to 3600 seconds, with retryable operational backoff capped at 1800 seconds. Ctrl+C records a clean stop and releases the lock. Fatal configuration, malformed request-head and integrity/identity conflicts stop fail-closed. No scheduler, autostart, background service, semantic edit, verification, finalization, Git staging/commit/push or GitHub mutation is included. See `SYNC_WATCHER_V1.md`.

Foreground watcher verification: **304 passed, 2 Windows symlink-permission skips**. Coverage includes multiple cycles without real sleeping, no-pending repetition, created-to-active single-pack behavior, exponential growth/cap/reset, retryable and fatal classifications, Ctrl+C shutdown and exit code, exclusive lock/release, atomic state, sanitized valid JSONL, crash-tail repair and fail-closed complete-record corruption, runtime symlink rejection, unchanged bounded-cycle behavior, and all Bridge/terminal regressions.

## Automatic Pickup Core — Stage 1

Version 0.8.0 adds the OS-neutral, single-cycle `project sync watch --once`. It validates local configuration/repository/bindings, reconciles completed transport identity, evaluates authorized marked open GitHub Issues oldest-first by Issue number, and delegates at most one selected request to the existing pull/intake/plan pipeline. Stable structured statuses distinguish created/no-pending, active or awaiting-push work, dirty state, unfinished transactions, malformed queue heads, integrity/identity conflicts, configuration failures and transport failures. The shared intake lock coordinates selection through pack creation with manual pull/intake. The cycle never performs semantic edits, verification, finalization, staging, commit, push or GitHub mutation. Persistent loops, scheduler integration, autostart and notifications are not part of Stage 1. See `SYNC_PICKUP_V1.md`.

Automatic pickup verification: **276 passed, 1 Windows symlink-permission skip**. Coverage includes empty and multi-Issue queues, oldest-first single selection, completed skips and request-identity drift, lifecycle-only close/reopen/update tolerance, active/awaiting-push/dirty/transaction blocks, marker and author filtering, malformed-head and duplicate-request blocking, no canonical/Git mutation, and cooperative intake-lock races. Mutable GitHub lifecycle timestamps/state remain provenance-only for completed records; request identity and archived integrity remain fail-closed. Temporary package verification uses mocked transport only; no live network or production-project mutation is performed.

## Durable terminal SYNC lifecycle

Version 0.7.0 moves successfully pushed GitHub transport packs from `inbox/sync/` into an immutable, worktree-local completed store beneath the Git administrative directory. Exact archived pack bytes plus a sealed versioned binding preserve processed Issue/request identity after `.generated/` is removed without dirtying the working tree. GitHub commit-only finalization remains active as `awaiting_push`; terminalization occurs only after proven push. Explicit `--complete --outcome reviewed-no-change|rejected|abandoned --reason ...` records human technical disposition without claiming cryptographic identity. `sync migrate-bindings` audits legacy 0.6 bindings and `--apply` archives only unambiguous pushed records. No watcher, background process, scheduler, canonical semantic edit, automatic stage, implicit commit, Issue mutation or release action is included. See `SYNC_TERMINAL_V1.md`.

Durable terminal verification: **250 passed, 1 Windows symlink-permission skip**. Temporary wheel and sdist metadata/assets were verified at `0.7.0`; installed-wheel smoke covered GitHub commit-only `awaiting_push`, local bare-remote push terminalization, clean working-tree restoration, durable identity after `.generated` deletion, and completed-Issue reuse. All temporary build, wheel, venv, repository and test artifacts were removed. No GitHub or production-project mutation was performed.

## Bridge v2 transport

Version 0.6.0 adds origin-bound GitHub Issue retrieval through gh with explicit author allowlists, strict markers, shared intake validation and immutable transport provenance. Acknowledgement is local-only; Issues remain open. Repeated pulls reuse the original Issue binding across HEAD changes; selected transport drift blocks reuse, while unrelated historical drift is reported for reconciliation without blocking independent requests. Multiple pending requests require explicit selection, and duplicate request identities remain blocking even with `--issue`. No canonical edits, remote Issue writes, Git staging, commits or pushes occur. See `SYNC_PULL_V2.md` for setup, exact input format, limits and recovery boundaries.

Bridge v2 verification: **234 passed, 1 Windows symlink-permission skip**. Installed-wheel smoke exercises pull/plan/reuse, rejection paths, verification and dry-run finalization through mocked gh REST responses at the subprocess boundary. A separate authenticated live GET confirms that the owner-corrected open test Issue now includes the required unresolved `proposal`, passes the unchanged SYNC REQUEST v1 validator, and plans with an empty allowed write set. No production intake was performed, SportOS and the Issue were not changed, and production opt-in policy remains untouched. Historical release/verification artifacts remain unchanged.

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
