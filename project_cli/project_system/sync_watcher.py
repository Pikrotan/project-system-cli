"""Persistent foreground runtime for the bounded automatic SYNC pickup cycle.

The watcher owns only disposable runtime artifacts.  Queue selection and pack
creation remain entirely in :mod:`sync_pickup` and its shared intake lock.
"""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import time

from . import sync_pull
from .sync_intake import SyncIntakeError, _safe_local_path
from .sync_pickup import pickup_once
from .utils import atomic_write_text


DEFAULT_INTERVAL_SECONDS = 120
MIN_INTERVAL_SECONDS = 60
MAX_INTERVAL_SECONDS = 3600
MAX_BACKOFF_SECONDS = 1800

NORMAL_STATUSES = {'created', 'processed', 'no_pending', 'blocked_active'}
RETRYABLE_STATUSES = {
    'blocked_awaiting_push',
    'blocked_dirty',
    'blocked_transaction',
    'blocked_transport',
}
FATAL_STATUSES = {'blocked_config', 'blocked_conflict', 'blocked_malformed'}


class SyncWatcherError(RuntimeError):
    exit_code = 3


class SyncWatcherAlreadyRunningError(SyncWatcherError):
    pass


def _safe_watcher_path(root, relative):
    try:
        return _safe_local_path(root, relative)
    except SyncIntakeError as exc:
        raise SyncWatcherError(f'unsafe watcher runtime path: {exc}') from exc


def _utc_now():
    return datetime.now(timezone.utc)


def _iso(value):
    if not isinstance(value, datetime):
        raise SyncWatcherError('watcher clock must return datetime values')
    if value.tzinfo is None or value.utcoffset() is None:
        raise SyncWatcherError('watcher clock must return timezone-aware values')
    return value.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def _validate_interval(interval):
    if type(interval) is not int or not MIN_INTERVAL_SECONDS <= interval <= MAX_INTERVAL_SECONDS:
        raise SyncWatcherError(
            f'watch interval must be {MIN_INTERVAL_SECONDS}..{MAX_INTERVAL_SECONDS} seconds'
        )
    return interval


def _lock_file(stream):
    stream.seek(0)
    if os.name == 'nt':
        import msvcrt
        try:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise SyncWatcherAlreadyRunningError(
                'another persistent watcher is already running for this project/worktree'
            ) from exc
        return ('windows', msvcrt)
    import fcntl
    try:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        raise SyncWatcherAlreadyRunningError(
            'another persistent watcher is already running for this project/worktree'
        ) from exc
    return ('posix', fcntl)


def _unlock_file(stream, primitive):
    kind, module = primitive
    stream.seek(0)
    if kind == 'windows':
        module.locking(stream.fileno(), module.LK_UNLCK, 1)
    else:
        module.flock(stream.fileno(), module.LOCK_UN)


@contextmanager
def watcher_lock(root, *, project_id=None, started_at=None):
    """Hold a crash-released OS lock for one project/worktree watcher.

    The lock file intentionally persists after release.  Its OS lock, not a
    stale PID marker, is authoritative; this makes crash recovery safe without
    ever deleting another live process's lock file.
    """
    root = Path(root).resolve()
    path = _safe_watcher_path(root, '.generated/sync/auto/watcher.lock')
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Ensure one lockable byte exists before attempting the platform lock.
        try:
            stream = path.open('x+b')
        except FileExistsError:
            stream = path.open('r+b')
    except OSError as exc:
        raise SyncWatcherError('cannot open watcher lock file') from exc
    primitive = None
    try:
        if path.stat().st_size == 0:
            stream.write(b'\0')
            stream.flush()
            os.fsync(stream.fileno())
        primitive = _lock_file(stream)
        metadata = json.dumps({
            'schema_version': 1,
            'project_id': project_id,
            'pid': os.getpid(),
            'started_at': started_at,
        }, sort_keys=True, separators=(',', ':')).encode('utf-8')
        stream.seek(0)
        stream.write(metadata)
        stream.truncate()
        stream.flush()
        os.fsync(stream.fileno())
        yield path
    finally:
        if primitive is not None:
            try:
                _unlock_file(stream, primitive)
            finally:
                stream.close()
        else:
            stream.close()


def _runtime_path(root, relative):
    return _safe_watcher_path(root, f'.generated/sync/auto/{relative}')


def _write_state(root, state):
    path = _runtime_path(root, 'state.json')
    try:
        atomic_write_text(path, json.dumps(state, indent=2, sort_keys=True) + '\n')
    except (OSError, RuntimeError) as exc:
        raise SyncWatcherError('cannot atomically write watcher state') from exc
    return path


def _json_object_record(raw, line_number):
    try:
        value = json.loads(raw.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SyncWatcherError(
            f'watcher event log contains malformed complete record at line {line_number}'
        ) from exc
    if not isinstance(value, dict):
        raise SyncWatcherError(
            f'watcher event log record at line {line_number} is not a JSON object'
        )
    return value


def _recover_event_log(root):
    """Repair only a provably non-newline-terminated interrupted tail.

    All newline-terminated records are treated as complete and must validate.
    A valid final object without LF is preserved and LF-normalized.  An invalid
    non-terminated tail is discarded after its preceding complete records have
    validated.  No interior or terminated corruption is silently repaired.
    """
    path = _runtime_path(root, 'events.jsonl')
    if not path.exists():
        return None
    if not path.is_file():
        raise SyncWatcherError('watcher event log is not a regular file')
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise SyncWatcherError('cannot read watcher event log for recovery') from exc
    if not raw:
        return None

    parts = raw.split(b'\n')
    terminated = raw.endswith(b'\n')
    complete = parts[:-1]
    for index, record in enumerate(complete, start=1):
        if record.endswith(b'\r'):
            record = record[:-1]
        if record:
            _json_object_record(record, index)

    tail = b'' if terminated else parts[-1]
    if terminated:
        return None
    try:
        _json_object_record(tail.removesuffix(b'\r'), len(complete) + 1)
    except SyncWatcherError:
        boundary = len(raw) - len(tail)
        try:
            with path.open('r+b', buffering=0) as stream:
                stream.truncate(boundary)
                os.fsync(stream.fileno())
        except OSError as exc:
            raise SyncWatcherError('cannot remove interrupted watcher event tail') from exc
        return {
            'recovery_action': 'discarded_incomplete_tail',
            'removed_tail_bytes': len(tail),
            'removed_tail_sha256': sha256(tail).hexdigest(),
        }

    try:
        with path.open('ab', buffering=0) as stream:
            if stream.write(b'\n') != 1:
                raise OSError('short event-log newline write')
            os.fsync(stream.fileno())
    except OSError as exc:
        raise SyncWatcherError('cannot normalize watcher event log boundary') from exc
    return {
        'recovery_action': 'normalized_final_newline',
        'removed_tail_bytes': 0,
    }


def _append_event(root, event, *, clock, project_id, repository, **fields):
    path = _runtime_path(root, 'events.jsonl')
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        'schema_version': 1,
        'timestamp': _iso(clock()),
        'event': event,
        'project_id': project_id,
        'repository': repository,
    }
    # Callers pass only bounded identifiers/statuses.  Reasons and Issue bodies
    # are deliberately excluded from the operational log.
    record.update({key: value for key, value in fields.items() if value is not None})
    payload = (json.dumps(record, sort_keys=True, separators=(',', ':')) + '\n').encode('utf-8')
    try:
        with path.open('ab', buffering=0) as stream:
            if stream.write(payload) != len(payload):
                raise OSError('short watcher event write')
            os.fsync(stream.fileno())
    except OSError as exc:
        raise SyncWatcherError('cannot append watcher event log') from exc
    return record


def _preflight(root):
    try:
        repository = sync_pull.github_repository(root)
        project_id, _ = sync_pull.pull_policy(root, repository)
    except (sync_pull.SyncPullError, OSError, ValueError, TypeError, KeyError) as exc:
        raise SyncWatcherError(f'watcher startup configuration failed: {exc}') from exc
    return project_id, repository


def _next_delay(interval, failures):
    delay = interval
    for _ in range(min(max(failures - 1, 0), 31)):
        if delay >= MAX_BACKOFF_SECONDS:
            break
        delay = min(delay * 2, MAX_BACKOFF_SECONDS)
    return min(delay, MAX_BACKOFF_SECONDS)


def run_watcher(
    root,
    interval=DEFAULT_INTERVAL_SECONDS,
    *,
    pickup=None,
    clock=None,
    sleeper=None,
    on_cycle=None,
    max_cycles=None,
):
    """Run the foreground watcher until interruption or a fatal cycle result.

    ``max_cycles`` and injected boundaries exist for deterministic tests and
    packaged smoke checks; the public persistent CLI does not set a limit.
    """
    root = Path(root).resolve()
    interval = _validate_interval(interval)
    pickup = pickup or pickup_once
    clock = clock or _utc_now
    sleeper = sleeper or time.sleep
    if max_cycles is not None and (type(max_cycles) is not int or max_cycles < 1):
        raise SyncWatcherError('max_cycles must be a positive integer')

    project_id, repository = _preflight(root)
    started_at = _iso(clock())
    state = {
        'schema_version': 1,
        'project_id': project_id,
        'repository': repository,
        'watcher_status': 'starting',
        'started_at': started_at,
        'stopped_at': None,
        'last_cycle_at': None,
        'last_cycle_status': None,
        'last_issue': None,
        'last_pack': None,
        'consecutive_failures': 0,
        'current_delay_seconds': interval,
        'next_check_at': None,
    }
    interrupted = False
    cycles = 0

    with watcher_lock(root, project_id=project_id, started_at=started_at):
        recovery = _recover_event_log(root)
        try:
            state['watcher_status'] = 'running'
            _write_state(root, state)
            _append_event(
                root, 'watcher_started', clock=clock, project_id=project_id,
                repository=repository, interval_seconds=interval, pid=os.getpid(),
            )
            if recovery is not None:
                _append_event(
                    root, 'event_log_recovered', clock=clock, project_id=project_id,
                    repository=repository, **recovery,
                )
            while True:
                _append_event(
                    root, 'cycle_started', clock=clock, project_id=project_id,
                    repository=repository, cycle=cycles + 1,
                )
                try:
                    report = pickup(root)
                except KeyboardInterrupt:
                    interrupted = True
                    break
                except Exception as exc:
                    state['watcher_status'] = 'error'
                    state['next_check_at'] = None
                    _write_state(root, state)
                    _append_event(
                        root, 'watcher_error', clock=clock, project_id=project_id,
                        repository=repository, error_type=type(exc).__name__,
                    )
                    raise SyncWatcherError(
                        f'watcher pickup cycle raised {type(exc).__name__}'
                    ) from exc

                cycles += 1
                status = report.get('status') if isinstance(report, dict) else None
                if status not in NORMAL_STATUSES | RETRYABLE_STATUSES | FATAL_STATUSES:
                    state['watcher_status'] = 'error'
                    state['next_check_at'] = None
                    _write_state(root, state)
                    _append_event(
                        root, 'watcher_error', clock=clock, project_id=project_id,
                        repository=repository, error_type='invalid_pickup_status',
                    )
                    raise SyncWatcherError(
                        f'watcher pickup returned unknown status: {status!r}'
                    )

                now = clock()
                state['last_cycle_at'] = _iso(now)
                state['last_cycle_status'] = status
                if report.get('issue_number') is not None:
                    state['last_issue'] = report['issue_number']
                if report.get('pack_id'):
                    state['last_pack'] = report['pack_id']

                if status in RETRYABLE_STATUSES:
                    state['consecutive_failures'] += 1
                    delay = _next_delay(interval, state['consecutive_failures'])
                else:
                    state['consecutive_failures'] = 0
                    delay = interval
                state['current_delay_seconds'] = delay

                fatal = status in FATAL_STATUSES
                state['watcher_status'] = 'error' if fatal else 'running'
                state['next_check_at'] = None if fatal else _iso(now + timedelta(seconds=delay))
                _write_state(root, state)
                event_fields = {
                    'cycle': cycles,
                    'status': status,
                    'issue_number': report.get('issue_number'),
                    'pack_id': report.get('pack_id'),
                    'consecutive_failures': state['consecutive_failures'],
                    'next_delay_seconds': None if fatal else delay,
                }
                _append_event(
                    root, 'cycle_finished', clock=clock, project_id=project_id,
                    repository=repository, **event_fields,
                )
                if status == 'created':
                    _append_event(
                        root, 'request_created', clock=clock, project_id=project_id,
                        repository=repository, issue_number=report.get('issue_number'),
                        pack_id=report.get('pack_id'),
                    )
                elif status.startswith('blocked_'):
                    _append_event(
                        root, 'blocked', clock=clock, project_id=project_id,
                        repository=repository, status=status,
                        issue_number=report.get('issue_number'),
                        pack_id=report.get('pack_id'),
                    )
                if status in RETRYABLE_STATUSES:
                    _append_event(
                        root, 'backoff_changed', clock=clock, project_id=project_id,
                        repository=repository, status=status,
                        consecutive_failures=state['consecutive_failures'],
                        delay_seconds=delay,
                    )
                if on_cycle is not None:
                    on_cycle(report, dict(state))
                if fatal:
                    _append_event(
                        root, 'watcher_error', clock=clock, project_id=project_id,
                        repository=repository, status=status,
                    )
                    raise SyncWatcherError(
                        f'watcher stopped on non-retryable status {status}'
                    )
                if max_cycles is not None and cycles >= max_cycles:
                    break
                try:
                    sleeper(delay)
                except KeyboardInterrupt:
                    interrupted = True
                    break
        finally:
            if state['watcher_status'] != 'error':
                state['watcher_status'] = 'stopped'
            state['stopped_at'] = _iso(clock())
            state['next_check_at'] = None
            _write_state(root, state)
            _append_event(
                root, 'watcher_stopped', clock=clock, project_id=project_id,
                repository=repository,
                stop_reason='keyboard_interrupt' if interrupted else state['watcher_status'],
                cycles=cycles,
            )

    return {
        'schema_version': 1,
        'project_id': project_id,
        'repository': repository,
        'status': 'stopped',
        'interrupted': interrupted,
        'cycles': cycles,
        'last_cycle_status': state['last_cycle_status'],
    }
