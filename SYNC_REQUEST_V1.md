# SYNC REQUEST v1 / Bridge v1

SYNC REQUEST is an approved external transport artifact, not canonical product truth and not a locally bound SYNC PACK. Intake copies supplied semantic content; it does not interpret discussion, infer approval, invent IDs for canonical objects, apply edits, call an LLM, or commit/push.

```text
external approved discussion
→ SYNC REQUEST v1
→ project sync intake
→ immutable local SYNC PACK v1
→ project sync plan
→ external semantic edit → verify → finalize → explicit commit → explicit push
```

## Contract

The machine schema is `project_cli/project_system_assets/schemas/sync-request.schema.json`. It references the installed `sync-pack.schema.json` locally, without network access, so both formats share source, approval, change-class, changes, and expected-target definitions. The same in-memory resolver validates intake and planning targets.

All eight top-level properties are required; any other property is rejected:

| Property | Contract |
| --- | --- |
| `schema_version` | Integer `1`. |
| `request_id` | 1–128 ASCII letters, digits, dots, underscores or hyphens; starts with a letter or digit. Stable identity of this exact transport artifact. |
| `source` | Object with required nonempty `type` and `ref`, optional `note`. |
| `approval` | Required nonblank `approved_by` and timezone-qualified `approved_at`. |
| `change_class` | `A`, `B`, `C`, or `D`, as in SYNC PACK. |
| `changes` | Nonempty array of existing SYNC PACK changes, with unique `change_id` values. |
| `expected_targets` | Exact set of direct canonical target IDs and narrative paths; inferred impact-policy docs are separate. May be empty for proposal/unresolved-only input. |
| `notes` | String; may be empty. |

Supported change kinds are `create_object`, `update_object`, `retire_object`, `narrative_impact`, `proposal`, and `unresolved`. Object identity always uses real IDs, not filenames or slugs. New object IDs must already be supplied in canonical lowercase-hex format. Proposal/unresolved items never authorize canonical writes; their existing `related_ids` are checked. Full change field definitions are in the shared pack schema and [SYNC PACK contract](SYNC_PACK_V1.md).

The external request cannot supply `project_id`, `base_commit`, `pack_id`, `created_at`, provenance, an output path, or a local repository path. Narrative paths inside changes remain project-relative `docs/*.md` targets, not arbitrary output destinations.

## Example

This fixture contains only an unresolved discussion item, with no new product decision or canonical write:

```yaml
schema_version: 1
request_id: REQUEST-20260906-aabbccdd
source:
  type: external_discussion
  ref: approved-transport-example
approval:
  approved_by: project-owner
  approved_at: 2026-09-06T10:00:00+03:00
change_class: C
changes:
  - change_id: unresolved-example
    kind: unresolved
    summary: No implementation option has been selected.
    proposal: Keep this item unresolved; do not create active canonical truth.
expected_targets: []
notes: Illustrative transport only; approval of transport is not resolution of the item.
```

## CLI

[Bridge v2](SYNC_PULL_V2.md) also accepts this unchanged request contract from a marked, allowlisted GitHub Issue through `project sync pull [--plan]`. Transport provenance is added locally; it is not a new request field.

Run inside the intended initialized Git project, with an existing HEAD:

```text
project sync intake <REQUEST_PATH>
project sync intake -
project sync intake <REQUEST_PATH> --plan
project sync intake - --plan
```

Files may use `.yaml`, `.yml`, or `.json`; stdin accepts the same UTF-8 content. An optional UTF-8 BOM is supported. The transport is bounded to 2 MiB; duplicate mapping keys (including JSON), YAML anchors/aliases, unsafe tags, and invalid schemas are rejected. Timestamps require a calendar-valid date, seconds, and a timezone (`Z` or numeric offset); leap-second notation is not supported. These checks do not depend on optional jsonschema format packages.

PowerShell can pipe UTF-8 text, for example `Get-Clipboard | project sync intake -`. Older shells may require configuring their pipe encoding to UTF-8. Hashes bind the bytes actually received by the CLI, including BOM and line endings; two shell pipelines that change encoding/line endings produce different request hashes.

Successful stdout is line-oriented:

```text
Pack: SYNC-20260906-11223344
Path: inbox/sync/SYNC-20260906-11223344.yaml
Base: <current-HEAD>
Changes: 1
Status: created
```

Reuse emits `Status: reused`. With `--plan`, success also emits `Plan: ready` and `Allowed writes: N`. Failures go to stderr with exit code `2`; success returns `0`. `--commit`, `--push`, and `--message` are not intake options. Legacy `project sync <OBJECT-ID>` and `sync plan/verify/finalize` are unchanged.

## Local binding and provenance

| Request / local source | Final pack field |
| --- | --- |
| Request source, approval, change_class, changes, expected_targets, notes | Same fields, unchanged as data; YAML formatting is reserialized. |
| `project.yaml` → `project.id` | `project_id` |
| Current local Git HEAD | `base_commit` |
| Local UTC date + cryptographically random 8-character lowercase hex suffix | `pack_id = SYNC-YYYYMMDD-xxxxxxxx` |
| Local intake UTC time | `created_at` and `provenance.intake_at` |
| `request_id` | `provenance.request_id` |
| SHA-256 of original transport bytes | `provenance.request_sha256` |
| Request `source` | `provenance.source` (also retained at the pack root) |

The newly allocated ID/time are intentionally local values, not a deterministic hash ID. Repeated intake is deterministic through reuse of the original binding. The new optional pack provenance block is backward-compatible: packs without it still work with Phases 1–3. Pack hashes and request hashes are different evidence, not interchangeable and not digital signatures. Retain the original request externally if independent byte-hash verification is needed; intake does not keep another raw request copy.

## Storage, baseline and idempotency

The only persistent writes are the exclusive new `inbox/sync/<PACK_ID>.yaml` and generated reports under `.generated/sync/<PACK_ID>/`. Intake temporarily holds `.generated/sync/.intake.lock` to serialize cooperating intake writers. Existing pack bytes are never overwritten. Symlinks/junctions in inbox/report output components, traversal, external output injection, duplicate pack IDs, duplicate request bindings, and object-ID conflicts fail closed. ID/path collisions retry allocation; allocation has a finite retry limit.

- Same request bytes + project + HEAD: reuse the existing pack, preserve its ID/time/bytes, and refresh the generated report. A changed working tree is still revalidated.
- Same request bytes at a different HEAD: create a new locally bound pack. The earlier binding is preserved.
- Same request ID with different bytes: reject; provide a new request ID for a revised transport artifact.
- Existing intake pack modified/reformatted or inconsistent with its recorded hash: reject reuse. Duplicate same-HEAD request bindings reject rather than choose one arbitrarily.

Intake without `--plan` permits a dirty but structurally valid canonical project and performs no canonical writes. Intake with `--plan` preflights Phase 1's clean baseline before pack creation. Planning excludes only the exact selected pack and `.generated/**`; it does **not** ignore `inbox/**` generally. Thus `intake → plan` works, while another unrelated untracked inbox file still blocks planning. Keep requests outside the repository or supply stdin when planning from a clean tree.

Invalid requests or failed initial validation do not create a pack. A later planning/I/O failure can leave a successfully created immutable pack; inspect the reported path and retry rather than edit it. If planning fails after creation, intake records `plan_result: failed` and the error. A process crash may leave a lock or incomplete file: inspect before manual recovery; the CLI never removes someone else's lock or silently repairs an input pack.

## Generated reports

`intake.json` and `intake.md` contain request identity/hash, source, pack identity/path/hash, project/HEAD, intake time, approval, change count/kinds, expected targets, created/reused status, whether planning was requested, plan outcome, warnings, and errors. JSON additionally includes the computed allowed-write count. With `--plan`, the existing `plan.json`, `manifest.json`, and `context.md` are emitted alongside them. Generated reports are evidence, not canonical truth.

## Boundaries

Approval is validated structurally, not authenticated. Intake cannot recover an external chat, prove human identity, determine semantic correctness, or resolve conflicting product proposals. Creation descriptions remain subject to full canonical validation after an external editor supplies complete objects; this is the existing Phase 1 contract. Verification and finalization remain separate explicit commands. Local locks and path checks assume a cooperating local filesystem; they are not isolation from a privileged concurrent process replacing directories or tampering with all integrity artifacts. No provider API, clipboard API, background service, webhook, Google integration, semantic apply, commit, or push is added by Bridge v1.
