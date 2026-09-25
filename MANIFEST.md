# Distribution Manifest

- Template version: 1.1.0 Stable
- CLI version: 0.12.0
- Schema version: 1
- Narrative templates: 34
- Atomic object types: 12
- Blueprint modules: 13
- JSON schemas: 20
- Packaged project Skills: 8 entrypoints (7 core, 1 conditional design handoff)
- Machine assets: packaged in `project_cli/project_system_assets/` for source and wheel installs
- SYNC PACK assets: v1 contract document and JSON schema included in release artifacts
- SYNC REQUEST assets: Bridge v1 contract document and shared-definition JSON schema included in release/package assets
- SYNC pull assets: Bridge v2 contract, transport runtime and optional policy/provenance schemas included in package assets
- Terminal SYNC assets: durable binding v1 contract/schema, Git-admin completed store, migration runtime and crash-safe terminalization included in package assets
- Automatic pickup assets: bounded-cycle contract and OS-neutral pickup runtime included in release/package assets
- Foreground watcher assets: persistent runtime, watcher contract, lock/state/event/backoff implementation included in release/package assets
- Windows automatic SYNC assets: external registration runtime, packaged no-console background runner, mockable Task Scheduler XML adapter and Windows automation contract included in release/package assets
- Google Workspace assets: OAuth/DPAPI boundary, Drive/Docs/Sheets adapter, durable workspace/import bindings, deterministic projections, Design Changes intake, architecture and designer guides included in source/wheel/sdist assets
- Skills Architecture assets: v1 contract, strict registry schema, catalog, 8 portable Skill entrypoints, stock-v0.11 migration identities and deterministic validation/migration runtime included in source/wheel/sdist assets
- Adversarial hardening: path/symlink safety, transactional module enable rollback, target-first atomic context budgeting, 8-char random IDs, strict frontmatter parsing, symmetric blueprint conflicts

## CLI commands

`init`, `new`, `validate`, `generate`, `context`, `impact`, `health`, `modules`, `enable`, `disable`, `task`, `google` (including `connect/status/disconnect` and `workspace init/status/rebind/sync`), `sync` (including `sync auto install/status/remove`, persistent `sync watch`, bounded `sync watch --once`, `sync pull`, `sync intake`, `sync plan`, `sync verify`, `sync finalize`, and `sync migrate-bindings`), `bootstrap`, `skills` (`list`, `validate`, and dry-run-first `install [--apply]`), `prepare-pr`.

## Skills Architecture v1

Version 0.12.0 adds a project-local orchestration layer at `.agents/skills/<name>/SKILL.md`, registered by `.project/skills.yaml`. Fresh projects receive seven core Skills; `design-handoff` is conditional on an enabled design integration. Context, task, knowledge-bootstrap and new deterministic SYNC evidence can bind immutable selected-Skill snapshots and the intersection of Skill ceilings, task/SYNC scope and governance authorization. Skills cannot approve meaning changes, execute arbitrary registry commands, bypass protected roots, make `.generated/**` semantic, or grant external-provider authority. Explicit dry-run-first migration upgrades proven stock v0.11 constitutions transactionally while genuine legacy v0.11 projects and complete legacy artifacts remain compatible. See `SKILLS_ARCHITECTURE_V1.md`.

Skills release verification: full final suite **491 passed, 3 environment-specific skips, 0 failed in 360.49s**. The skips are the existing unavailable Windows symlink/junction privilege cases and the unavailable DPAPI user profile in the managed test process. Source CLI/runtime reports `0.12.0`; wheel/sdist and installed-wheel verification are recorded in the release-preparation report.

## Google Workspace / Designer Bridge

Version 0.11.0 adds a project-neutral, opt-in Google Workspace made of Project Overview, Design Knowledge and Design Changes resources while preserving Git canonical knowledge as the only SSOT. OAuth Desktop App bootstrap requests only `drive.file`; client/token material is separated and protected with Windows DPAPI outside repositories. Resource IDs and immutable designer imports live in integrity-sealed worktree-specific Git administrative storage. Git-to-Docs projections detect and restore manual drift, while Sheet rows become stable, immutable proposals and never approved canonical truth. A separately human-approved standard SYNC REQUEST can reference a Change ID, after which existing terminal lifecycle evidence drives Sheet feedback. The existing bounded watcher/Windows task gains one non-interactive Google branch without a second daemon/task, browser UI, canonical mutation, staging, commit, push or provider mutation beyond the explicitly bound Workspace resources. See `GOOGLE_WORKSPACE_BRIDGE_V1.md` and `DESIGN_CHANGES_DESIGNER_V1.md`.

Google Workspace verification: focused Google/intake/pickup regression **178 passed, 2 skipped**; targeted intake-lock regression **32 passed**; full release-candidate suite **428 passed, 3 environment-specific skips** (two unavailable Windows symlink/junction privileges and one unavailable DPAPI user profile in the managed test process). A fresh wheel and sdist were built in temporary storage; the installed-wheel smoke verified CLI/runtime metadata `0.11.0`, importability, packaged hardening code/assets, embedded-H1 cleanup and reusable crash-safe intake locking. Separately, the owner-operated real SportOS gate confirmed OAuth/DPAPI, workspace creation, one-way projections and drift restore, Design Changes identity/conflict/recovery, human-approved reviewed-no-change feedback, scheduled background cycles, revoke/reconnect recovery and a clean final worktree. Automated tests and packaging used no production credentials or resources.

## Windows Automatic SYNC Runtime — Stage 3

Version 0.10.0 adds an external per-project registration and current-user, least-privilege Windows Scheduled Task. Each invocation runs the same-installation `pythonw.exe -m project_system.sync_auto_runner --registration <ID>` no-console action for exactly one existing bounded pickup cycle, then exits; an explicit background context makes every reachable `git.exe`/`gh.exe` child use Windows `CREATE_NO_WINDOW` while foreground commands remain unchanged. Task Scheduler remains cadence/reboot authority and `IgnoreNew` is reinforced by the existing watcher OS lock. Registration is integrity-sealed beneath `%LOCALAPPDATA%\ProjectSystem\watchers\`, task ownership is proven before replacement/removal, and install is idempotent with explicit controlled `--replace` and rollback. Read-only status includes scheduler/registration identity plus advisory state and version/runtime warnings. No canonical semantic edit, verify/finalize, staging, commit, push, GitHub mutation, administrator requirement or arbitrary configured command is introduced. See `SYNC_AUTO_WINDOWS_V1.md`.

Windows automation verification: **362 passed, 2 Windows symlink-permission skips**. Coverage includes install/idempotency/replace/rollback, status and JSON output, missing/damaged state, ownership-safe removal, bounded scheduled runs and locks, semantic Windows XML normalization including the omitted-`RunLevel` `LeastPrivilege` default, exact sanitized mismatch diagnostics, same-installation `pythonw.exe` discovery, strict no-console runner identity/arguments, explicit background propagation to every reachable Git/GitHub child process, preserved foreground behavior and subprocess error/timeout/capture semantics, background exit/error recording, multiple projects, safe registration paths, unchanged foreground watcher behavior, and all Bridge/terminal regressions. Packaged fake-scheduler install/status/one-cycle/remove smoke passed from the isolated 0.10.0 wheel in a Unicode path with spaces; direct installed process probes reported no console for both the `pythonw.exe` runner and a console-subsystem child launched under the background context, while foreground invocation omitted the flag. Wheel and sdist contain the background runner, central process helper and Windows contract. No real Scheduled Task or production project was modified, and live Windows installation remains a separate owner-operated step.

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

No autonomous approvals, no direct LLM API calls, no Figma API/image analysis,
no Google Docs-to-Git canonical import, no vector DB, no universal semantic code
analyzer, and no automatic merge to main.

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
