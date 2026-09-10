# Durable Terminal SYNC Lifecycle v1

CLI 0.7.0 adds a clone/worktree-local terminal layer for GitHub Issue transport. It is a prerequisite for future automatic pickup, not a watcher or semantic executor.

## Authority and storage

Active transport input remains `inbox/sync/<PACK_ID>.yaml`. Completed identity is stored beneath the directory returned by `git rev-parse --absolute-git-dir`:

```text
project-system/sync/completed/<PACK_ID>/pack.yaml
project-system/sync/completed/<PACK_ID>/binding.json
project-system/sync/transactions/<PACK_ID>.json
```

The exact archived pack bytes plus the validated, integrity-sealed binding are authoritative after completion. `.generated/**` remains disposable and may be regenerated where the active lifecycle permits. Deleting it after completion does not erase processed Issue/request identity, although Phase 2/3 reports can no longer be replayed from that deleted evidence.

The store is local to one Git administrative worktree. It is not pushed or copied by a normal clone and therefore does not provide global exactly-once delivery. Hashes detect accidental or uncoordinated modification; they are not signatures and do not resist a malicious user with full local filesystem control.

## GitHub canonical change lifecycle

```text
pull → intake → plan → human semantic edit → verify
→ finalize --commit → committed / awaiting_push
→ finalize --push → pushed → completed archive → inbox cleanup
```

`--commit` alone never terminalizes a GitHub transport pack. Cloud canonical context is considered updated only after the configured upstream proves the exact verified commit was pushed. `--commit --push` performs both explicit operations and terminalizes only after push success. A failed push preserves the active pack and committed association for retry.

Dry-run finalization remains `prepared` and is never terminal approval. Direct-intake packs retain the 0.6.0 finalization behavior and are not silently mixed with GitHub transport semantics.

## Explicit non-commit outcomes

```text
project sync finalize <PACK> --complete --outcome reviewed-no-change --reason "..."
project sync finalize <PACK> --complete --outcome rejected --reason "..."
project sync finalize <PACK> --complete --outcome abandoned --reason "..."
```

`--complete` is incompatible with `--commit`, `--push`, and `--message`. A nonempty reason is mandatory. `reviewed-no-change` requires a passed verification with no canonical changes. `rejected` and `abandoned` require an intact pack, unchanged GitHub transport snapshot, current base, safe Git preflight and a clean canonical working state; they do not manufacture a successful semantic verification. The explicit invocation is a technical human-authorization boundary, not cryptographic proof of identity.

Malformed requests without a pack, changed transport, failed verification, partial Git state and integrity conflicts never complete automatically. Canonical edits must be reverted or resolved by the human before rejection/abandonment.

## Crash and collision safety

Completion writes a sealed transaction and a temporary archive within the Git administrative store, verifies schema/hashes/cross-field identity, then exclusively publishes the completed directory. Only a matching published record permits removal of the active inbox pack. A crash before publication leaves the active pack authoritative. An identical active/archive pair after publication is recoverable; any byte or binding mismatch is blocking and nothing is removed.

Paths use validated collision-resistant `SYNC-YYYYMMDD-xxxxxxxx` IDs only. Issue content and project configuration cannot supply archive paths or commands. Symlink, junction, reparse traversal, duplicate active/completed IDs, binding tampering and transaction tampering fail closed. Archive files are never staged or committed.

Committed terminal proof records the verification fingerprint, exact canonical paths, direct base-parent relationship, commit SHA, upstream remote/ref and push result. Later local checks require the terminal commit to remain an ancestor of current HEAD.

## Pull after completion

The unified binding layer reads active inbox packs and completed Git-admin records. An unchanged completed Issue returns `completed`/already processed and never creates or replans a pack. Changes to body, title, author, URL, GitHub update timestamp or request bytes are transport drift. Reusing its request ID in another Issue is a collision. A new approved intent requires a new Issue and request ID.

CLI 0.8.0 bounded automatic pickup refines this for terminal records: GitHub lifecycle-only `state`, `state_reason`, `updated_at`, and `closed_at` changes are provenance-only and do not block the unattended queue. Request/Issue identity, content, hashes, and archive/binding integrity remain blocking. Existing 0.7 records require no migration. Manual 0.7 pull UX retains its full-snapshot comparison.

## Legacy migration

```text
project sync migrate-bindings
project sync migrate-bindings --apply
```

The default is read-only audit. `--apply` is explicit and idempotent. Only GitHub transport records with valid pack/plan/verification/finalization integrity and locally proven pushed commit/upstream state are archived. Commit-only records remain `awaiting_push`; prepared/no-op records require explicit completion; active/unverified and direct-intake packs remain in the inbox; ambiguous or tampered records are conflicts.

## Deliberate exclusions

CLI 0.7.0 does not implement `sync watch`, autostart, Task Scheduler, polling, background processes, notifications, semantic edits, automatic canonical writes, implicit commit/push, force operations, or GitHub Issue mutations.
