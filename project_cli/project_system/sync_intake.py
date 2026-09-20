"""Bridge v1: bind approved transport data without interpreting its meaning."""
from collections import Counter
from contextlib import contextmanager, nullcontext
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import secrets
import sys
import tempfile

from jsonschema import Draft202012Validator
from referencing import Registry, Resource
import yaml

from .sync_planning import (
    MAX_SYNC_PACK_BYTES, PACK_SUFFIXES, SyncPlanError, _git_head,
    _load_pack_text, _validate_pack_schema, _write_output,
    load_sync_bytes, plan_sync, prepare_sync_plan, sync_format_checker,
)
from .sync_bindings import SyncBindingError, load_sync_bindings
from .utils import distribution_root, load_yaml


LOCAL_PACK_ID = re.compile(r'^SYNC-[0-9]{8}-[0-9a-f]{8}$')
REQUEST_FIELDS = ('source', 'approval', 'change_class', 'changes', 'expected_targets', 'notes')
INTAKE_LOCK_KIND = 'project-system-intake-lock'
INTAKE_LOCK_MAGIC = b'PROJECT-SYSTEM-INTAKE-LOCK-V1\n'
MAX_INTAKE_LOCK_BYTES = 4096


class SyncIntakeError(RuntimeError):
    exit_code = 2


def _read_request(selector, stdin=None):
    if str(selector) == '-':
        stream = stdin if stdin is not None else getattr(sys.stdin, 'buffer', sys.stdin)
        raw = stream.read(MAX_SYNC_PACK_BYTES + 1)
        if isinstance(raw, str):
            raw = raw.encode('utf-8')
    else:
        path = Path(selector)
        if path.suffix.lower() not in PACK_SUFFIXES or not path.is_file():
            raise SyncIntakeError('request must be an existing .yaml, .yml, or .json file')
        with path.open('rb') as stream:
            raw = stream.read(MAX_SYNC_PACK_BYTES + 1)
    request, _ = load_sync_bytes(raw, 'SYNC REQUEST')
    schemas = distribution_root() / 'schemas'
    pack_schema = json.loads((schemas / 'sync-pack.schema.json').read_text(encoding='utf-8'))
    schema = json.loads((schemas / 'sync-request.schema.json').read_text(encoding='utf-8'))
    # Resolve shared change/approval definitions from installed assets, never HTTP.
    registry = Registry().with_resource(pack_schema['$id'], Resource.from_contents(pack_schema))
    validator = Draft202012Validator(schema, registry=registry, format_checker=sync_format_checker())
    errors = sorted(validator.iter_errors(request), key=lambda error: str(list(error.path)))
    if errors:
        raise SyncIntakeError('request schema validation failed: ' + '; '.join(
            f'{".".join(map(str, error.path)) or "<root>"}: {error.message}' for error in errors
        ))
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}', request['request_id']):
        raise SyncIntakeError('invalid request_id')
    return request, sha256(raw).hexdigest()


def _safe_local_path(root, relative):
    """Reject symlinks/junctions in every existing component, even in-root links."""
    path = root
    for part in Path(relative).parts:
        if part in {'.', '..'} or ':' in part or '/' in part or '\\' in part:
            raise SyncIntakeError(f'unsafe intake path: {relative}')
        path = path / part
        if path.is_symlink() or getattr(path, 'is_junction', lambda: False)():
            raise SyncIntakeError(f'refusing symlink/junction intake path: {path}')
        try:
            path.resolve().relative_to(root)
        except ValueError as exc:
            raise SyncIntakeError(f'intake path escapes project root: {path}') from exc
    return path


@contextmanager
def _intake_lock(root):
    """Serialize intake with a crash-released OS lock, not file existence."""
    lock = _safe_local_path(root, '.generated/sync/.intake.lock')
    try:
        lock.parent.mkdir(parents=True, exist_ok=True)
        stream = _open_initialized_intake_lock(root, lock)
    except SyncIntakeError:
        raise
    except OSError as exc:
        raise SyncIntakeError('cannot open intake lock file') from exc

    primitive = None
    try:
        current = lock.stat()
        opened = os.fstat(stream.fileno())
        if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
            raise SyncIntakeError('intake lock changed while being opened')
        primitive = _lock_intake_stream(stream)
        lock_format = _intake_lock_format(stream)
        if lock_format is None:
            raise SyncIntakeError(
                'intake lock artifact is not a recognized Project System lock; '
                'refusing unsafe recovery'
            )
        if lock_format == 'v1':
            _write_intake_lock_metadata(stream)
        yield lock
    finally:
        if primitive is not None:
            try:
                _unlock_intake_stream(stream, primitive)
            finally:
                stream.close()
        else:
            stream.close()


def _intake_lock_metadata():
    return {
        'schema_version': 1,
        'kind': INTAKE_LOCK_KIND,
        'pid': os.getpid(),
        'acquired_at': datetime.now(timezone.utc).isoformat(),
        'lock_id': secrets.token_hex(8),
    }


def _intake_lock_payload():
    metadata = json.dumps(
        _intake_lock_metadata(), sort_keys=True, separators=(',', ':'),
    ).encode('utf-8')
    return INTAKE_LOCK_MAGIC + metadata


def _publish_initialized_intake_lock(lock):
    """Atomically publish a non-empty v1 marker without replacing a winner."""
    fd, temporary = tempfile.mkstemp(
        prefix='.intake.lock.', suffix='.tmp', dir=str(lock.parent),
    )
    temporary = Path(temporary)
    try:
        with os.fdopen(fd, 'wb') as stream:
            payload = _intake_lock_payload()
            if stream.write(payload) != len(payload):
                raise OSError('short intake lock initialization write')
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, lock)
        except FileExistsError:
            return False
        return True
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _open_initialized_intake_lock(root, lock):
    for _ in range(32):
        if not lock.exists():
            _publish_initialized_intake_lock(lock)
        checked = _safe_local_path(root, '.generated/sync/.intake.lock')
        if checked != lock or not lock.is_file():
            raise SyncIntakeError('intake lock path is not a safe regular file')
        before = lock.stat()
        if before.st_size == 0:
            if not _legacy_empty_lock_is_orphaned(lock):
                raise SyncIntakeError(
                    'legacy empty intake lock cannot be proven orphaned; '
                    'confirm no old intake is running before removing it'
                )
            after = lock.stat()
            if ((before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
                    or after.st_size != 0):
                continue
            try:
                lock.unlink()
            except FileNotFoundError:
                pass
            continue
        stream = lock.open('r+b')
        current = lock.stat()
        opened = os.fstat(stream.fileno())
        if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
            stream.close()
            continue
        return stream
    raise SyncIntakeError('cannot stabilize intake lock identity after concurrent changes')


def _intake_lock_format(stream):
    stream.seek(0)
    raw = stream.read(MAX_INTAKE_LOCK_BYTES + 1)
    if len(raw) > MAX_INTAKE_LOCK_BYTES:
        return None
    if raw.startswith(INTAKE_LOCK_MAGIC):
        return 'v1'
    try:
        metadata = json.loads(raw.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if (isinstance(metadata, dict) and metadata.get('schema_version') == 1
            and metadata.get('kind') == INTAKE_LOCK_KIND):
        return 'pre-magic-v1'
    return None


def _write_intake_lock_metadata(stream):
    metadata = json.dumps(
        _intake_lock_metadata(), sort_keys=True, separators=(',', ':'),
    ).encode('utf-8')
    if len(INTAKE_LOCK_MAGIC) + len(metadata) > MAX_INTAKE_LOCK_BYTES:
        raise SyncIntakeError('intake lock metadata exceeds the size limit')
    stream.seek(len(INTAKE_LOCK_MAGIC))
    if stream.write(metadata) != len(metadata):
        raise OSError('short intake lock metadata write')
    stream.truncate()
    stream.flush()
    os.fsync(stream.fileno())


def _lock_intake_stream(stream):
    stream.seek(0)
    if os.name == 'nt':
        import msvcrt
        try:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise SyncIntakeError('intake is already running; OS lock is held') from exc
        return 'windows', msvcrt
    import fcntl
    try:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        raise SyncIntakeError('intake is already running; OS lock is held') from exc
    return 'posix', fcntl


def _unlock_intake_stream(stream, primitive):
    kind, module = primitive
    stream.seek(0)
    if kind == 'windows':
        module.locking(stream.fileno(), module.LK_UNLCK, 1)
    else:
        module.flock(stream.fileno(), module.LOCK_UN)


def _legacy_empty_lock_is_orphaned(path):
    """Prove that a legacy empty Windows lock has no open owner handle.

    The pre-0.11 lock held an ordinary open file without a kernel byte-range
    lock. Windows share-mode compatibility lets us prove that no such handle is
    live. POSIX cannot make the equivalent proof for an unlinked/open inode, so
    it deliberately fails closed rather than using file age or PID guessing.
    """
    if os.name != 'nt':
        return False
    import ctypes
    from ctypes import wintypes

    create_file = ctypes.WinDLL('kernel32', use_last_error=True).CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    close_handle = ctypes.WinDLL('kernel32', use_last_error=True).CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL
    handle = create_file(
        str(path), 0x80000000 | 0x40000000, 0, None, 3, 0x80, None,
    )
    invalid = wintypes.HANDLE(-1).value
    if handle == invalid:
        error = ctypes.get_last_error()
        if error in {32, 33}:  # sharing / lock violation: a legacy owner may be live
            return False
        raise OSError(error, 'cannot prove legacy intake lock ownership')
    try:
        return True
    finally:
        close_handle(handle)


def _bound_pack(request, request_hash, project_id, head, pack_id, timestamp, transport=None):
    pack = {
        'schema_version': 1,
        'pack_id': pack_id,
        'project_id': project_id,
        **{field: request[field] for field in REQUEST_FIELDS},
        'base_commit': head,
        'created_at': timestamp,
        'provenance': {
            'request_id': request['request_id'],
            'request_sha256': request_hash,
            'source': deepcopy(request['source']),
            'intake_at': timestamp,
        },
    }
    if transport is not None:
        pack['provenance']['transport'] = deepcopy(transport)
    return pack


def _pack_bytes(pack):
    return yaml.safe_dump(pack, sort_keys=False, allow_unicode=True).encode('utf-8')


def intake_bindings(root):
    """Read the unified active/completed binding layer without writing output."""
    try:
        return load_sync_bindings(root)
    except SyncBindingError as exc:
        raise SyncIntakeError(str(exc)) from exc


def _find_reusable(root, request, request_hash, project_id, head, transport=None):
    inbox = _safe_local_path(root, 'inbox/sync')
    bindings = intake_bindings(root)
    matches = []
    for binding in bindings:
        path, pack, raw = tuple(binding)
        provenance = pack.get('provenance', {})
        if provenance.get('request_id') != request['request_id']:
            continue
        if provenance['request_sha256'] != request_hash:
            raise SyncIntakeError('request_id already used with different bytes; use a new request_id')
        if pack['project_id'] != project_id:
            raise SyncIntakeError('request_id already bound to a different project in this inbox')
        existing_transport = provenance.get('transport')
        if transport is not None and existing_transport != transport:
            raise SyncIntakeError('request_id already bound to a different or changed transport')
        if binding.state == 'completed':
            # Direct intake keeps its independent new-HEAD semantics, but it may
            # not silently acquire/reuse a completed GitHub transport identity.
            if transport is None:
                raise SyncIntakeError('request_id is already completed through GitHub transport')
            continue
        expected = _bound_pack(
            request, request_hash, project_id, pack['base_commit'],
            pack['pack_id'], pack['created_at'], existing_transport,
        )
        if pack != expected or raw != _pack_bytes(expected):
            raise SyncIntakeError('existing intake pack was modified; refusing reuse')
        if not LOCAL_PACK_ID.fullmatch(pack['pack_id']) or path != inbox / f'{pack["pack_id"]}.yaml':
            raise SyncIntakeError('existing intake pack has inconsistent filename/identity')
        # Intake reports are derived; if present, also enforce the recorded byte hash.
        report_path = _safe_local_path(root, f'.generated/sync/{pack["pack_id"]}/intake.json')
        if report_path.exists():
            previous, _ = load_sync_bytes(report_path.read_bytes(), 'intake report')
            if previous.get('pack_sha256') != sha256(raw).hexdigest():
                raise SyncIntakeError('existing pack differs from its intake report hash')
        if pack['base_commit'] == head:
            matches.append((path, pack, raw))
    if len(matches) > 1:
        raise SyncIntakeError('duplicate request binding at the same HEAD')
    return matches[0] if matches else None, {binding.pack['pack_id'] for binding in bindings}


def _new_pack_id(now):
    return f'SYNC-{now:%Y%m%d}-{secrets.token_hex(4)}'


def _check_report_paths(root, pack_id):
    output = _safe_local_path(root, f'.generated/sync/{pack_id}')
    for name in ('intake.json', 'intake.md', 'plan.json', 'manifest.json', 'context.md', 'pull.json', 'pull.md'):
        target = _safe_local_path(root, f'.generated/sync/{pack_id}/{name}')
        if target.exists() and not target.is_file():
            raise SyncIntakeError(f'generated output is not a regular file: {target}')
    return output


def _write_intake_report(root, report):
    output = _check_report_paths(root, report['pack_id'])
    output.mkdir(parents=True, exist_ok=True)
    _write_output(output, 'intake.json', json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + '\n')
    lines = [
        '# SYNC Intake', '', '> Generated transport/provenance report; not canonical truth.', '',
        f'- Request: `{report["request_id"]}`', f'- Request SHA-256: `{report["request_hash"]}`',
        f'- Pack: `{report["pack_id"]}`', f'- Path: `{report["pack_path"]}`',
        f'- Project: `{report["project_id"]}`', f'- Base: `{report["base_commit"]}`',
        f'- Status: `{report["intake_result"]}`', f'- Reused: `{report["reused"]}`',
        f'- Changes: {report["change_count"]}', f'- Plan: `{report["plan_result"]}`', '',
        '## Approval metadata', '', '```json',
        json.dumps(report['approval'], indent=2, ensure_ascii=False), '```', '',
        '## Expected targets', '',
        *[f'- `{target}`' for target in report['expected_targets']], '',
        '## Warnings / errors', '',
        *[f'- Warning: {warning}' for warning in report['warnings']],
        *[f'- Error: {error}' for error in report['errors']], '',
    ]
    if report.get('transport'):
        lines += ['## Transport provenance', '', '```json',
                  json.dumps(report['transport'], indent=2, ensure_ascii=False), '```', '']
    _write_output(output, 'intake.md', '\n'.join(lines))


def intake_sync(root, selector, *, plan=False, stdin=None, transport=None, _lock_held=False):
    """Validate first, exclusively create/reuse an immutable pack, optionally plan."""
    try:
        return _intake_sync(
            Path(root).resolve(), selector, plan=plan, stdin=stdin,
            transport=transport, lock_held=_lock_held,
        )
    except SyncIntakeError:
        raise
    except (SyncPlanError, SyncBindingError, OSError, ValueError, TypeError) as exc:
        raise SyncIntakeError(str(exc)) from exc


def _intake_sync(root, selector, *, plan, stdin, transport=None, lock_held=False):
    request, request_hash = _read_request(selector, stdin)
    project_id = load_yaml(root / 'project.yaml').get('project', {}).get('id')
    head = _git_head(root)
    with (nullcontext() if lock_held else _intake_lock(root)):
        existing, used_ids = _find_reusable(root, request, request_hash, project_id, head, transport)
        now = datetime.now(timezone.utc)
        reused = existing is not None
        for _ in range(32):
            if reused:
                path, pack, raw = existing
            else:
                pack_id = _new_pack_id(now)
                if not LOCAL_PACK_ID.fullmatch(pack_id):
                    raise SyncIntakeError('generated pack ID is malformed')
                path = _safe_local_path(root, f'inbox/sync/{pack_id}.yaml')
                output = _check_report_paths(root, pack_id)
                if pack_id in used_ids or path.exists() or output.exists():
                    continue
                pack = _bound_pack(request, request_hash, project_id, head, pack_id, now.isoformat(), transport)
                raw = _pack_bytes(pack)
            if len(raw) > MAX_SYNC_PACK_BYTES:
                raise SyncIntakeError('bound pack exceeds the SYNC PACK size limit')
            _check_report_paths(root, pack['pack_id'])
            # Identical validation/target resolution as Phase 1, with zero output writes.
            _, preview, _, _ = prepare_sync_plan(
                root, path, pack, raw.decode('utf-8'), raw, require_clean=plan,
            )
            if _git_head(root) != head or load_yaml(root / 'project.yaml')['project']['id'] != project_id:
                raise SyncIntakeError('local binding changed during intake; retry')
            if not reused:
                _safe_local_path(root, path.relative_to(root))
                path.parent.mkdir(parents=True, exist_ok=True)
                try:
                    with path.open('xb') as stream:
                        stream.write(raw)
                        stream.flush()
                        os.fsync(stream.fileno())
                except FileExistsError:
                    continue
            break
        else:
            raise SyncIntakeError('cannot allocate a collision-free pack ID; no pack overwritten')

        report = {
            'schema_version': 1, 'request_id': request['request_id'],
            'request_hash': request_hash, 'source': request['source'],
            'pack_id': pack['pack_id'], 'project_id': project_id, 'base_commit': head,
            'pack_path': path.relative_to(root).as_posix(), 'pack_sha256': sha256(raw).hexdigest(),
            'intake_at': pack['created_at'], 'approval': request['approval'],
            'change_count': len(request['changes']),
            'change_kinds': dict(sorted(Counter(item['kind'] for item in request['changes']).items())),
            'expected_targets': request['expected_targets'],
            'intake_result': 'reused' if reused else 'created', 'reused': reused,
            'plan_requested': plan, 'plan_result': 'not_requested',
            'allowed_write_count': len(preview['allowed_write_set']),
            'warnings': preview['warnings'], 'errors': [],
        }
        if pack['provenance'].get('transport'):
            report['transport'] = deepcopy(pack['provenance']['transport'])
        _write_intake_report(root, report)
        if plan:
            try:
                _, manifest = plan_sync(root, path)
                report['plan_result'] = 'ready'
                report['allowed_write_count'] = len(manifest['allowed_write_set'])
            except (SyncPlanError, OSError, ValueError) as exc:
                report['plan_result'] = 'failed'
                report['errors'].append(str(exc))
                _write_intake_report(root, report)
                raise SyncIntakeError(f'pack retained at {path}; planning failed: {exc}') from exc
            _write_intake_report(root, report)
        return path, report
