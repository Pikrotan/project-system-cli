# Project System Bridge v2 / SYNC pull

CLI 0.6.0 adds a transport adapter, not a semantic executor:

```text
approved discussion → marked GitHub Issue → local gh GET
→ shared SYNC REQUEST v1 validator → existing intake → immutable pack
→ optional plan → external semantic edit → verify → finalize
```

## Opt-in author policy

Configure the intended project through its normal human approval path:

```yaml
external_systems:
  github:
    enabled: true
    sync_pull:
      allowed_authors:
        - alice
      expected_repository: example-owner/example-project
```

`allowed_authors` is mandatory, nonempty and case-insensitively unique. These are GitHub Issue creator logins, not request `approved_by` strings. There is no implicit trust in the current gh login, repository owner, collaborators or organization members. Bots also require explicit allowlisting. `expected_repository` is an optional recommended pin that must match origin. Projects without this opt-in remain compatible with CLI 0.5.0; pull fails closed until enabled. The project schema validates the optional policy; pull additionally checks enabled state and normalized identities.

The repository comes only from one `git remote origin` fetch URL. HTTPS and git SSH URLs for `github.com` are supported, with optional `.git` suffix. Embedded credentials, traversal, other protocols/hosts, ambiguous origins and a mismatched repository pin are rejected. `GH_REPO`/`GH_HOST` cannot select a different destination: API calls specify the repository and host. GitHub Enterprise hosts are not supported in this version.

## Issue contract

- Open Issue, not a pull request.
- Exact case-sensitive title prefix `[SYNC REQUEST]`. `[SYNC REQUEST][TEST] Transport fixture` is valid; a normal Issue merely mentioning the marker later is not.
- Creator login is in the local allowlist before body acceptance.
- Body uses one deterministic transport mode. In raw mode, the complete Issue body is parsed as exactly one YAML/JSON SYNC REQUEST v1 document; leading/trailing whitespace is accepted by the parser but remains part of the request hash. In fenced mode, the body contains exactly one `yaml`, `yml` or `json` triple-backtick block and only that block is parsed; surrounding text is inert human prose and remains covered by the full-body hash. Two or more fenced blocks, an untagged/unsupported fence, malformed fenced content, multiple YAML documents, attachments and comment-based requests are rejected. Fence delimiters must start at the beginning of a line.
- The request remains [SYNC REQUEST v1](SYNC_REQUEST_V1.md). Shared intake validates syntax, schema, approval, change IDs, target IDs, narrative paths and canonical state before pack creation. Requests cannot supply local project/HEAD/pack identity, transport metadata or output paths.

Example body (unresolved fixture, not an active decision):

```yaml
schema_version: 1
request_id: REQUEST-transport-example
source:
  type: external_discussion
  ref: approved-discussion-reference
approval:
  approved_by: example-project-owner
  approved_at: 2026-09-07T10:00:00Z
change_class: C
changes:
  - change_id: keep-open
    kind: unresolved
    summary: No implementation choice has been approved.
    proposal: Leave this item unresolved and non-canonical.
expected_targets: []
notes: Transport example only.
```

## Commands and selection

```text
project sync pull
project sync pull --plan
project sync pull --issue 123
project sync pull --issue 123 --plan
```

Discovery searches open marked Issues, enforces the exact prefix locally and refreshes individual snapshots via the Issues API. Explicit selection still performs this scoped transport discovery to detect duplicate request identities, while adding the selected number independently so search-index lag cannot hide it. Already bound Issues are re-read even if subsequently closed or unmarked, so removing a marker cannot hide drift. Incomplete search, duplicate responses, more than 1000 hits, empty intermediate pages and oversized responses fail rather than silently truncate. `--issue NUMBER` never bypasses author, marker, repository, duplicate-identity or selected-Issue drift checks.

Exactly one pending Issue is processed per invocation. Multiple pending candidates fail without writing packs and list the numbers for explicit selection. Duplicate request IDs across valid open transport candidates and local transport bindings fail even with `--issue`, rather than attach another Issue to an existing request identity. No candidates means successful `Status: no_pending`. One unchanged processed Issue returns `Status: already_processed`; multiple processed Issues with no pending work return `no_pending`, while replanning requires an explicit number.

Drift of the selected Issue is blocking and never overwrites or rebinds its immutable pack. Drift or inaccessibility of an unrelated older processed Issue is isolated as a local `reconciliation needed` warning and excluded from pending candidates, so independent valid requests remain processable. Selecting that older Issue explicitly makes its drift blocking again. With explicit selection, invalid unrelated discovered candidates are likewise warnings; the selected Issue must always pass the complete contract.

`--plan` invokes existing planning. Only the selected pack and `.generated/**` are exempt from the clean baseline. Other untracked inbox files/packs are not automatically staged or ignored. Output includes `Repository`, `Status`, and, for a selected Issue, `Issue`, `Pack`, `Path`, `Base`, `Acknowledgement: local_only`; successful `--plan` adds `Plan: ready`. Success is exit `0`; transport, policy, validation or drift refusal is exit `2`. Intake/finalization-only arguments are rejected. Legacy object sync, intake, plan, verify and finalize are preserved.

## gh and credentials

The adapter invokes installed gh without a shell, only as `gh api --hostname github.com --method GET`. The method is explicit even with query fields. It does not obtain, print, store or manage tokens; authentication stays with gh. On Windows a standard GitHub CLI installation under Program Files is also discoverable when PATH is missing it. Calls have a 30-second timeout and bounded accepted response sizes; gh failures do not echo potentially sensitive stderr. No Issue editing, closing, commenting, labeling, commit, push, `git add -A`, provider API integration or LLM call is performed.

## Immutable provenance and acknowledgement

Pack location remains `inbox/sync/<PACK_ID>.yaml`. Existing source/approval data is preserved. Optional `provenance.transport` records `kind: github_issue`, repository, Issue number/URL, author, GitHub `updated_at`, and SHA-256 of the complete UTF-8 body, extracted request bytes and title. This also appears in both intake reports. Phases 1–3 bind the whole pack through their existing pack SHA-256.

For a fence, the extracted hash excludes the opening fence line and newline immediately before its closing fence. GitHub may normalize pasted line endings: this proves fetched bytes, not original clipboard bytes. External requests cannot inject this locally obtained transport block.

Acknowledgement is **local only**. After successful intake and a final unchanged-Issue check, `.generated/sync/<PACK_ID>/pull.json` and `pull.md` record the binding/hashes and `remote_action: none`. No remote state changes, labels or comments are needed. The Issue stays open; a human can close it separately. Receipt of input is not successful semantic application.

While active, the immutable intake pack is the durable local processed identity; generated receipts are disposable and reconstructible. After terminal completion, CLI 0.7.0 preserves the exact pack bytes and a sealed binding in the worktree-specific Git administrative store, removes the inbox copy, and continues to compare the selected live snapshot to that completed identity. Body/title/author/URL/`updated_at` changes are drift, even when request bytes still match. Deleted/inaccessible processed Issues require reconciliation. Timestamp-only GitHub updates can therefore require review; unrelated drift is reported without globally disabling intake. See `SYNC_TERMINAL_V1.md`.

Unlike direct intake, pull never creates another binding for a processed Issue merely because HEAD advanced. It returns the original pack; `--plan` against an old HEAD fails as stale. Intentional new synchronization needs a separately approved new request/Issue. Direct intake retains its 0.5.0 same-bytes/new-HEAD behavior. A request previously ingested without matching transport cannot acquire that provenance by overwriting its pack; use a reviewed new request identity.

## Failure and trust boundaries

Invalid Issues create no pack/acknowledgement. Multiple-candidate refusal makes no partial batch edits. Network/planning failure after valid pack creation retains it without a successful pull receipt; retry inspects/reuses it. Cooperating writers use the existing intake lock and exclusive creation. Output symlinks/junctions and traversal are checked before writes.

Local acknowledgement is not global exactly-once delivery: a clone without intake artifacts can import the Issue again. Preserve/share provenance through the reviewed repository workflow. Hashes are not signatures; Issue authorship is not proof of the last body editor or human approval identity. Compromised allowlisted accounts and privileged local filesystem tampering remain outside these guarantees. Re-fetches detect ordinary drift, but GitHub and filesystem writes are not one distributed transaction.

The read-only `inspect_pull` layer exposes discovery/author/schema/drift checks without generated writes, but still requires project policy. It does not replace canonical target validation in intake. Production smoke must not call mutating `pull_sync` unless local artifact writes are separately authorized.
