# Google Workspace / Designer Bridge v1

Project System CLI 0.11.0 adds a project-neutral bridge between canonical Git
knowledge and a small Google Workspace. Git remains the only source of truth.
The bridge is deterministic infrastructure: it does not infer product meaning,
approve designer input, edit canonical files, call an LLM, commit, push, or
create another watcher or Scheduled Task.

```text
canonical Git knowledge
  -> Project Overview projection
  -> Design Knowledge projection

Design Changes row
  -> immutable local proposal/provenance
  -> external semantic preparation + human approval
  -> existing SYNC REQUEST / pack / verify / finalize lifecycle
  -> Sheet feedback + refreshed projections
```

Figma remains visual truth. Version 0.11.0 stores and projects canonical Figma
URLs but has no Figma API, image or pixel integration.

## Configuration

Only declarative enablement belongs in `project.yaml`:

```yaml
external_systems:
  google_workspace:
    enabled: true
    project_overview: true
    design_knowledge: true
    design_changes: true
    projection_drift: restore
```

`enabled` is required when the block exists. Resource booleans default to
`true`; `projection_drift` defaults to and currently only accepts `restore`.
Old project files with no block remain valid and retain exactly their previous
runtime behavior. Resource IDs, credentials and tokens are forbidden from this
configuration by the exact schema.

## One-time Google Cloud prerequisite

An owner creates or selects a Google Cloud project, enables Drive API, Docs API
and Sheets API, configures an OAuth consent screen appropriate for the account,
and creates an OAuth **Desktop App** client. The downloaded JSON is used once
from a trusted local path. Project System does not create Cloud projects,
consent screens or OAuth clients.

The only requested scope is:

```text
https://www.googleapis.com/auth/drive.file
```

This lets the app manage files it created or which were explicitly opened for
it; it does not silently broaden access to all Drive files. Docs and Sheets are
accessed through those bound Drive files.

## Authorization commands

```text
project google connect --credentials C:\secure\desktop-client.json
project google connect
project google status
project google disconnect
```

The first connect validates a Desktop App document, opens the interactive
browser OAuth flow, requires a refresh token, and stores the client
configuration and user refresh material as two separate Windows DPAPI-protected
records beneath `%LOCALAPPDATA%\ProjectSystem\google\credentials\`. The source
JSON is never copied into the repository. A later `connect` without a path can
reuse the protected client configuration to reauthorize.
The Desktop App redirect must parse as `http://localhost` with an optional
port and root path, matching the local-server callback; hostname-prefix
lookalikes and non-loopback redirects are rejected.

`status` reports only presence, selected scopes and protected-storage state.
It never prints client IDs, client secrets, refresh/access tokens or raw provider
errors. `disconnect` removes both local protected records. It does not claim to
revoke consent at Google; revoke it in the Google account when required.

All Google command failures use process exit code `8` and a sanitized category;
provider response bodies and credential values are never included.

The credential store is an abstraction. Version 0.11.0 ships the Windows DPAPI
backend and fails closed on an OS without a protected backend. No plaintext
fallback exists. Normal workspace operations may refresh credentials but never
open a browser. Scheduled/background execution follows the same rule and emits
`google_auth_required` after missing, revoked or interaction-requiring auth.

## Workspace lifecycle

```text
project google workspace init
project google workspace status
project google workspace rebind
project google workspace sync
```

`workspace init` creates the enabled resources:

```text
<Project Name> — Project Workspace
  <Project Name> — Обзор проекта
  <Project Name> — Design Knowledge
  <Project Name> — Design Changes
```

Each Drive resource receives private app properties binding project ID,
lowercase repository identity and one exact role. Creation refuses to duplicate
an already discoverable project workspace; use `rebind` after a lost local
binding when the complete expected resource set exists. The command initializes
the Sheet contract and initial Docs projections. A partial remote failure is not
rolled back destructively: tagged resources remain discoverable for owner
review. If the set is incomplete, the owner must repair or remove those partial
resources before retrying; `rebind` will not guess missing roles.

Version 1 uses the existing validated `github.com` origin identity as the
repository binding key. Support for other Git hosting identities is deferred;
there are no hardcoded project or account names.

`workspace status` is read-only. It validates local integrity, each remote ID,
MIME type, project/repository metadata, parent folder and projection content
hash. Deleted, trashed, moved, foreign or ambiguous resources fail closed.

`workspace rebind` searches only app-visible resources carrying the exact
project and repository metadata. It requires exactly one folder and one resource
for every enabled role, verifies children belong to that folder, and refuses
unknown roles or ambiguity. Rebind never adopts an arbitrary Doc by title.

`workspace sync` runs one bounded foreground cycle: clean Git preflight,
binding/resource verification, projection refresh/drift restoration, Design
Changes import and lifecycle feedback. It is useful for explicit diagnostics;
the same internal cycle is also used by automation.

## Durable resource binding

The integrity-sealed binding lives outside the worktree:

```text
<git-admin-dir>/project-system/google/workspace.json
```

It records schema/CLI version, project/repository identity, folder and resource
IDs/roles/names/MIME types/timestamps, projection source/content hashes and an
integrity hash. It contains no OAuth material. The worktree-specific Git
administrative location preserves the existing Project System durable-binding
model and does not dirty canonical Git.

Symlink/junction path escapes, malformed schema, identity mismatch and integrity
damage are blocking errors. The SHA-256 seal detects accidental/local drift; it
is not a signature or protection from an attacker able to replace all local
records.

## Project Overview projection

Project Overview is a concise human view built only from active canonical
vision, product, scope and current-state narratives plus current decisions. It
is explicitly labelled as a Git projection and includes source commit and last
projection update. Superseded, deprecated, removed, rejected, cancelled and
archived atomic material is excluded.

The renderer does not load history, `.generated`, `inbox/sync`, CLI mechanics or
repository administration. Missing canonical sections are stated as missing;
the CLI does not invent product copy. Exact length depends on available project
knowledge rather than padding to a page target.

## Design Knowledge projection

Design Knowledge combines available canonical design/product narratives with
current screens, flows, related entities, requirements/features/decisions,
recent design-change objects and canonical Figma links. Open/proposed questions
appear in a separate section with an explicit `UNRESOLVED` marker. Non-current
objects do not appear as active truth.

Projection is plain human-readable document text in v1. Rich Google Docs styles,
embedded images and Figma previews are deliberately deferred; content identity
and trust direction take priority.

Canonical documents are embedded beneath projection-owned section titles. The
renderer removes only an embedded document's leading document-level H1 so the
same title is not repeated; inner headings and body text remain in order and
the canonical Markdown file is never changed.

## Projection identity and drift

For each Doc the binding records:

- canonical source commit;
- deterministic source SHA-256;
- normalized projected-content SHA-256;
- projection timestamp.

If source is unchanged but the remote content hash differs, the Doc is recorded
as manually drifted and restored from Git. Its text is never imported, never
becomes a proposal and never changes canonical files. If canonical source or the
renderer output changes, a fresh projection replaces the Doc. Reread hash
verification detects partial writes. There is no Google Docs → Git path and no
sync loop.

## Design Changes Sheet contract

Designer-facing columns are:

1. Date
2. Author
3. Project Area
4. Screen / Flow
5. Change Type
6. What Changed
7. Why
8. Figma URL
9. Logic Changed? (`Yes`, `No`, `Unsure`)

System-managed columns are Change ID, Import Status, Decision Needed?, Canonical
Objects, Applied In, Processing Error and Notes. The Sheet adapter protects the
exact system range `J:P` and applies data validation to Logic Changed?. Columns
`Q` and later are outside this protection. `What Changed` is required; a Figma
URL alone is rejected. A provided Figma URL must be HTTPS on `figma.com`.

The CLI allocates `GDES-YYYYMMDD-<8 lowercase hex>` into the row before import.
Identity follows that cell through reorder and retry; row number is only initial
provenance. A row is reread after identity assignment and immediately before
import. Duplicate Change IDs fail closed. Google Sheets does not offer a
transactional compare-and-swap for arbitrary cells, so a concurrent edit can
leave an assigned ID, but no immutable import is published unless the reread
payload hash matches. The next cycle safely retries or reports conflict.

## Immutable inbound provenance

Every valid first import is exclusively written outside the worktree:

```text
<git-admin-dir>/project-system/google/design-changes/<CHANGE_ID>.json
```

The sealed record retains project/repository and spreadsheet identity, initial
row number, import time, exact normalized designer fields and their SHA-256. It
is created before Sheet status feedback, so a status-write/API crash is safely
retryable. The same unchanged ID/payload is reused. Editing designer fields
after import produces `CONFLICT`; the original record is never overwritten.
Malformed rows keep their assigned ID, receive actionable `ERROR` feedback and
can be retried after correction.

An imported row is an unapproved proposal, not a SYNC REQUEST and not canonical
truth. The CLI never creates an approved pack from designer prose.
Until human-controlled lifecycle evidence exists, `Decision Needed?` is
`PENDING` regardless of the designer's `Logic Changed?` answer; that answer is
not a semantic approval. `NO` is reserved for completed reviewed-no-change,
rejected, abandoned or applied outcomes.

## Human semantic executor boundary and lifecycle feedback

When an owner decides that a proposal should enter the existing lifecycle, an
external semantic executor may prepare a normal SYNC REQUEST v1. A human must
review and approve it. For durable terminal feedback in v1, that approved
request is submitted through the existing GitHub Issue transport (direct local
intake can still plan and verify but has no durable transport completion record).
To bind later feedback, its source is:

```yaml
source:
  type: google_design_change
  ref: GDES-YYYYMMDD-xxxxxxxx
```

All normal request approval, target IDs, plan, semantic edit, verification,
finalization, explicit commit and explicit push rules remain unchanged. The CLI
does not implement or impersonate that semantic executor.

On a later Google cycle, a uniquely linked active pack becomes `IN REVIEW`. A
durably completed pushed pack becomes `APPLIED` with canonical object IDs,
commit and pack references. `reviewed-no-change`, `rejected` and `abandoned`
outcomes are reflected explicitly. Multiple packs claiming one Change ID are a
conflict. Sheet status is feedback only; durable Git lifecycle evidence remains
authoritative. A visual-only change can therefore finish through the existing
human-authorized reviewed-no-change outcome.

## Existing watcher and Windows automation

There is still one bounded cycle and one scheduler:

```text
Task Scheduler
  -> pythonw.exe
  -> existing watcher lock / one bounded pickup
     -> existing GitHub branch when enabled
     -> Google Design Changes pickup when enabled
     -> Google projection check/refresh
  -> exit
```

Google is skipped byte-for-byte for projects without enabled configuration.
The scheduled branch uses non-interactive credential refresh, never browser UI,
and inherits v0.10's explicit background process context and headless Git/gh
policy. It adds no subprocess/shell wrapper and does not modify the Task
Scheduler registration contract.

The Google branch runs only after the Git side is not newly created or blocked.
Dirty canonical files, an active/awaiting-push pack or terminal transaction
block Google mutation. Temporary Google transport/rate-limit failures use the
watcher's retryable path; missing auth, binding/resource integrity, permission
and configuration failures stop fail closed for owner action. Sanitized Google
state/events are written under `.generated/google/`; payloads, URLs containing
private data, provider error bodies and credentials are excluded.

Google API calls and OAuth refresh requests use a 30-second per-request
transport timeout (connect/read inactivity, not a hard whole-cycle deadline).
A provider timeout fails the current short-lived cycle and follows the same
sanitized retryable path; it never opens an interactive fallback.

## Failure and recovery summary

| Condition | Behavior |
| --- | --- |
| Missing/revoked credentials | `google_auth_required`; no browser in background. |
| Permission denied | Fail closed; verify sharing/ownership and reconnect if needed. |
| Deleted/trashed/moved resource | Binding integrity failure; restore or rebind deliberately. |
| Corrupt/stale local binding | Fail closed; no title-based adoption. |
| Ambiguous rebind | Fail closed until duplicates are resolved by a human. |
| Network/5xx/rate limit | Sanitized retryable transport status. |
| Malformed row | Stable ID plus actionable Sheet error; no immutable/canonical import. |
| Duplicate Change ID | Whole import pass fails closed. |
| Row edited during import | No immutable publication; conflict/retry. |
| Row edited after import | Immutable original retained; Sheet conflict. |
| Manual Doc drift | Recorded and restored from canonical Git. |
| Crash after immutable import | Next cycle reuses record and repairs Sheet feedback. |
| Dirty/active/awaiting-push/transaction | Google cycle blocked before remote mutation. |
| Partial workspace creation | Tagged remote resources remain for status/rebind review. |

## Upgrade from 0.10.0

Upgrade the installed package and pin project tooling only through the project's
normal release process. Existing configuration and commands remain valid.
Google is inert until the new config block is explicitly enabled and an owner
runs connect/workspace init. Existing Windows automatic registrations continue
to point at the same internal module; replacing/reinstalling remains an explicit
operator decision if the Python executable changes.

## Owner-operated production live smoke

Do not use production credentials in automated tests. After implementation
review and explicit pilot approval:

1. Enable the three Google flags in the pilot `project.yaml`, validate and
   commit that configuration through normal governance.
2. Create Desktop App OAuth configuration and enable Drive/Docs/Sheets APIs.
3. Run `project google connect --credentials <trusted-path>` interactively.
4. Confirm `project google status` exposes no credential values.
5. Run `project google workspace init`, inspect metadata, folder, both Docs,
   Sheet headers/validation/protected columns and local clean Git status.
6. Run `workspace status`, manually edit a projection, then `workspace sync` and
   confirm deterministic drift restoration with no Git change.
7. Add one valid, one malformed and one Figma-only designer row; confirm stable
   IDs, immutable imports, actionable errors and idempotent retry.
8. Submit one separately human-approved GitHub-transported SYNC REQUEST
   referencing a Change ID, complete its normal lifecycle, then confirm Sheet
   feedback and projection refresh.
9. Revoke authorization and confirm scheduled execution records
   `google_auth_required` without browser, console or popup.
10. Observe at least three scheduled cycles and confirm Git stays clean, only
    one task exists, state/events update, and no window appears.

The owner-operated SportOS gate completed these steps against real Google APIs,
including OAuth/DPAPI, projection/drift restore, proposal identity and conflict
recovery, human-approved reviewed-no-change feedback, multiple scheduled
background cycles, revoke/reconnect recovery and a final clean worktree. These
live observations remain separate from deterministic automated tests and do
not grant the test suite access to production credentials or resources.
