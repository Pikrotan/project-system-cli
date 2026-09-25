# Changelog

## CLI 0.12.0 — 2026-09-25
- Add Skills Architecture v1: seven core portable workflows under `.agents/skills/<name>/SKILL.md`, a strict `.project/skills.yaml` registry, and conditional `design-handoff` materialization when design integration is enabled. Skills orchestrate bounded semantic work and never replace canonical knowledge, policy, schemas, deterministic logic or human approval.
- Add `project skills list`, `project skills validate`, and dry-run-first `project skills install [--apply]`; make new-project initialization, context, task and the existing knowledge-bootstrap flow Skills-aware, and generate a disposable Skills index.
- Bind selected Skill snapshots, hashes, registry identity, task scope, effective write scope and per-path authorization into new SYNC plans; verify the same evidence through verification and finalization while preserving genuine v0.11 project/artifact compatibility and rejecting partial evidence or downgrade attempts.
- Enforce the portable Skills contract with deterministic schema/registry validation, an allowlist of non-executable capabilities, protected and case-insensitive write roots, strict activation-marker typing, contained paths and immutable snapshot/hash evidence. Skills cannot execute arbitrary scripts or self-approve meaning-changing decisions.
- Add explicit legacy-project migration with fail-closed clean-Git preflight, exact stock-v0.11 identity checks, pre-write revalidation and transactional rollback. Existing divergent project constitutions are never silently overwritten.

## CLI 0.11.0 — 2026-09-15
- Add the universal Google Workspace / Designer Bridge with `project google connect|status|disconnect` and `project google workspace init|status|rebind|sync`; Git canonical knowledge remains the sole source of truth.
- Use the official Google OAuth/API libraries with the single `drive.file` scope. Validate OAuth Desktop App configuration, require long-lived refresh capability, and prohibit browser authorization from all normal/background operations.
- Store OAuth client configuration and user refresh material as separate Windows DPAPI-protected records outside every repository. Never persist tokens or credentials in project configuration, bindings, generated state/events, packs or diagnostics; ship no plaintext fallback on unsupported operating systems.
- Create and metadata-bind an optional project folder, Project Overview Doc, Design Knowledge Doc and Design Changes Sheet. Store integrity-sealed resource identities and projection hashes in the worktree-specific Git administrative store, fail closed on foreign/deleted/ambiguous resources, and support metadata-based recovery through `workspace rebind`.
- Add deterministic one-way Project Overview and designer-facing Design Knowledge projections from active canonical Git narratives/objects. Explicitly separate unresolved design questions, exclude non-current material, retain canonical Figma links, detect manual Doc drift by hash and restore from Git without any Docs-to-Git import path.
- Add the Design Changes Sheet contract with designer fields, validated Logic Changed choices and protected system columns. Require textual What Changed content even with Figma URLs, allocate collision-resistant stable Change IDs and preserve identity across retry/reorder.
- Store every successfully imported designer payload once as an immutable, integrity-sealed record outside the worktree. Reject duplicate IDs, preserve the original after edited-after-import drift, make status-write interruptions retryable and keep malformed rows actionable without creating canonical truth.
- Preserve the human semantic boundary: designer rows never create approved SYNC packs. A separately human-approved standard SYNC REQUEST may bind to a Change ID via `source.type=google_design_change`; existing durable lifecycle evidence then drives Sheet `IN REVIEW`, `APPLIED`, reviewed-no-change, rejected or abandoned feedback.
- Extend the existing single bounded pickup/watcher/Windows Scheduled Task cycle with an optional non-interactive Google branch. Do not add a daemon/task, semantic editing, staging, commit, push or GitHub mutation; preserve v0.10 headless process behavior and make projects without Google configuration follow the unchanged legacy path.
- Add Google binding/import schemas, comprehensive architecture/upgrade/live-smoke documentation and a designer-only guide. Automated tests use fake Google boundaries and no credentials or network; real production Google smoke remains separately owner-authorized.
- Remove each embedded canonical Markdown document's leading document-level H1 before projection, preventing duplicate section titles while retaining inner headings and body text without modifying canonical sources.
- Limit Design Changes protection to the exact system-managed `J:P` range; designer columns remain `A:I` and unrelated `Q+` columns are not protected by Project System.
- Replace the existence-based intake marker with a crash-released OS file lock plus bounded owner metadata. Atomically publish a fully initialized magic-prefixed marker before opening/locking it, so a POSIX crash cannot leave a new v0.11 artifact indistinguishable from a legacy empty marker. Live writers remain mutually exclusive regardless of artifact age, crashed owners recover without PID guessing, ambiguous empty POSIX markers remain fail-closed, and legacy empty Windows markers recover only after an exclusive-handle proof.
- Record completion of the owner-operated SportOS Google live-smoke gate, including OAuth/DPAPI, projections and drift restoration, Design Changes import/conflict/recovery, human-approved reviewed-no-change feedback, scheduled background cycles, revoke/reconnect recovery and a final clean SportOS worktree.

## CLI 0.10.0 — 2026-09-11
- Add Windows Automatic SYNC Runtime v1 with `project sync auto install [--interval 120] [--replace]`, read-only `project sync auto status [--json]`, and idempotent `project sync auto remove`.
- Register a current-user, least-privilege Windows Scheduled Task which uses `IgnoreNew` and `StartWhenAvailable` and invokes the internal cwd-independent `project sync auto run --registration <ID>` action for exactly one existing bounded pickup cycle.
- Store one integrity-sealed, collision-safe deterministic registration per project/worktree under `%LOCALAPPDATA%\ProjectSystem\watchers\`, outside canonical content and Git state; support multiple projects with independent task names, intervals, locks, and operational state.
- Prove task ownership from the deterministic identity, exact Project System description, current-user principal, absolute runtime executable and fixed argument vector before replacement/removal. Refuse damaged, mismatched, traversal, symlink/junction, or inaccessible ownership state rather than touching an unknown task.
- Make install idempotent, require explicit `--replace` for changed settings, verify reread task configuration, and roll registration/task state back after creation failure where ownership can be proven.
- Reuse the 0.9 watcher OS lock, crash-safe state/event log and recovery implementation in scheduled mode while delegating queue selection and manual-pull races to the unchanged intake-locked pickup core.
- Treat normal and retryable bounded outcomes as completed scheduler invocations; persist sanitized error categories and fail non-zero for human-required malformed/configuration/integrity conflicts without storing Issue bodies, request payloads, secrets, or raw exception messages.
- Preserve `project sync watch`, `project sync watch --once`, all Bridge/terminal commands and their authority boundaries. Automatic runtime never performs semantic edits, verification/finalization, Git staging/commit/push, GitHub mutation, package upgrade, or arbitrary project-configured command execution.
- Include the OS-neutral registration/runtime module, mockable Windows Task Scheduler XML adapter, and `SYNC_AUTO_WINDOWS_V1.md` in source/wheel/sdist assets. Tests and packaged smoke use a fake scheduler; real Task Scheduler installation remains an explicit owner-operated live check.
- Harden the unreleased Windows reread verifier after live installation feedback: compare account-name/SID identity, executable paths, fixed argument tokens, case-normalized scheduler enums, booleans, and ISO-8601 durations semantically while keeping task marker/action/principal ownership fail-closed. Verification failures now report bounded field-level expected/actual mismatches before the owned-task rollback result, without emitting raw XML, Issue content, credentials, or arbitrary task arguments.
- Treat an omitted exported `RunLevel` specifically as Task Scheduler's `LeastPrivilege` default. Explicit `LeastPrivilege` remains accepted, while `HighestAvailable` remains a hard configuration mismatch; no default is inferred for other security-critical enums.
- Replace the scheduled console-Python action with the same-installation `pythonw.exe -m project_system.sync_auto_runner --registration <ID>` no-console action. The fixed internal runner delegates one bounded cycle, preserves meaningful exit codes, catches unhandled failures without a GUI dialog, and records only sanitized state/event categories. Missing `pythonw.exe` fails safely with no console fallback; prior unreleased console registrations require explicit ownership-proven `--replace`.
- Propagate an explicit background execution context through the bounded scheduled cycle and centralize every reachable Git/GitHub subprocess boundary. On Windows, scheduled `git.exe` and `gh.exe` children now use `CREATE_NO_WINDOW` with argument lists, `shell=False`, capture, timeout and error semantics preserved; foreground/manual CLI processes remain unchanged.

## CLI 0.9.0 — 2026-09-10
- Add the Stage 2A persistent foreground runtime through `project sync watch`, retaining `project sync watch --once` as the unchanged bounded-cycle command and supporting a validated `--interval` of 60–3600 seconds (120 seconds by default).
- Reuse the 0.8 pickup core for every cycle, preserving the single-active-request model, oldest-first fail-closed queue, immutable intake, shared intake lock, completed lifecycle tolerance, and request-identity drift protection without duplicating transport or queue logic.
- Hold a separate project/worktree watcher lock using crash-released OS file-lock primitives, preventing concurrent persistent watchers while continuing to serialize automatic and manual intake through the existing intake lock.
- Write disposable, atomic `.generated/sync/auto/state.json` runtime state and a sanitized append-only `events.jsonl` operational log. Durable processed identity remains exclusively in the 0.7+ binding layer.
- Apply bounded exponential backoff to temporary transport failures and operational dirty/awaiting-push/transaction blocks, capped at 1800 seconds, and reset to the configured interval after normal cycles. Configuration, malformed-head and integrity/identity conflicts stop fail-closed for human intervention.
- Handle Ctrl+C as a clean foreground shutdown with stopped state, event logging and watcher-lock release. The watcher performs no semantic edits, verification, finalization, canonical writes, staging, commit, push, arbitrary command execution or GitHub mutation.
- Recover deterministically from an interrupted final `events.jsonl` append: preserve all valid complete records, normalize a valid final object without LF, discard only an invalid non-terminated tail under the watcher lock, and fail closed on interior or newline-terminated corruption. Recovery logs only sanitized tail length/hash metadata.
- Keep Task Scheduler, autostart, background services, and automatic install/status/remove lifecycle management pending a later stage.

## CLI 0.8.0 — 2026-09-09
- Add the OS-neutral Automatic Pickup Core and `project sync watch --once`. One invocation performs exactly one bounded queue inspection and creates/plans at most one immutable SYNC pack; it is not a persistent watcher.
- Reuse Bridge v2 repository discovery, GitHub GET transport, marker/author policy, strict request extraction/validation, transport drift detection, Bridge v1 intake, Phase 1 planning, and the 0.7 active/completed binding authority without semantic duplication.
- Use a stable oldest-first queue ordered by ascending GitHub Issue number. Ordinary, unauthorized, closed and already-completed Issues are skipped; a malformed authorized head, duplicate request identity or completed transport drift blocks unattended progress rather than silently bypassing the request.
- Add structured `created`, `processed`, `no_pending`, `blocked_active`, `blocked_awaiting_push`, `blocked_dirty`, `blocked_transaction`, `blocked_malformed`, `blocked_conflict`, `blocked_config`, and `blocked_transport` cycle results for CLI and future scheduler/state consumers.
- Serialize automatic selection through intake/plan with manual pull and direct intake using the existing local intake lock, closing the cooperative selection-to-create race without adding a daemon lock or OS-specific scheduler behavior.
- Keep automatic authority transport-only: the cycle may write one immutable `inbox/sync/<PACK_ID>.yaml` and derived `.generated` intake/plan/pull reports, but never edits canonical knowledge/docs, verifies, finalizes, stages, commits, pushes or mutates GitHub.
- Preserve the 0.7 lifecycle and all older commands/configuration. Persistent polling, watcher loops, autostart/install/status/remove, Task Scheduler, notifications, services and desktop UI remain pending future stages.
- Treat mutable lifecycle metadata of an already completed GitHub Issue (`state`, `state_reason`, `updated_at`, `closed_at`) as provenance-only during unattended reconciliation. Repository/Issue/author/title/body/request identities and their hashes remain fail-closed drift invariants, including for existing 0.7 terminal bindings.

## CLI 0.7.0 — 2026-09-09
- Add Durable Terminal SYNC Binding v1: exact completed pack bytes and an integrity-sealed binding are stored beneath the worktree-specific Git administrative directory, outside the working tree and independently of disposable `.generated/**` reports.
- Add one active/completed binding loader for pack/request/Issue collision detection, GitHub pull idempotency, transport drift checks, intake reuse boundaries and future queue consumers.
- Make GitHub transport commit-only finalization remain active with `transport_state: awaiting_push`; archive and remove the inbox pack only after a proven successful push, including safe retry after a commit succeeded but push failed.
- Add explicit `project sync finalize <PACK> --complete --outcome reviewed-no-change|rejected|abandoned --reason ...` terminal actions. Reviewed no-change requires passed verification; rejected/abandoned require an intact unchanged transport and a clean canonical baseline.
- Publish completed records exclusively and crash-safely, detect active/archive mismatches and transaction tampering, and recover an identical duplicate left by a crash after archive publication but before inbox cleanup.
- Preserve completed GitHub Issue/request identity across HEAD changes and `.generated` deletion; unchanged Issues remain processed, while body/title/author/URL/update-time drift and request identity collisions remain blocking.
- Add audit-only `project sync migrate-bindings` and explicit `--apply`, archiving only legacy GitHub transport records with integrity-checked verification/finalization and locally proven pushed commit/upstream state. Ambiguous, prepared, unverified and direct-intake records are not guessed.
- Preserve direct intake and all 0.6.0 commands. No watcher, polling, Task Scheduler, background process, notification, semantic executor, Issue mutation, implicit commit or automatic push is added.

## CLI 0.6.0 — 2026-09-07
- Add universal `project sync pull` and `project sync pull --plan`, deriving the GitHub repository from origin and using local gh GET calls for transport/authentication without storing tokens.
- Accept only open Issues with the exact `[SYNC REQUEST]` title prefix and an explicitly allowlisted creator; support an optional expected-repository pin and fail closed without opt-in policy.
- Require exactly one raw or fenced SYNC REQUEST v1 document, treating surrounding prose as inert, and delegate parsing, validation, binding, immutable pack creation and planning to existing Bridge v1 components.
- Add optional pack/intake transport provenance: repository, Issue number/URL/author/update time and body/request/title SHA-256 hashes. Older requests/packs and Phases 1–3 remain compatible.
- Acknowledge successful input locally with generated pull receipts; never close, label or comment on Issues. Repeated pulls preserve the original binding even at another HEAD; selected-Issue drift blocks reuse, while unrelated historical drift is isolated as a reconciliation warning.
- Reject duplicate request identities across relevant open transport candidates even with `--issue`, plus wrong repositories, unauthorized authors, malformed/unsafe input and output escapes; fail without partial batch processing when multiple candidates require explicit selection.
- Preserve canonical content, selected-pack-only clean-baseline exceptions, and all 0.5.0 commands. Pull never stages, commits, pushes, edits semantics or invokes an LLM.

## CLI 0.5.0 — 2026-09-06
- Add Bridge v1 / deterministic SYNC intake with a versioned SYNC REQUEST v1 transport contract sharing change definitions and target validation with SYNC PACK v1.
- Add `project sync intake <REQUEST_PATH>` for approved request files and `project sync intake -` for UTF-8 stdin/pipe workflows, including `Get-Clipboard | project sync intake -` when the shell uses UTF-8. Optional `--plan` runs existing deterministic planning after intake.
- Bind `project_id` from local `project.yaml` and `base_commit` from current Git HEAD, allocate a collision-resistant `SYNC-YYYYMMDD-<8 lowercase hex chars>` pack ID, and record the local UTC intake time without semantic interpretation.
- Preserve approval/source/change data and the original request ID, exact-byte SHA-256 and intake timestamp in the backward-compatible optional SYNC PACK `provenance` block.
- Exclusively create immutable input packs at `inbox/sync/<PACK_ID>.yaml`; never overwrite an existing pack. Emit derived `intake.json` and `intake.md` reports under `.generated/sync/<PACK_ID>/`.
- Reuse an unchanged pack for identical request bytes, project and HEAD, preserving pack identity, timestamp and bytes. At a different HEAD, create a new locally bound pack and preserve the earlier binding. Reject revised bytes reusing an existing request ID.
- Reject request-supplied `project_id`, `base_commit`, `pack_id`, output paths and other unsupported fields; validate schema/version, approval timestamps, duplicate keys/change IDs, target IDs, expected targets, narrative paths and create-object identities before pack creation.
- Reject unsafe YAML, anchors/aliases, oversized input, traversal, symlink/junction output escapes, duplicate bindings and pack overwrite attempts; serialize cooperating intake writers with a local generated lock.
- Keep the clean-baseline exception limited to the exact selected pack and `.generated/**`, not the whole inbox. Intake with `--plan` requires a clean planning baseline; intake alone permits structurally valid existing edits without making canonical writes.
- Preserve `proposal` and `unresolved` as non-canonical input, and never apply semantic edits, modify canonical knowledge/docs, commit, push or call an LLM during intake. Approval validation remains structural, not proof of human identity.
- Include `SYNC_REQUEST_V1.md`, `sync-request.schema.json`, the shared pack contract/schema and intake runtime in package assets. Preserve legacy `project sync <OBJECT-ID>` and `project sync plan`, `project sync verify`, `project sync finalize` behavior.

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
