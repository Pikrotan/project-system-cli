# SYNC PACK v1

SYNC PACK is an immutable, human-approved input artifact for deterministic synchronization planning. It is provenance, not canonical product truth. `project sync plan` never edits `knowledge/`, `docs/`, configuration, source code, or Git state, and it does not invoke an LLM.

The machine contract is `project_cli/project_system_assets/schemas/sync-pack.schema.json`.

## Storage and invocation

The stable project-local inbox is `inbox/sync/`. An explicit `.yaml`, `.yml`, or `.json` path may also be supplied:

```text
project sync plan inbox/sync/SYNC-20260905-deadbeef.yaml
project sync plan D:\approved-inputs\SYNC-20260905-deadbeef.yaml
```

Legacy object targeting remains available as `project sync <OBJECT-ID>`.

Planning requires a clean Git working tree, excluding `.generated/**` and the selected pack itself. Pre-existing gitignored files outside `.generated/**` are recorded with path, size, and SHA-256; verification permits them only while that fingerprint remains unchanged. This keeps the later verification baseline unambiguous; dirty tracked/untracked baseline planning is not supported in Phase 2.

## Change kinds

- `create_object`: declares a new collision-resistant object ID and proposed canonical content.
- `update_object`: targets one existing canonical object ID and carries a frontmatter/body patch description.
- `retire_object`: targets an existing object for an approved superseded/deprecated/rejected/removed status.
- `narrative_impact`: declares existing narrative docs directly affected by the approved change.
- `proposal`: records a non-canonical proposal; it never authorizes a canonical write.
- `unresolved`: records an unresolved item; it never authorizes a canonical write.

`expected_targets` is an exact assertion over direct canonical targets: object IDs for object changes and project-relative `docs/*.md` paths for narrative changes. Narrative docs inferred through impact policy are recorded separately.

## Example

```yaml
schema_version: 1
pack_id: SYNC-20260905-deadbeef
project_id: sportos
source:
  type: chatgpt_project
  ref: discussion-2026-09-05
created_at: 2026-09-05T10:00:00+03:00
base_commit: 0123456789abcdef0123456789abcdef01234567
approval:
  approved_by: project-owner
  approved_at: 2026-09-05T10:05:00+03:00
change_class: C
changes:
  - change_id: update-sports-profile
    kind: update_object
    summary: Apply the approved profile clarification.
    target_id: FEAT-20260902-d1978ca3
    patch:
      body: |
        Approved replacement body supplied by the semantic editor workflow.
  - change_id: product-doc-impact
    kind: narrative_impact
    summary: Keep the product narrative consistent with the approved clarification.
    narrative_paths:
      - docs/03_PRODUCT.md
  - change_id: unresolved-feed-ranking
    kind: unresolved
    summary: Feed ranking remains undecided.
    proposal: Evaluate ranking options later; do not activate one during this sync.
    related_ids:
      - FEAT-20260902-d1978ca3
expected_targets:
  - FEAT-20260902-d1978ca3
  - docs/03_PRODUCT.md
notes: Planning only; semantic apply is outside Phase 1.
```

## Deterministic outputs

For a valid pack at the exact current `HEAD`, planning writes only:

```text
.generated/sync/<pack_id>/plan.json
.generated/sync/<pack_id>/manifest.json
.generated/sync/<pack_id>/context.md
```

The manifest includes the SHA-256 hash of the exact pack bytes, approval metadata, resolved targets, allowed writes, protected/out-of-scope paths, proposal/unresolved items, warnings, and errors. Replanning unchanged content at the same `HEAD` produces equivalent bytes. Reusing a planned `pack_id` with changed content is rejected.

`plan.json` and `manifest.json` carry the same deterministic SHA-256 integrity block over their canonical JSON payloads. Verification rejects missing, mismatched, or changed planning artifacts. These hashes detect accidental or uncoordinated tampering; they are not a digital signature and do not replace repository access controls.

## Phase 2 verification

After an external semantic executor changes canonical files, verify the result with either the original pack path or its pack ID:

```text
project sync verify D:\approved-inputs\SYNC-20260905-deadbeef.yaml
project sync verify SYNC-20260905-deadbeef
```

Pack-ID lookup uses the source path recorded in the validated manifest, so the original pack must remain readable. Verification checks pack, plan, manifest, project, Git HEAD, and allowed-write integrity before reading the actual staged, unstaged, tracked, untracked, deleted, and renamed change set relative to `base_commit`. The exact selected pack and `.generated/**` are excluded; every other changed path must be in the validated plan's `allowed_write_set`. A canonical path with divergent staged and unstaged content is rejected because a single working-tree validation cannot certify both versions.

On a valid scope, verification executes the deterministic pipeline `validate → generate → validate`, confirms generation did not change anything outside `.generated/**`, and writes only:

```text
.generated/sync/<pack_id>/verification.json
.generated/sync/<pack_id>/verification.md
.generated/sync/<pack_id>/diff-summary.md
```

Exit codes are `3` for integrity/preflight failure, `4` for Git scope violation, and `5` for canonical validation failure. Reports explicitly do not claim semantic correctness: human semantic review remains required. Verification never edits canonical content, repairs violations, invokes an LLM, commits, pushes, or creates a pull request.
