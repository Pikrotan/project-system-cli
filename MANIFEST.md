# Distribution Manifest

- Template version: 1.1.0 Stable
- CLI version: 0.2.0
- Schema version: 1
- Narrative templates: 34
- Atomic object types: 12
- Blueprint modules: 13
- JSON schemas: 15
- Machine assets: packaged in `project_cli/project_system_assets/` for source and wheel installs
- SYNC PACK assets: v1 contract document and JSON schema included in release artifacts
- Adversarial hardening: path/symlink safety, transactional module enable rollback, target-first atomic context budgeting, 8-char random IDs, strict frontmatter parsing, symmetric blueprint conflicts

## MVP CLI commands

`init`, `new`, `validate`, `generate`, `context`, `impact`, `health`, `modules`, `enable`, `disable`, `task`, `sync` (including `sync plan`), `bootstrap`, `prepare-pr`.

## Phase 1 deterministic SYNC

Version 0.2.0 adds the versioned SYNC PACK v1 contract and deterministic `project sync plan <pack>` planning. Planning validates project, Git base, approval, and target identity; resolves an allowed write set; records proposals and unresolved items without canonical writes; and emits hash-bound, idempotent artifacts only under `.generated/sync/`. Legacy `project sync <OBJECT-ID>` remains supported.

## Deliberately not automated in v1.1

No autonomous approvals, no direct LLM API calls, no live Figma/Google Docs/Sheets sync, no vector DB, no universal semantic code analyzer, no automatic merge to main.

## Security / governance boundary

Local validation proves schema/graph/filesystem invariants only. It cannot prove that a human personally supplied approval metadata. Enforced HITL requires repository-hosting controls such as protected branches and required human reviews. Knowledge frontmatter rejects aliases, duplicate keys, oversized metadata, and unsafe Python YAML constructors.

## Stable verification baseline

- Unit + adversarial regression suite: **59 passed, 1 platform-permission skip, 0 failed**.
- Source-tree end-to-end flow: **passed**.
- Wheel build: **passed**; packaged assets verified inside wheel.
- Installed console entry point outside source checkout: `project --version → init → validate`: **passed**.
- Clean target installation from wheel (dependencies supplied by host): `init → new → validate → generate → enable backend → enable payments → validate`: **passed**.

## External review status

- External adversarial review round 3 reported **26 passed, 0 failed** and recommended promotion of RC2 to Stable.
- Stable promotion was also re-verified locally with **26 passed, 0 failed**.
