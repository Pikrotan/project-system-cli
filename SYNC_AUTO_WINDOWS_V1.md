# Windows Automatic SYNC Runtime v1

Project System CLI 0.10.0 adds an optional per-project Windows Scheduled Task
which performs one bounded Bridge pickup at each invocation. It does not run the
persistent foreground watcher as a daemon:

```text
Windows Task Scheduler
  -> pythonw.exe -m project_system.sync_auto_runner --registration REG-...
  -> explicit background execution context
  -> git.exe / gh.exe with CREATE_NO_WINDOW
  -> exactly one existing pickup_once cycle
  -> .generated/sync/auto/state.json and events.jsonl
  -> process exit
```

`project sync watch` remains the manual foreground/debug runtime. Queue
selection, GitHub GET transport, durable binding reconciliation, intake locking,
pack creation and deterministic planning remain in the existing pickup stack.
The scheduled layer makes no semantic edits and never verifies, finalizes,
stages, commits, pushes, or mutates GitHub.

## Commands

```text
project sync auto install [--interval 120] [--replace]
project sync auto status [--json]
project sync auto remove
project sync auto run --registration REG-...   # internal scheduled action
```

Intervals are whole seconds from 60 through 3600; the default is 120.
`install` requires a valid project Git origin, enabled GitHub SYNC pull policy,
an available `gh` executable, a stable current Python executable, and Windows
Task Scheduler. An identical installation returns `already_installed` without
creating a duplicate. Changed parameters fail until the user explicitly passes
`--replace`. Replacement is allowed only after the existing task proves exact
ownership. Registration write and task creation are verified; a failed install
attempt restores the former owned task and registration where possible and
otherwise fails closed.

`status` is read-only and has a stable JSON object with `schema_version: 1`,
registration/project/repository/task identity, installation and ownership flags,
interval, scheduler state, last cycle/Issue/pack/error information, optional next
run, and warnings. Project-local watcher state is advisory: a stale
`watcher_status: running` after a hard kill is not treated as process liveness or
as proof that automation is installed.

`remove` deletes only the exactly owned Scheduled Task and this project's
registration. It preserves inbox packs, durable bindings, canonical files,
`.generated` history/state/events, Git state, and GitHub. A missing task permits
controlled registration cleanup. A missing or damaged registration never
authorizes deletion of an existing task. Repeated removal returns
`not_installed`.

## Registration

Registrations are outside the worktree:

```text
%LOCALAPPDATA%\ProjectSystem\watchers\<REGISTRATION_ID>.json
```

The deterministic ID is `REG-` plus 16 lowercase hexadecimal characters derived
from the resolved worktree root, project ID, and lowercased `owner/repository`.
Each worktree/project therefore has an independent registration, task, interval,
lock, and project-local operational state.

Schema v1 contains exactly:

- `schema_version`
- `registration_id`
- `project_id`
- `project_root`
- `repository`
- `interval_seconds`
- `task_name`
- `installed_at`
- `cli_version`
- `runner` (`executable` and fixed argument array)
- `registration_sha256`

The JSON is UTF-8, duplicate-key-rejected, integrity-bound, and atomically
replaced. It contains no token, Issue body, request payload, or other secret.
The internal run command accepts only a validated registration ID; callers
cannot supply a registration path. The path is reconstructed beneath the fixed
LOCALAPPDATA store, and registration/store symlink or junction escapes are
rejected.

## Scheduled Task contract

The deterministic task name is `ProjectSystem-Sync-<REGISTRATION_ID>`. It runs as
the current interactive user with `LeastPrivilege`, never highest privilege,
uses `IgnoreNew`, `StartWhenAvailable`, a daily trigger with the requested
repetition interval, and a bounded execution-time limit. This permits normal
recovery after login/reboot without an always-running Python process.

The action uses `pythonw.exe` from the same Python installation as the invoking
CLI and the fixed argument array:

```text
-m project_system.sync_auto_runner --registration <REGISTRATION_ID>
```

`pythonw.exe` is derived beside the current `python.exe`; PATH is not searched
for another runtime. Install requires that sibling to be a regular, non-link
Windows executable and fails safely rather than falling back to console
`python.exe`. The action has no working-directory dependency, does not execute a
repository script, and cannot include commands from project configuration or
Issue content. Arguments are emitted through Task Scheduler XML and Windows
command-line quoting; subprocesses use argument arrays with `shell=False`.

The dedicated module performs no terminal I/O and delegates exactly once to the
existing `run_auto` business logic. Normal bounded outcomes exit `0`, validated
automatic runtime failures retain exit code `3`, malformed runner arguments exit
`2`, and an unexpected background exception exits `1`. Exceptions are caught so
`pythonw.exe` does not show an unhandled-error dialog. When registration state is
safe enough to locate the project, sanitized failure categories are persisted in
`.generated/sync/auto/state.json` and `events.jsonl`; exception messages, request
content, credentials and tracebacks are not persisted.

The runner also establishes an explicit background-process context for the
whole bounded cycle. All Git and GitHub CLI process boundaries reachable from
automatic pickup use one internal argument-list process runner. On Windows that
runner combines any existing creation flags with `CREATE_NO_WINDOW`; it keeps
`shell=False`, output capture, timeouts, encodings, return codes and existing
sanitized error handling unchanged. The Windows flag is never referenced on
non-Windows platforms. Foreground commands do not enter this context and do not
receive `CREATE_NO_WINDOW`, so manual CLI and watcher console behavior remains
unchanged. No `cmd.exe`, PowerShell, batch file, hidden-window wrapper or global
`subprocess` monkeypatch is used.

Ownership requires the exact deterministic task name, Project System description
marker, registration ID, exact `pythonw.exe`, exact internal module/arguments,
and current-user
principal. Configuration matching additionally checks the interval,
least-privilege/logon settings, `IgnoreNew`, and `StartWhenAvailable`. Similar
names are not ownership proof. Query results use exported XML rather than
localized human output; query failure for an existing task is an error rather
than being mistaken for absence.

Windows may serialize an equivalent task differently from the submitted XML.
Reread verification therefore compares semantic values rather than literal XML:
the exported user name must resolve to the same current-user SID; executable
paths use Windows case/path normalization and existing-file identity; the fixed
argument string is parsed back to the exact expected tokens; known scheduler
enums are case-normalized; an omitted `RunLevel` alone uses Task Scheduler's
documented `LeastPrivilege` default; booleans honor their schema defaults; and
repetition and execution durations are compared as seconds (`PT60S == PT1M`,
`P1D == PT24H`). Explicit `HighestAvailable` remains a hard mismatch, and no
default is inferred for other security-critical enums. The description marker,
registration identity, XML/task shape,
single executable action, absence of a working directory, executable identity,
argument tokens, and principal SID remain ownership-critical and fail closed.

If reread verification fails, install preserves a bounded mismatch list before
rollback and reports entries such as:

```text
created Scheduled Task failed ownership/configuration verification:
- start_when_available: expected=True actual=False
- execution_time_limit_seconds: expected=600 actual=540
registration rolled back
```

Diagnostics contain field-level normalized values only. Untrusted descriptions,
arguments, and working directories are represented by a short SHA-256/length
fingerprint; raw task XML, tokens, Issue/request content, and secrets are never
printed. An owned configuration mismatch is safely removed during rollback. A
genuine ownership mismatch is not treated as permission to delete an unknown or
raced task.

Registrations from the earlier unreleased console-runner candidate remain
loadable only so an owner can inspect, remove, or explicitly migrate them.
They are not reported as installed/compatible, and installation requires the
existing ownership-proven `--replace` flow. A `python.exe` or `project.exe`
action is never equivalent to the no-console action.

## Runtime, locking, and diagnostics

`auto run` reloads and validates the registration and SHA-256, resolves the
stored project root, rechecks project ID and repository against current
configuration/origin, verifies the registered executable is the current runtime,
then invokes the existing watcher engine with `max_cycles=1` and
`mode: scheduled`.

Task Scheduler `IgnoreNew` is backed by the existing project/worktree OS lock, so
two scheduled runs or a scheduled run and foreground watcher cannot both own the
runtime. The existing intake lock still serializes automatic pickup with manual
pull/intake through selection and immutable pack creation.

Normal results (`no_pending`, `processed`, `created`, `blocked_active`) and
retryable operational blocks exit normally after recording state/events. Dirty,
awaiting-push, transaction, and transport conditions remain visible for later
scheduled retries. Malformed, conflict/integrity, configuration, invalid-status,
and runtime exceptions fail with a non-zero exit and a bounded error category in
state/events. Raw reasons, Issue bodies, payloads, credentials, and exception
messages are not persisted in operational records. Event-log tail recovery and
atomic state behavior are inherited unchanged from the 0.9 watcher runtime.

## Authority and limitations

Installation authorizes only periodic transport pickup under the project's
existing GitHub allowlist and owner policy. It does not grant semantic approval,
canonical write authority, verification/finalization authority, Git mutation, or
GitHub mutation. Updating the package is never automatic. `status` warns when the
registration records another CLI version or a missing runtime executable;
reinstall/replace is an explicit operator action when the executable path changes.

The adapter targets Windows Task Scheduler and `github.com`; it assumes the user
is logged on so the existing `gh` credential context is available. Unit,
integration, and packaged smoke tests use a fake scheduler and do not create real
user tasks. A live install/status/wait/remove check is intentionally a separate,
explicit owner-operated release step.
