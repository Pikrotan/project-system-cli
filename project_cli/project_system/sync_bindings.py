"""Durable active/completed SYNC transport bindings.

Completed records live in the worktree-specific Git administrative directory,
outside the Git working tree.  They are local integrity records, not signatures.
"""
from dataclasses import dataclass
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import uuid

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from .process_runner import run_process
from .sync_planning import (
    PACK_SUFFIXES,
    SyncPlanError,
    _load_pack_text,
    _validate_pack_schema,
    sync_format_checker,
)
from .utils import distribution_root, load_yaml


DURABLE_PACK_ID = re.compile(r'^SYNC-[0-9]{8}-[0-9a-f]{8}$')


class SyncBindingError(RuntimeError):
    exit_code = 3


@dataclass(frozen=True)
class SyncBinding:
    state: str
    path: Path
    pack: dict
    raw: bytes
    terminal: dict | None = None
    recoverable_active_path: Path | None = None

    def __iter__(self):
        # Preserve the tuple-shaped 0.6.0 internal API while callers migrate.
        yield self.path
        yield self.pack
        yield self.raw

    @property
    def original_pack_path(self):
        if self.terminal:
            return self.terminal['original_pack_path']
        return None


def _is_link_or_junction(path):
    return path.is_symlink() or getattr(path, 'is_junction', lambda: False)()


def _git(root, args):
    try:
        result = run_process(
            ['git', *args], cwd=root, capture_output=True, text=True,
            check=False, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SyncBindingError(f'cannot discover Git administrative directory: {exc}') from exc
    if result.returncode:
        raise SyncBindingError('cannot discover Git administrative directory')
    value = result.stdout.strip()
    if not value or '\n' in value or '\r' in value:
        raise SyncBindingError('Git returned an invalid administrative path')
    return value


def git_admin_dir(root):
    """Return the worktree-specific Git administrative directory."""
    root = Path(root).resolve()
    top = Path(_git(root, ['rev-parse', '--show-toplevel'])).resolve()
    if top != root:
        raise SyncBindingError('project root differs from the Git worktree root')
    value = Path(_git(root, ['rev-parse', '--absolute-git-dir']))
    if not value.is_absolute():
        raise SyncBindingError('Git administrative path is not absolute')
    admin = value.resolve()
    if not admin.is_dir():
        raise SyncBindingError('Git administrative path is not a directory')
    # Git may legitimately place linked-worktree administration outside root,
    # but the resolved administrative directory itself must not be a reparse link.
    if _is_link_or_junction(value) or _is_link_or_junction(admin):
        raise SyncBindingError('refusing a symlink/junction Git administrative directory')
    return admin


def _fixed_child(parent, name, *, create=False):
    if not re.fullmatch(r'[A-Za-z0-9._-]+', name) or name in {'.', '..'}:
        raise SyncBindingError(f'unsafe durable-store path component: {name!r}')
    if _is_link_or_junction(parent):
        raise SyncBindingError(f'refusing durable-store parent symlink/junction: {parent}')
    if create:
        parent.mkdir(parents=True, exist_ok=True)
    path = parent / name
    if path.exists() and _is_link_or_junction(path):
        raise SyncBindingError(f'refusing durable-store symlink/junction: {path}')
    try:
        path.resolve(strict=False).relative_to(parent.resolve())
    except ValueError as exc:
        raise SyncBindingError(f'durable-store path escapes its parent: {path}') from exc
    return path


def durable_roots(root, *, create=False):
    current = git_admin_dir(root)
    for name in ('project-system', 'sync'):
        current = _fixed_child(current, name, create=create)
        if create:
            current.mkdir(exist_ok=True)
    completed = _fixed_child(current, 'completed', create=create)
    transactions = _fixed_child(current, 'transactions', create=create)
    if create:
        completed.mkdir(exist_ok=True)
        transactions.mkdir(exist_ok=True)
    return completed, transactions


def _canonical_payload_sha256(document):
    payload = dict(document)
    payload.pop('integrity', None)
    raw = json.dumps(
        payload, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
    ).encode('utf-8')
    return sha256(raw).hexdigest()


def _seal(document):
    value = dict(document)
    value['integrity'] = {
        'algorithm': 'sha256',
        'payload_sha256': _canonical_payload_sha256(value),
    }
    return value


def _validate_seal(document, label):
    integrity = document.get('integrity')
    if not isinstance(integrity, dict) or set(integrity) != {'algorithm', 'payload_sha256'}:
        raise SyncBindingError(f'{label} integrity metadata is missing')
    if integrity.get('algorithm') != 'sha256' or integrity.get('payload_sha256') != _canonical_payload_sha256(document):
        raise SyncBindingError(f'{label} integrity hash mismatch')


def _binding_validator():
    schemas = distribution_root() / 'schemas'
    schema = json.loads((schemas / 'sync-terminal-binding.schema.json').read_text(encoding='utf-8'))
    pack_schema = json.loads((schemas / 'sync-pack.schema.json').read_text(encoding='utf-8'))
    registry = Registry().with_resource(pack_schema['$id'], Resource.from_contents(pack_schema))
    return Draft202012Validator(schema, registry=registry, format_checker=sync_format_checker())


def _validate_binding(binding, pack, raw, directory_name):
    errors = sorted(_binding_validator().iter_errors(binding), key=lambda error: str(list(error.path)))
    if errors:
        rendered = '; '.join(
            f'{".".join(map(str, error.path)) or "<root>"}: {error.message}' for error in errors
        )
        raise SyncBindingError(f'terminal binding schema validation failed: {rendered}')
    _validate_seal(binding, 'terminal binding')
    if directory_name != binding['pack_id'] or pack.get('pack_id') != binding['pack_id']:
        raise SyncBindingError('completed directory/pack/binding identity mismatch')
    if sha256(raw).hexdigest() != binding['pack_sha256']:
        raise SyncBindingError('archived pack bytes differ from terminal binding hash')
    provenance = pack.get('provenance') or {}
    checks = {
        'project_id': pack.get('project_id'),
        'request_id': provenance.get('request_id'),
        'request_sha256': provenance.get('request_sha256'),
        'base_commit': pack.get('base_commit'),
        'transport': provenance.get('transport'),
    }
    for field, value in checks.items():
        if binding.get(field) != value:
            raise SyncBindingError(f'terminal binding differs from archived pack field {field}')
    expected_path = f'inbox/sync/{binding["pack_id"]}.yaml'
    if binding['original_pack_path'] != expected_path:
        raise SyncBindingError('terminal binding has an invalid original inbox path')
    paths = binding['terminal']['verified_canonical_paths']
    if paths != sorted(paths):
        raise SyncBindingError('terminal binding verified paths are not normalized/sorted')
    for value in paths:
        pure = PurePosixPath(value)
        if (pure.is_absolute() or not pure.parts or any(part in {'', '.', '..'} for part in pure.parts)
                or ':' in pure.parts[0] or pure.parts[0] not in {'knowledge', 'docs'}):
            raise SyncBindingError(f'terminal binding contains an unsafe canonical path: {value}')


def _read_completed_dir(path, expected_pack_id=None):
    if not path.is_dir() or _is_link_or_junction(path):
        raise SyncBindingError(f'completed binding is not a safe directory: {path}')
    names = sorted(item.name for item in path.iterdir())
    if names != ['binding.json', 'pack.yaml']:
        raise SyncBindingError(f'completed binding has unexpected files: {path}')
    pack_path = _fixed_child(path, 'pack.yaml')
    binding_path = _fixed_child(path, 'binding.json')
    try:
        pack, _, raw = _load_pack_text(pack_path)
        _validate_pack_schema(pack)
        binding = json.loads(binding_path.read_text(encoding='utf-8'))
    except (OSError, ValueError, TypeError, SyncPlanError) as exc:
        raise SyncBindingError(f'cannot read completed binding {path.name}: {exc}') from exc
    _validate_binding(binding, pack, raw, expected_pack_id or path.name)
    return SyncBinding('completed', pack_path, pack, raw, binding)


def _transaction_document(binding):
    return _seal({
        'schema_version': 1,
        'state': 'publishing',
        'pack_id': binding['pack_id'],
        'pack_sha256': binding['pack_sha256'],
        'binding_sha256': binding['integrity']['payload_sha256'],
        'created_at': binding['terminal']['completed_at'],
        'binding': binding,
    })


def _validate_transaction(document, expected=None):
    if not isinstance(document, dict) or document.get('schema_version') != 1 or document.get('state') != 'publishing':
        raise SyncBindingError('terminal transaction has invalid structure')
    if not DURABLE_PACK_ID.fullmatch(str(document.get('pack_id', ''))):
        raise SyncBindingError('terminal transaction has invalid pack_id')
    for field in ('pack_sha256', 'binding_sha256'):
        if not re.fullmatch(r'[0-9a-f]{64}', str(document.get(field, ''))):
            raise SyncBindingError(f'terminal transaction has invalid {field}')
    _validate_seal(document, 'terminal transaction')
    binding = document.get('binding')
    if not isinstance(binding, dict) or binding.get('integrity', {}).get('payload_sha256') != document['binding_sha256']:
        raise SyncBindingError('terminal transaction binding payload is missing or inconsistent')
    if expected is not None and _binding_intent(binding) != _binding_intent(expected['binding']):
        raise SyncBindingError('existing terminal transaction differs from requested completion')


def _binding_intent(binding):
    value = deepcopy(binding)
    value.pop('integrity', None)
    terminal = value.get('terminal')
    if isinstance(terminal, dict):
        terminal.pop('completed_at', None)
    return value


def _write_exclusive(path, raw):
    with path.open('xb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def _write_atomic_json(path, document):
    temporary = path.with_name(f'.{path.name}.{uuid.uuid4().hex}.tmp')
    raw = (json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + '\n').encode('utf-8')
    try:
        _write_exclusive(temporary, raw)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def validate_transactions(root):
    _, transactions = durable_roots(root, create=False)
    if not transactions.exists():
        return []
    if _is_link_or_junction(transactions):
        raise SyncBindingError('refusing transactions symlink/junction')
    values = []
    entries = sorted(transactions.iterdir())
    for path in entries:
        if path.is_dir() and re.fullmatch(r'SYNC-[0-9]{8}-[0-9a-f]{8}\.[0-9a-f]{32}\.tmp', path.name):
            pack_id = path.name.split('.', 1)[0]
            if not (transactions / f'{pack_id}.json').is_file() or _is_link_or_junction(path):
                raise SyncBindingError(f'orphaned or unsafe terminal archive temporary: {path.name}')
            if any(item.name not in {'pack.yaml', 'binding.json'} or _is_link_or_junction(item) for item in path.iterdir()):
                raise SyncBindingError(f'unsafe terminal archive temporary contents: {path.name}')
            continue
        if path.suffix.lower() != '.json' or not path.is_file():
            raise SyncBindingError(f'unexpected terminal transaction store entry: {path.name}')
        if not DURABLE_PACK_ID.fullmatch(path.stem) or _is_link_or_junction(path):
            raise SyncBindingError(f'unsafe terminal transaction path: {path}')
        try:
            value = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError) as exc:
            raise SyncBindingError(f'cannot read terminal transaction {path.name}: {exc}') from exc
        _validate_transaction(value)
        if value['pack_id'] != path.stem:
            raise SyncBindingError('terminal transaction filename/internal ID mismatch')
        values.append(value)
    return values


def _cleanup_transaction_temps(transactions, pack_id):
    pattern = re.compile(rf'{re.escape(pack_id)}\.[0-9a-f]{{32}}\.tmp')
    for path in sorted(transactions.iterdir()):
        if not pattern.fullmatch(path.name):
            continue
        if not path.is_dir() or _is_link_or_junction(path):
            raise SyncBindingError(f'unsafe terminal archive temporary: {path}')
        children = list(path.iterdir())
        if any(child.name not in {'pack.yaml', 'binding.json'} or not child.is_file() or _is_link_or_junction(child)
               for child in children):
            raise SyncBindingError(f'unsafe terminal archive temporary contents: {path}')
        for child in children:
            child.unlink()
        path.rmdir()


def _active_bindings(root):
    inbox = Path(root) / 'inbox' / 'sync'
    if not inbox.exists():
        return []
    if _is_link_or_junction(inbox):
        raise SyncBindingError('refusing inbox/sync symlink/junction')
    bindings = []
    for path in sorted(item for item in inbox.iterdir() if item.is_file()):
        if path.suffix.lower() not in PACK_SUFFIXES:
            continue
        if _is_link_or_junction(path):
            raise SyncBindingError(f'refusing intake pack symlink/junction: {path}')
        try:
            pack, _, raw = _load_pack_text(path)
            _validate_pack_schema(pack)
        except SyncPlanError as exc:
            raise SyncBindingError(str(exc)) from exc
        bindings.append(SyncBinding('active', path, pack, raw))
    return bindings


def completed_bindings(root):
    completed, _ = durable_roots(root, create=False)
    if not completed.exists():
        return []
    if _is_link_or_junction(completed):
        raise SyncBindingError('refusing completed-store symlink/junction')
    values = []
    project_id = load_yaml(Path(root) / 'project.yaml').get('project', {}).get('id')
    for path in sorted(completed.iterdir()):
        if not DURABLE_PACK_ID.fullmatch(path.name):
            raise SyncBindingError(f'unsafe completed binding directory: {path.name}')
        binding = _read_completed_dir(path)
        if binding.terminal['project_id'] != project_id:
            raise SyncBindingError('completed binding targets a different local project_id')
        validate_completed_git_proof(root, binding)
        values.append(binding)
    return values


def validate_completed_git_proof(root, binding):
    """Recheck durable commit ancestry without contacting a remote."""
    terminal = binding.terminal['terminal']
    if terminal['outcome'] != 'pushed':
        return
    commit_sha = terminal['commit_sha']
    checks = [
        ['cat-file', '-e', f'{commit_sha}^{{commit}}'],
        ['merge-base', '--is-ancestor', commit_sha, 'HEAD'],
    ]
    for args in checks:
        result = run_process(
            ['git', *args], cwd=root, capture_output=True, check=False, timeout=30,
        )
        if result.returncode:
            raise SyncBindingError('terminal commit is missing or is not an ancestor of current HEAD')
    parents = run_process(
        ['git', 'rev-list', '--parents', '-n', '1', commit_sha], cwd=root,
        capture_output=True, text=True, check=False, timeout=30,
    )
    values = parents.stdout.split()
    if parents.returncode or len(values) != 2 or values[1].lower() != binding.terminal['base_commit']:
        raise SyncBindingError('terminal commit/base relationship is invalid')
    changed = run_process(
        ['git', 'diff-tree', '--root', '--no-commit-id', '--name-only', '-r', commit_sha, '--'],
        cwd=root, capture_output=True, text=True, check=False, timeout=30,
    )
    paths = sorted(line.replace('\\', '/') for line in changed.stdout.splitlines() if line)
    if changed.returncode or paths != terminal['verified_canonical_paths']:
        raise SyncBindingError('terminal commit paths differ from the completed binding')


def load_sync_bindings(root):
    """Load the one active/completed binding layer and detect collisions."""
    root = Path(root).resolve()
    validate_transactions(root)
    active = _active_bindings(root)
    completed = completed_bindings(root)
    active_by_id = {}
    for item in active:
        pack_id = item.pack.get('pack_id')
        if pack_id in active_by_id:
            raise SyncBindingError(f'duplicate pack_id in active inbox: {pack_id}')
        active_by_id[pack_id] = item
    completed_by_id = {}
    for item in completed:
        pack_id = item.pack['pack_id']
        if pack_id in completed_by_id:
            raise SyncBindingError(f'duplicate completed pack_id: {pack_id}')
        completed_by_id[pack_id] = item
    result = []
    for pack_id, item in active_by_id.items():
        archived = completed_by_id.get(pack_id)
        if archived is None:
            result.append(item)
            continue
        expected_active = root / archived.terminal['original_pack_path']
        if item.path.resolve() != expected_active.resolve() or item.raw != archived.raw or item.pack != archived.pack:
            raise SyncBindingError(f'active/completed integrity conflict for pack {pack_id}')
        completed_by_id[pack_id] = SyncBinding(
            'completed', archived.path, archived.pack, archived.raw,
            archived.terminal, item.path,
        )
    result.extend(completed_by_id.values())
    return sorted(result, key=lambda item: (item.pack['pack_id'], item.state))


def completed_binding(root, pack_id):
    if not DURABLE_PACK_ID.fullmatch(str(pack_id)):
        return None
    for item in completed_bindings(root):
        if item.pack['pack_id'] == pack_id:
            return item
    return None


def build_terminal_binding(pack, raw, *, outcome, reason, verification_fingerprint=None,
                           commit_sha=None, verified_paths=None, push_proof=None,
                           completed_at=None):
    provenance = pack.get('provenance') or {}
    transport = provenance.get('transport')
    if not isinstance(transport, dict) or transport.get('kind') != 'github_issue':
        raise SyncBindingError('durable terminal completion requires GitHub Issue transport provenance')
    pack_id = pack.get('pack_id')
    if not DURABLE_PACK_ID.fullmatch(str(pack_id)):
        raise SyncBindingError('transport pack_id is not eligible for durable completion')
    completed_at = completed_at or datetime.now(timezone.utc).isoformat()
    terminal = {
        'outcome': outcome,
        'completed_at': completed_at,
        'authorization': 'explicit_cli',
        'reason': reason,
        'verification_fingerprint': verification_fingerprint,
        'commit_sha': commit_sha,
        'verified_canonical_paths': sorted(verified_paths or []),
        'push_proof': push_proof,
    }
    return _seal({
        'schema_version': 1,
        'record_type': 'completed_sync_binding',
        'pack_id': pack_id,
        'pack_sha256': sha256(raw).hexdigest(),
        'project_id': pack.get('project_id'),
        'request_id': provenance.get('request_id'),
        'request_sha256': provenance.get('request_sha256'),
        'base_commit': pack.get('base_commit'),
        'original_pack_path': f'inbox/sync/{pack_id}.yaml',
        'transport': transport,
        'terminal': terminal,
    })


def _terminalize_hook(stage, **kwargs):
    """Test seam for crash/failure simulations; production implementation is inert."""


def terminalize_binding(root, active_path, pack, raw, binding):
    """Crash-safely publish one immutable completed record, then remove inbox input."""
    root = Path(root).resolve()
    expected_active = root / f'inbox/sync/{pack["pack_id"]}.yaml'
    active_path = Path(active_path).resolve()
    if active_path != expected_active.resolve():
        raise SyncBindingError('terminalization requires the canonical active inbox pack path')
    _validate_pack_schema(pack)
    _validate_binding(binding, pack, raw, pack['pack_id'])
    if active_path.is_symlink() or getattr(active_path, 'is_junction', lambda: False)():
        raise SyncBindingError('refusing to terminalize a symlink/junction pack')
    if not active_path.is_file() or active_path.read_bytes() != raw:
        raise SyncBindingError('active pack changed before terminalization')
    staged = run_process(
        ['git', 'ls-files', '--stage', '--', f'inbox/sync/{pack["pack_id"]}.yaml'],
        cwd=root, capture_output=True, text=True, check=False, timeout=30,
    )
    if staged.returncode or staged.stdout.strip():
        raise SyncBindingError('active transport pack must be untracked and unstaged before terminalization')

    completed, transactions = durable_roots(root, create=True)
    final = _fixed_child(completed, pack['pack_id'])
    transaction_path = _fixed_child(transactions, f'{pack["pack_id"]}.json')
    transaction = _transaction_document(binding)
    if transaction_path.exists():
        try:
            existing_transaction = json.loads(transaction_path.read_text(encoding='utf-8'))
        except (OSError, ValueError) as exc:
            raise SyncBindingError('cannot read existing terminal transaction') from exc
        _validate_transaction(existing_transaction, transaction)
        stored_binding = existing_transaction['binding']
        _validate_binding(stored_binding, pack, raw, pack['pack_id'])
        binding = stored_binding
        transaction = existing_transaction
    else:
        _write_exclusive(
            transaction_path,
            (json.dumps(transaction, indent=2, sort_keys=True) + '\n').encode('utf-8'),
        )
    _cleanup_transaction_temps(transactions, pack['pack_id'])
    _terminalize_hook('before_publish', pack_id=pack['pack_id'])

    if final.exists():
        existing = _read_completed_dir(final)
        if existing.raw != raw or existing.terminal != binding:
            raise SyncBindingError(f'completed record already exists with different content: {pack["pack_id"]}')
    else:
        temporary = _fixed_child(transactions, f'{pack["pack_id"]}.{uuid.uuid4().hex}.tmp')
        temporary.mkdir()
        try:
            _write_exclusive(temporary / 'pack.yaml', raw)
            _write_exclusive(
                temporary / 'binding.json',
                (json.dumps(binding, indent=2, sort_keys=True, ensure_ascii=False) + '\n').encode('utf-8'),
            )
            _read_completed_dir(temporary, pack['pack_id'])
            os.replace(temporary, final)
        finally:
            if temporary.exists():
                for child in temporary.iterdir():
                    child.unlink()
                temporary.rmdir()
    published = _read_completed_dir(final)
    if published.raw != raw or published.terminal != binding:
        raise SyncBindingError('published completed binding failed exact verification')
    validate_completed_git_proof(root, published)
    _terminalize_hook('after_publish', pack_id=pack['pack_id'])
    if active_path.read_bytes() != raw:
        raise SyncBindingError('active pack differs from published archive; refusing cleanup')
    active_path.unlink()
    _terminalize_hook('after_unlink', pack_id=pack['pack_id'])
    transaction_path.unlink(missing_ok=True)
    return published


def terminal_status(binding):
    terminal = binding.terminal['terminal']
    return {
        'status': 'completed',
        'pack_id': binding.pack['pack_id'],
        'project_id': binding.pack['project_id'],
        'request_id': binding.terminal['request_id'],
        'outcome': terminal['outcome'],
        'completed_at': terminal['completed_at'],
        'commit_sha': terminal.get('commit_sha'),
    }
