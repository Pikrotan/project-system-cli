# Persistent Foreground Watcher Runtime v1

CLI 0.9.0 Stage 2A adds a persistent foreground wrapper around the bounded 0.8 pickup core:

```text
project sync watch
project sync watch --interval 120
```

`project sync watch --once` remains the original single bounded cycle. Persistent mode defaults to 120 seconds and accepts whole-second intervals from 60 through 3600. The foreground command remains attached to the terminal and is not itself a scheduler, service, or daemon. CLI 0.10 reuses this runtime for one-cycle Windows Scheduled Task invocations as documented separately in `SYNC_AUTO_WINDOWS_V1.md`.

## Runtime lifecycle

Persistent startup resolves a valid Project System root, discovers the GitHub repository from the sole `origin` fetch URL, and validates the existing GitHub sync/author policy. It then obtains the watcher lock, records startup state/event, and repeatedly invokes the existing `pickup_once` core. Queue parsing, selection, reconciliation, intake and planning are not reimplemented by the watcher.

Each cycle creates at most one pack. A `created` result therefore becomes `blocked_active` on later cycles until the existing explicit verify/finalize lifecycle resolves it. The runtime never bypasses an active pack, malformed authorized head, duplicate request identity, integrity conflict, dirty baseline, unfinished terminal transaction, or awaiting-push state.

Ctrl+C during pickup or waiting records a stopped state and event, releases the watcher lock, and exits with code 130. A fatal startup or cycle condition exits with code 3. State/event write failures also unwind the OS lock and fail rather than continuing without observable runtime state.

## Watcher lock

`.generated/sync/auto/watcher.lock` is a separate runtime-level lock. The process holds a non-blocking OS file lock for its full lifetime (`msvcrt` on Windows and `flock` on POSIX). The file itself may remain after shutdown or a crash; an unlocked file is safely reusable, so no PID-only stale-lock guessing or deletion of another live process's lock is needed. A second foreground or scheduled runtime for the same project/worktree fails clearly. The existing `.generated/sync/.intake.lock` remains responsible for races with manual pull/intake writers.

## Disposable state

`.generated/sync/auto/state.json` is atomically replaced after startup, every cycle, and shutdown. Schema version 1 contains:

- `schema_version`, `project_id`, and origin-derived `repository`;
- `mode` (`foreground` or `scheduled`) and optional scheduled `registration_id`;
- `watcher_status`: `starting`, `running`, `stopped`, or `error`;
- `started_at`, `stopped_at`, `last_cycle_at`, and `next_check_at` UTC timestamps;
- `last_cycle_status`, `last_issue`, and `last_pack`;
- `consecutive_failures`, `current_delay_seconds`, and bounded `last_error_category`.

Deletion of this state loses only operational observability. Processed request identity remains in the durable 0.7+ active/completed bindings.

## Operational events

`.generated/sync/auto/events.jsonl` is append-only during normal operation. Every line is one UTF-8 JSON object with `schema_version`, UTC `timestamp`, `event`, `project_id`, `repository`, runtime `mode`, optional scheduled `registration_id`, and bounded event-specific identifiers/statuses. Event names are `watcher_started`, `cycle_started`, `cycle_finished`, `request_created`, `blocked`, `backoff_changed`, `watcher_error`, `watcher_stopped`, and `event_log_recovered`.

The log may contain Issue numbers, pack IDs, statuses, delays, counters and process ID. It does not contain Issue bodies, request payloads, GitHub tokens, subprocess stderr, or free-form failure reasons.

Before the first append of each run, and only after acquiring the exclusive watcher lock, the runtime validates every newline-terminated record. A valid final JSON object without a newline is preserved and receives the missing newline boundary. If all complete records are valid but the non-terminated tail is invalid, that tail alone is treated as an interrupted append and removed; a recovery event records only its byte count and SHA-256, never the damaged bytes. Malformed interior records and malformed newline-terminated final records fail closed without truncation. New events are serialized as one binary UTF-8 JSON object plus LF, written in one append call, and followed by `fsync`; this supports deterministic next-start recovery but does not claim absolute power-loss atomicity.

The event log and state remain disposable operational evidence. Durable active/completed request identity does not depend on either file.

## Status and backoff policy

Normal results reset failures and delay to the configured interval:

- `created`, `processed`, `no_pending`, `blocked_active`.

Retryable operational results keep the watcher alive and use exponential delay, capped at 1800 seconds:

- `blocked_transport`;
- `blocked_dirty`;
- `blocked_awaiting_push`;
- `blocked_transaction`.

For the default interval the retry delays are 120, 240, 480, 960, then 1800 seconds. A later normal cycle resets the delay to 120. Production waiting never uses less than the configured minimum of 60 seconds.

Fatal/human-required results stop fail-closed without skipping the request:

- `blocked_config`;
- `blocked_malformed`;
- `blocked_conflict`.

## Authority and security boundary

The watcher may read local Git/GitHub state, ask the 0.8 pickup core to create one immutable `inbox/sync/<PACK_ID>.yaml`, create derived intake/plan/pull reports, and write its disposable lock/state/events. It cannot edit `knowledge/` or `docs/`, perform semantic work, verify/finalize a pack, stage, commit, push, mutate an Issue, execute configured commands, or obtain new authority.

GitHub continues through the existing argument-list `gh` GET-only boundary with no `shell=True`. Output paths are project-bound constants checked against traversal and symlink/junction components; Issue data never forms a path. State replacement is same-directory and atomic. Event fields are explicitly bounded and sanitized.
