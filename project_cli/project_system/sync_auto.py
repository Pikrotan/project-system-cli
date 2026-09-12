"""OS-neutral registration and one-shot automatic SYNC orchestration."""
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from . import __version__, sync_pull
from .process_runner import background_processes
from .sync_watcher import (
    DEFAULT_INTERVAL_SECONDS, SyncWatcherError, _runtime_path,
    _append_event, _recover_event_log, _validate_interval, _write_state,
    run_watcher, watcher_lock,
)
from .utils import atomic_write_text


REGISTRATION_ID_RE = re.compile(r'^REG-[0-9a-f]{16}$')
TASK_NAME_RE = re.compile(r'^ProjectSystem-Sync-REG-[0-9a-f]{16}$')
REGISTRATION_FIELDS = {
    'schema_version', 'registration_id', 'project_id', 'project_root',
    'repository', 'interval_seconds', 'task_name', 'installed_at',
    'cli_version', 'runner', 'registration_sha256',
}
RUNNER_FIELDS = {'executable', 'arguments'}
BACKGROUND_RUNNER_MODULE = 'project_system.sync_auto_runner'
BACKGROUND_RUNNER_TYPE = 'background'
LEGACY_RUNNER_TYPE = 'legacy_console'


class SyncAutoError(RuntimeError):
    exit_code = 3


def _now():
    return datetime.now(timezone.utc)


def _iso(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise SyncAutoError('automatic runtime clock must return a timezone-aware datetime')
    return value.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def _root_identity(root):
    return os.path.normcase(os.path.normpath(str(Path(root).resolve())))


def _auto_interval(value):
    try:
        return _validate_interval(value)
    except SyncWatcherError as exc:
        raise SyncAutoError(str(exc)) from exc


def registration_id_for(root, project_id, repository):
    identity = '\0'.join(('1', _root_identity(root), project_id, repository.lower()))
    return 'REG-' + sha256(identity.encode('utf-8')).hexdigest()[:16]


def task_name_for(registration_id):
    if not REGISTRATION_ID_RE.fullmatch(registration_id):
        raise SyncAutoError('invalid automatic SYNC registration ID')
    return 'ProjectSystem-Sync-' + registration_id


def pythonw_executable(executable=None):
    """Derive pythonw.exe from the exact Python installation in use."""
    runtime = Path(executable or sys.executable)
    if not runtime.is_absolute():
        runtime = runtime.absolute()
    runtime = runtime.resolve()
    name = runtime.name.casefold()
    if name == 'pythonw.exe':
        return str(runtime)
    if name != 'python.exe':
        raise SyncAutoError(
            'automatic SYNC requires a Windows python.exe/pythonw.exe runtime'
        )
    return str(runtime.with_name('pythonw.exe'))


def _background_arguments(registration_id):
    return [
        '-m', BACKGROUND_RUNNER_MODULE,
        '--registration', registration_id,
    ]


def _legacy_arguments(registration_id):
    return [
        '-m', 'project_system', 'sync', 'auto', 'run',
        '--registration', registration_id,
    ]


def _runner(registration_id, executable=None):
    executable = pythonw_executable(executable)
    return {
        'executable': executable,
        'arguments': _background_arguments(registration_id),
    }


def runner_type(registration):
    runner = registration.get('runner') if isinstance(registration, dict) else None
    if not isinstance(runner, dict):
        return None
    executable = runner.get('executable')
    arguments = runner.get('arguments')
    if not isinstance(executable, str):
        return None
    name = Path(executable).name.casefold()
    registration_id = registration.get('registration_id')
    if name == 'pythonw.exe' and arguments == _background_arguments(registration_id):
        return BACKGROUND_RUNNER_TYPE
    if name == 'python.exe' and arguments == _legacy_arguments(registration_id):
        return LEGACY_RUNNER_TYPE
    return None


def _validate_pythonw(path):
    candidate = Path(path)
    if candidate.name.casefold() != 'pythonw.exe':
        raise SyncAutoError('automatic SYNC background runner must use pythonw.exe')
    if (not candidate.is_file() or candidate.is_symlink()
            or getattr(candidate, 'is_junction', lambda: False)()):
        raise SyncAutoError(
            'matching pythonw.exe is unavailable for the current Python runtime'
        )
    try:
        with candidate.open('rb') as stream:
            signature = stream.read(2)
    except OSError as exc:
        raise SyncAutoError('matching pythonw.exe cannot be inspected safely') from exc
    if signature != b'MZ':
        raise SyncAutoError('matching pythonw.exe is not a Windows executable')
    return str(candidate.resolve())


def _registration_hash(registration):
    unsigned = {key: value for key, value in registration.items()
                if key != 'registration_sha256'}
    raw = json.dumps(
        unsigned, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
    ).encode('utf-8')
    return sha256(raw).hexdigest()


def build_registration(
    root, project_id, repository, interval_seconds, *, installed_at,
    executable=None,
):
    interval_seconds = _auto_interval(interval_seconds)
    root = Path(root).resolve()
    registration_id = registration_id_for(root, project_id, repository)
    registration = {
        'schema_version': 1,
        'registration_id': registration_id,
        'project_id': project_id,
        'project_root': str(root),
        'repository': repository,
        'interval_seconds': interval_seconds,
        'task_name': task_name_for(registration_id),
        'installed_at': installed_at,
        'cli_version': __version__,
        'runner': _runner(registration_id, executable),
    }
    registration['registration_sha256'] = _registration_hash(registration)
    return registration


def _pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise SyncAutoError(f'duplicate registration key: {key}')
        value[key] = item
    return value


def validate_registration(registration):
    if not isinstance(registration, dict) or set(registration) != REGISTRATION_FIELDS:
        raise SyncAutoError('registration has missing or unsupported fields')
    if registration.get('schema_version') != 1:
        raise SyncAutoError('unsupported automatic SYNC registration schema')
    registration_id = registration.get('registration_id')
    if not isinstance(registration_id, str) or not REGISTRATION_ID_RE.fullmatch(registration_id):
        raise SyncAutoError('invalid automatic SYNC registration ID')
    if not isinstance(registration.get('project_id'), str) or not registration['project_id']:
        raise SyncAutoError('invalid registration project_id')
    if not isinstance(registration.get('repository'), str) or not sync_pull.REPOSITORY_RE.fullmatch(registration['repository']):
        raise SyncAutoError('invalid registration repository')
    project_root = registration.get('project_root')
    if not isinstance(project_root, str) or not Path(project_root).is_absolute():
        raise SyncAutoError('registration project_root must be absolute')
    _auto_interval(registration.get('interval_seconds'))
    if (registration.get('task_name') != task_name_for(registration_id)
            or not TASK_NAME_RE.fullmatch(registration['task_name'])):
        raise SyncAutoError('registration task name does not match its identity')
    try:
        installed = datetime.fromisoformat(registration['installed_at'].replace('Z', '+00:00'))
    except (AttributeError, ValueError) as exc:
        raise SyncAutoError('registration installed_at is invalid') from exc
    if installed.tzinfo is None or installed.utcoffset() is None:
        raise SyncAutoError('registration installed_at must include a timezone')
    if not isinstance(registration.get('cli_version'), str):
        raise SyncAutoError('registration cli_version is invalid')
    runner = registration.get('runner')
    if not isinstance(runner, dict) or set(runner) != RUNNER_FIELDS:
        raise SyncAutoError('registration runner is invalid')
    executable = runner.get('executable')
    if not isinstance(executable, str) or not Path(executable).is_absolute():
        raise SyncAutoError('registration runner executable must be absolute')
    if runner_type(registration) is None:
        raise SyncAutoError('registration runner arguments are invalid')
    expected_id = registration_id_for(
        project_root, registration['project_id'], registration['repository'],
    )
    if registration_id != expected_id:
        raise SyncAutoError('registration identity does not match project binding')
    digest = registration.get('registration_sha256')
    if not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest):
        raise SyncAutoError('registration integrity hash is invalid')
    if digest != _registration_hash(registration):
        raise SyncAutoError('registration integrity check failed')
    return registration


def registration_store(store_root=None):
    if store_root is None:
        local = os.environ.get('LOCALAPPDATA')
        if not local:
            raise SyncAutoError('LOCALAPPDATA is unavailable')
        store = Path(local) / 'ProjectSystem/watchers'
    else:
        store = Path(store_root)
    if not store.is_absolute():
        raise SyncAutoError('automatic SYNC registration store must be absolute')
    for candidate in (store.parent, store):
        if candidate.exists() and (
            candidate.is_symlink() or getattr(candidate, 'is_junction', lambda: False)()
        ):
            raise SyncAutoError('refusing symlink/junction registration store')
    return store


def registration_path(registration_id, store_root=None):
    if not isinstance(registration_id, str) or not REGISTRATION_ID_RE.fullmatch(registration_id):
        raise SyncAutoError('invalid automatic SYNC registration ID')
    store = registration_store(store_root)
    path = store / f'{registration_id}.json'
    if path.exists() and (path.is_symlink() or getattr(path, 'is_junction', lambda: False)()):
        raise SyncAutoError('refusing symlink/junction registration file')
    return path


def load_registration(registration_id, store_root=None):
    path = registration_path(registration_id, store_root)
    if not path.is_file():
        raise SyncAutoError(f'automatic SYNC registration not found: {registration_id}')
    try:
        raw = path.read_text(encoding='utf-8')
        registration = json.loads(raw, object_pairs_hook=_pairs)
    except UnicodeDecodeError as exc:
        raise SyncAutoError('registration is not valid UTF-8 JSON') from exc
    except json.JSONDecodeError as exc:
        raise SyncAutoError('registration is malformed JSON') from exc
    return validate_registration(registration), path, raw


def _write_registration(registration, store_root=None):
    validate_registration(registration)
    path = registration_path(registration['registration_id'], store_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    registration_path(registration['registration_id'], store_root)
    try:
        atomic_write_text(
            path, json.dumps(registration, indent=2, sort_keys=True, ensure_ascii=False) + '\n',
        )
    except (OSError, RuntimeError) as exc:
        raise SyncAutoError('cannot atomically write automatic SYNC registration') from exc
    return path


def _project_binding(root, *, require_gh=False, gh_check=None):
    root = Path(root).resolve()
    try:
        repository = sync_pull.github_repository(root)
        project_id, _ = sync_pull.pull_policy(root, repository)
        if require_gh:
            (gh_check or sync_pull._gh_executable)()
    except (
        sync_pull.SyncPullError, OSError, subprocess.TimeoutExpired,
        ValueError, TypeError, KeyError,
    ) as exc:
        raise SyncAutoError(f'automatic SYNC project preflight failed: {exc}') from exc
    return root, project_id, repository


def _scheduler(adapter=None):
    if adapter is not None:
        return adapter
    from .sync_auto_windows import WindowsSchedulerError, WindowsTaskScheduler
    try:
        return WindowsTaskScheduler()
    except WindowsSchedulerError as exc:
        raise SyncAutoError(str(exc)) from exc


def _same_install(left, right):
    fields = REGISTRATION_FIELDS - {'installed_at', 'registration_sha256'}
    return all(left[field] == right[field] for field in fields)


def _inspect_task(scheduler, registration):
    try:
        return scheduler.inspect(registration)
    except Exception as exc:
        raise SyncAutoError('cannot inspect the automatic SYNC Scheduled Task') from exc


def _task_mismatch_lines(inspection):
    mismatches = inspection.get('mismatches') if isinstance(inspection, dict) else None
    if not isinstance(mismatches, list) or not mismatches:
        return ['- task: expected=owned/configured actual=mismatch-without-details']
    lines = []
    for item in mismatches[:32]:
        if not isinstance(item, dict):
            continue
        field = item.get('field')
        if not isinstance(field, str) or not re.fullmatch(r'[a-z0-9_]{1,64}', field):
            field = 'unknown_field'
        values = []
        for key in ('expected', 'actual'):
            value = str(item.get(key, '<missing>'))
            value = value.replace('\r', r'\r').replace('\n', r'\n')
            values.append(value[:200] + ('...' if len(value) > 200 else ''))
        lines.append(f'- {field}: expected={values[0]} actual={values[1]}')
    return lines or ['- task: expected=owned/configured actual=mismatch-without-details']


def _task_verification_error(inspection):
    return SyncAutoError(
        'created Scheduled Task failed ownership/configuration verification:\n'
        + '\n'.join(_task_mismatch_lines(inspection))
    )


def _scheduler_action(label, function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except SyncAutoError:
        raise
    except Exception as exc:
        raise SyncAutoError(f'cannot {label} the automatic SYNC Scheduled Task') from exc


def install_auto(
    root, *, interval=DEFAULT_INTERVAL_SECONDS, replace=False, adapter=None,
    store_root=None, clock=None, executable=None, gh_check=None,
):
    root, project_id, repository = _project_binding(
        root, require_gh=True, gh_check=gh_check,
    )
    scheduler = _scheduler(adapter)
    clock = clock or _now
    registration_id = registration_id_for(root, project_id, repository)
    path = registration_path(registration_id, store_root)
    existing = old_raw = None
    if path.exists():
        existing, _, old_raw = load_registration(registration_id, store_root)
    now_text = _iso(clock())
    candidate = build_registration(
        root, project_id, repository, interval,
        installed_at=existing['installed_at'] if existing else now_text,
        executable=executable,
    )
    if existing and _same_install(existing, candidate):
        desired = existing
    else:
        desired = build_registration(
            root, project_id, repository, interval,
            installed_at=now_text, executable=executable,
        )
    _validate_pythonw(desired['runner']['executable'])
    inspection = _inspect_task(scheduler, existing or desired)
    if existing and _same_install(existing, desired) and inspection['matches']:
        return {'status': 'already_installed', 'registration': existing,
                'task': inspection, 'registration_path': str(path)}
    if inspection['exists'] and not inspection['owned']:
        raise SyncAutoError('refusing automatic SYNC task ownership mismatch')
    if (existing or inspection['exists']) and not replace:
        raise SyncAutoError('automatic SYNC registration differs; rerun with --replace')

    old_inspection = inspection
    _write_registration(desired, store_root)
    try:
        _scheduler_action(
            'create', scheduler.create, desired, replace=inspection['exists'],
        )
        verified = _inspect_task(scheduler, desired)
        if not verified['exists'] or not verified['owned'] or not verified['matches']:
            raise _task_verification_error(verified)
    except Exception as exc:
        try:
            current = _inspect_task(scheduler, desired)
            if old_inspection['exists'] and old_inspection['owned'] and old_inspection.get('raw_xml'):
                _scheduler_action(
                    'restore', scheduler.restore,
                    (existing or desired)['task_name'], old_inspection['raw_xml'],
                )
            elif current['exists'] and current['owned']:
                _scheduler_action('delete', scheduler.delete, desired)
            if old_raw is not None:
                atomic_write_text(path, old_raw)
            elif path.exists():
                path.unlink()
        except Exception as rollback_exc:
            raise SyncAutoError('Scheduled Task installation and rollback both failed') from rollback_exc
        detail = str(exc) if isinstance(exc, SyncAutoError) else 'Scheduled Task installation failed'
        raise SyncAutoError(f'{detail}\nregistration rolled back') from exc
    return {
        'status': 'replaced' if existing or old_inspection['exists'] else 'installed',
        'registration': desired, 'task': verified, 'registration_path': str(path),
    }


def _read_runtime_state(root):
    try:
        path = _runtime_path(root, 'state.json')
    except SyncWatcherError:
        return None, ['runtime state path is unsafe']
    if not path.is_file() or path.is_symlink() or getattr(path, 'is_junction', lambda: False)():
        return None, []
    try:
        value = json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=_pairs)
        if not isinstance(value, dict):
            raise ValueError
        return value, []
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, SyncAutoError, ValueError):
        return None, ['runtime state is unavailable or malformed']


def status_auto(root, *, adapter=None, store_root=None, executable=None):
    root, project_id, repository = _project_binding(root)
    scheduler = _scheduler(adapter)
    registration_id = registration_id_for(root, project_id, repository)
    path = registration_path(registration_id, store_root)
    warnings = []
    if path.exists():
        registration, _, _ = load_registration(registration_id, store_root)
        if (Path(registration['project_root']).resolve() != root
                or registration['project_id'] != project_id
                or registration['repository'].lower() != repository.lower()):
            raise SyncAutoError('registration no longer matches the current project')
        inspection = _inspect_task(scheduler, registration)
    else:
        registration = build_registration(
            root, project_id, repository, DEFAULT_INTERVAL_SECONDS,
            installed_at=_iso(_now()), executable=executable,
        )
        inspection = _inspect_task(scheduler, registration)
        if inspection['exists']:
            warnings.append('deterministic task exists without its registration')
    if registration['cli_version'] != __version__:
        warnings.append(
            f"registration CLI {registration['cli_version']} differs from runtime {__version__}"
        )
    kind = runner_type(registration)
    runner_ready = kind == BACKGROUND_RUNNER_TYPE
    if kind != BACKGROUND_RUNNER_TYPE:
        warnings.append('registered runner is not the no-console background runner; replace required')
    try:
        _validate_pythonw(registration['runner']['executable'])
    except SyncAutoError:
        runner_ready = False
        warnings.append('registered no-console runtime is missing or invalid')
    state, state_warnings = _read_runtime_state(root)
    warnings.extend(state_warnings)
    installed = (
        path.exists() and inspection['exists'] and inspection['owned']
        and inspection['matches'] and runner_ready
    )
    return {
        'schema_version': 1,
        'registration_id': registration_id,
        'project_id': project_id,
        'project_root': str(root),
        'repository': repository,
        'task_name': registration['task_name'],
        'runner_type': kind,
        'runner_executable': registration['runner']['executable'],
        'installed': installed,
        'registration_exists': path.exists(),
        'task_exists': inspection['exists'],
        'task_owned': inspection['owned'],
        'task_matches': inspection['matches'],
        'task_state': inspection.get('task_state'),
        'interval_seconds': registration['interval_seconds'],
        'last_cycle_at': state.get('last_cycle_at') if state else None,
        'last_cycle_status': state.get('last_cycle_status') if state else None,
        'last_issue': state.get('last_issue') if state else None,
        'last_pack': state.get('last_pack') if state else None,
        'last_error_category': state.get('last_error_category') if state else None,
        'watcher_status_advisory': state.get('watcher_status') if state else None,
        'next_scheduled_run': inspection.get('next_scheduled_run'),
        'warnings': warnings,
    }


def remove_auto(root, *, adapter=None, store_root=None, executable=None):
    root, project_id, repository = _project_binding(root)
    scheduler = _scheduler(adapter)
    registration_id = registration_id_for(root, project_id, repository)
    path = registration_path(registration_id, store_root)
    if not path.exists():
        expected = build_registration(
            root, project_id, repository, DEFAULT_INTERVAL_SECONDS,
            installed_at=_iso(_now()), executable=executable,
        )
        inspection = _inspect_task(scheduler, expected)
        if inspection['exists']:
            raise SyncAutoError('task exists without an intact registration; refusing removal')
        return {'status': 'not_installed', 'registration_id': registration_id,
                'task_name': expected['task_name']}

    registration, _, _ = load_registration(registration_id, store_root)
    inspection = _inspect_task(scheduler, registration)
    if inspection['exists']:
        if not inspection['owned']:
            raise SyncAutoError('refusing to remove a task without ownership proof')
        _scheduler_action('delete', scheduler.delete, registration)
        if _inspect_task(scheduler, registration)['exists']:
            raise SyncAutoError('Scheduled Task still exists after removal request')
    try:
        path.unlink()
    except OSError as exc:
        raise SyncAutoError('cannot remove automatic SYNC registration') from exc
    return {'status': 'removed', 'registration_id': registration_id,
            'task_name': registration['task_name']}


@background_processes
def run_auto(registration_id, *, store_root=None, pickup=None, clock=None):
    registration, _, _ = load_registration(registration_id, store_root)
    root, project_id, repository = _project_binding(registration['project_root'])
    if (registration['project_id'] != project_id
            or registration['repository'].lower() != repository.lower()
            or registration_id_for(root, project_id, repository) != registration_id):
        raise SyncAutoError('registration project/repository identity no longer matches')
    try:
        if runner_type(registration) == BACKGROUND_RUNNER_TYPE:
            current_runtime = pythonw_executable(sys.executable)
        else:
            current_runtime = str(Path(sys.executable).resolve())
        if not Path(registration['runner']['executable']).samefile(current_runtime):
            raise SyncAutoError('registration belongs to a different runtime executable')
    except OSError as exc:
        raise SyncAutoError('registered runtime executable is unavailable') from exc

    captured = []
    try:
        result = run_watcher(
            root, interval=registration['interval_seconds'], pickup=pickup,
            clock=clock, sleeper=lambda seconds: None, max_cycles=1,
            mode='scheduled', registration_id=registration_id,
            on_cycle=lambda report, state: captured.append(deepcopy(report)),
        )
    except SyncWatcherError as exc:
        if captured:
            raise SyncAutoError(
                f"scheduled pickup requires human action: {captured[-1]['status']}"
            ) from exc
        raise SyncAutoError(f'scheduled pickup failed: {exc}') from exc
    report = captured[-1]
    return {
        'schema_version': 1, 'registration_id': registration_id,
        'project_id': project_id, 'repository': repository,
        'status': report['status'], 'issue_number': report.get('issue_number'),
        'pack_id': report.get('pack_id'), 'cycles': result['cycles'],
    }


def record_background_failure(
    registration_id, error_category, *, store_root=None, clock=None,
):
    """Best-effort sanitized diagnostics for failures outside the watcher loop."""
    if error_category not in {'scheduled_run_error', 'background_runner_exception'}:
        return False
    clock = clock or _now
    try:
        registration, _, _ = load_registration(registration_id, store_root)
        root = Path(registration['project_root']).resolve()
        timestamp = _iso(clock())
        with watcher_lock(
            root, project_id=registration['project_id'], started_at=timestamp,
        ):
            current, _ = _read_runtime_state(root)
            existing_error = bool(
                current and current.get('watcher_status') == 'error'
                and current.get('last_error_category')
            )
            recovery = _recover_event_log(root)
            if not existing_error:
                state = current if isinstance(current, dict) else {
                    'schema_version': 1,
                    'project_id': registration['project_id'],
                    'repository': registration['repository'],
                    'mode': 'scheduled',
                    'registration_id': registration_id,
                    'started_at': timestamp,
                    'last_cycle_at': None,
                    'last_cycle_status': None,
                    'last_issue': None,
                    'last_pack': None,
                    'consecutive_failures': 0,
                    'current_delay_seconds': registration['interval_seconds'],
                }
                state.update({
                    'watcher_status': 'error',
                    'stopped_at': timestamp,
                    'next_check_at': None,
                    'last_error_category': error_category,
                })
                _write_state(root, state)
            if recovery is not None:
                _append_event(
                    root, 'event_log_recovered', clock=clock,
                    project_id=registration['project_id'],
                    repository=registration['repository'], mode='scheduled',
                    registration_id=registration_id, **recovery,
                )
            _append_event(
                root, 'background_runner_error', clock=clock,
                project_id=registration['project_id'],
                repository=registration['repository'], mode='scheduled',
                registration_id=registration_id,
                error_type=error_category,
            )
        return True
    except Exception:
        return False
