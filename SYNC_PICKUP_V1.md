# Automatic Pickup Core / Bounded Cycle v1

CLI 0.8.0 Stage 1 adds one OS-neutral automatic pickup cycle:

```text
project sync watch --once
```

The command is bounded. It inspects one project queue, creates and plans at most one new SYNC pack, prints a structured result, and exits. It does not run a persistent watcher loop.

## Existing policy and authority

No new project configuration is required. Pickup uses the existing `external_systems.github.enabled`, `mode: sync`, `sync_pull.allowed_authors`, and optional `sync_pull.expected_repository` policy. Repository identity comes from the current Git `origin`; GitHub access and authentication use the existing local `gh` GET-only transport.

Request extraction, schema/target validation, author allowlisting, transport provenance, drift detection, immutable intake, planning, and active/completed identity are delegated to the Bridge v1/v2 and Durable Terminal implementations. Pickup does not introduce a second parser, validator, binding store, or acknowledgement source.

## One-cycle algorithm

1. Resolve the current project root, Git repository/origin, and GitHub sync policy.
2. Validate terminal transactions and the unified active/completed binding layer.
3. Refuse new intake while an active pack, awaiting-push commit, unfinished transaction, integrity conflict, or non-disposable working-tree change exists.
4. Reconcile completed GitHub Issue request identity. Unchanged completed requests are processed and skipped; request/identity drift is blocking. Mutable GitHub lifecycle metadata is provenance-only.
5. Inspect specially marked open Issues, ignore ordinary/unauthorized/closed Issues, and order the authorized queue by ascending Issue number.
6. Block on a malformed authorized oldest Issue or duplicate request identity; never silently bypass that head request.
7. Select at most one Issue and invoke the existing deterministic pull/intake/plan path.
8. Exit without verification, semantic application, finalization, Git or GitHub mutation.

The existing intake lock spans queue selection through intake/plan for cooperative automatic, manual pull, and direct intake writers. It is a non-blocking OS file lock (`msvcrt` on Windows, `flock` on POSIX), so process exit or crash releases ownership even though the diagnostic file persists. A new lock path is published atomically from a fully initialized same-directory temporary file through create-if-absent hard-link publication; its immutable magic prefix distinguishes every v0.11-created artifact from a legacy empty marker before any process can observe the final path. Owner metadata is rewritten only after that prefix, so an interrupted metadata update remains recoverable. PID and acquisition metadata are diagnostic only; liveness never depends on PID reuse or file age. A live lock cannot be stolen. Ambiguous empty POSIX legacy artifacts remain fail-closed; an empty Windows legacy artifact is replaced only after exclusive-handle proof that no legacy owner remains. GitHub itself cannot be locked; selected transport is therefore re-fetched and compared before and after intake by the existing pull contract.

## Stable results

Successful/non-action results use exit code `0`:

- `created`: one new immutable pack and its plan were created;
- `processed`: a concurrent/reused selection resolved without another pack;
- `no_pending`: no authorized marked open request remains pending.

Blocked results use exit code `3`:

- `blocked_active`;
- `blocked_awaiting_push`;
- `blocked_dirty`;
- `blocked_transaction`;
- `blocked_malformed`;
- `blocked_conflict`;
- `blocked_config`;
- `blocked_transport`.

CLI output always includes `Repository` and `Status`; blocked/no-pending results include `Reason`, while a selected request includes `Issue`, `Pack`, and `Plan`.

## Write and trust boundary

The only permitted writes are the selected immutable `inbox/sync/<PACK_ID>.yaml`, derived `.generated/sync/<PACK_ID>/` intake/plan/pull reports, and the persistent noncanonical intake lock artifact whose OS ownership is short-lived. There are no canonical knowledge/docs edits, automatic verification/finalization, staging, commit, push, Issue close/comment/label operations, arbitrary commands, or Issue-derived filesystem paths.

Author allowlisting proves only configured transport eligibility, not semantic human approval or cryptographic identity. Existing approval validation remains structural.

For completed bindings, hard drift invariants are repository, Issue number/URL, author identity, marked title and its hash, body and its hash, extracted request hash, request ID, and archived pack/binding integrity. `state`, `state_reason`, `updated_at`, `closed_at`, and other GitHub lifecycle fields that are not part of the SYNC REQUEST remain provenance-only: closing or reopening an already completed Issue does not block pickup or create a new pack. Existing 0.7 completed bindings require no migration.

## Deliberate exclusions

Stage 1 does not implement `project sync watch` without `--once`, persistent polling, watcher state files, Task Scheduler, install/status/remove commands, autostart, services/daemons, background processes, notifications, or desktop UI. Those require separate lifecycle and operating-system integration stages.
