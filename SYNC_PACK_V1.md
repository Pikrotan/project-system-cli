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
