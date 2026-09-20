"""Integrity-sealed Google Workspace bindings and immutable design imports."""
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import tempfile

from jsonschema import Draft202012Validator, FormatChecker

from .google_credentials import GoogleError
from .sync_bindings import git_admin_dir
from .utils import distribution_root


CHANGE_ID_RE = re.compile(r'^GDES-[0-9]{8}-[0-9a-f]{8}$')


class GoogleBindingError(GoogleError):
    exit_code = 8
    category = 'google_binding_integrity'


def _is_link(path):
    return path.is_symlink() or getattr(path, 'is_junction', lambda: False)()


def _safe_child(parent, name, *, directory=False, create=False):
    if not re.fullmatch(r'[A-Za-z0-9._-]+', name) or name in {'.', '..'}:
        raise GoogleBindingError('unsafe Google binding path component')
    if _is_link(parent):
        raise GoogleBindingError('Google binding parent is a symlink or junction')
    if create:
        parent.mkdir(parents=True, exist_ok=True)
    path = parent / name
    if path.exists() and _is_link(path):
        raise GoogleBindingError('Google binding path is a symlink or junction')
    try:
        path.resolve(strict=False).relative_to(parent.resolve())
    except ValueError as exc:
        raise GoogleBindingError('Google binding path escapes its durable store') from exc
    if directory and create:
        path.mkdir(exist_ok=True)
    return path


def google_store(root, *, create=False):
    current = git_admin_dir(root)
    for name in ('project-system', 'google'):
        current = _safe_child(current, name, directory=True, create=create)
    binding = _safe_child(current, 'workspace.json', create=create)
    imports = _safe_child(current, 'design-changes', directory=True, create=create)
    return binding, imports


def _canonical_hash(document):
    unsigned = dict(document)
    unsigned.pop('integrity', None)
    raw = json.dumps(
        unsigned, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
    ).encode('utf-8')
    return sha256(raw).hexdigest()


def seal(document):
    result = dict(document)
    result['integrity'] = {
        'algorithm': 'sha256', 'payload_sha256': _canonical_hash(result),
    }
    return result


def _schema(name):
    return json.loads(
        (distribution_root() / 'schemas' / name).read_text(encoding='utf-8')
    )


def _validate(document, schema_name, label):
    errors = sorted(
        Draft202012Validator(
            _schema(schema_name), format_checker=FormatChecker(),
        ).iter_errors(document),
        key=lambda error: str(list(error.path)),
    )
    if errors:
        rendered = '; '.join(
            f'{".".join(map(str, error.path)) or "<root>"}: {error.message}'
            for error in errors
        )
        raise GoogleBindingError(f'{label} schema validation failed: {rendered}')
    integrity = document.get('integrity')
    if (not isinstance(integrity, dict)
            or integrity.get('algorithm') != 'sha256'
            or integrity.get('payload_sha256') != _canonical_hash(document)):
        raise GoogleBindingError(f'{label} integrity hash mismatch')


def _atomic_json(path, document, *, exclusive=False):
    raw = (json.dumps(
        document, indent=2, sort_keys=True, ensure_ascii=False,
    ) + '\n').encode('utf-8')
    if exclusive:
        try:
            with path.open('xb') as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            return
        except FileExistsError as exc:
            raise GoogleBindingError(f'immutable Google record already exists: {path.name}') from exc
    handle, temporary = tempfile.mkstemp(
        prefix=f'.{path.name}.', suffix='.tmp', dir=str(path.parent),
    )
    try:
        with os.fdopen(handle, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def load_workspace_binding(root):
    path, _ = google_store(root)
    if not path.exists():
        return None
    if not path.is_file() or _is_link(path):
        raise GoogleBindingError('Google workspace binding is not a safe regular file')
    try:
        document = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GoogleBindingError('Google workspace binding is unreadable or malformed') from exc
    _validate(document, 'google-workspace-binding.schema.json', 'Google workspace binding')
    return document


def write_workspace_binding(root, binding):
    document = seal(binding)
    _validate(document, 'google-workspace-binding.schema.json', 'Google workspace binding')
    path, _ = google_store(root, create=True)
    if path.exists() and (not path.is_file() or _is_link(path)):
        raise GoogleBindingError('Google workspace binding target is unsafe')
    _atomic_json(path, document)
    reread = load_workspace_binding(root)
    if reread != document:
        raise GoogleBindingError('Google workspace binding failed reread verification')
    return reread


def import_path(root, change_id, *, create=False):
    if not CHANGE_ID_RE.fullmatch(str(change_id)):
        raise GoogleBindingError('invalid Google Design Change ID')
    _, imports = google_store(root, create=create)
    return _safe_child(imports, f'{change_id}.json', create=create)


def load_design_import(root, change_id):
    path = import_path(root, change_id)
    if not path.exists():
        return None
    if not path.is_file() or _is_link(path):
        raise GoogleBindingError('Google Design Change import is not a safe regular file')
    try:
        document = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GoogleBindingError('Google Design Change import is unreadable or malformed') from exc
    _validate(document, 'google-design-change.schema.json', 'Google Design Change import')
    if document.get('change_id') != change_id:
        raise GoogleBindingError('Google Design Change filename/internal identity mismatch')
    return document


def write_design_import(root, document):
    change_id = document.get('change_id')
    sealed = seal(document)
    _validate(sealed, 'google-design-change.schema.json', 'Google Design Change import')
    path = import_path(root, change_id, create=True)
    existing = load_design_import(root, change_id) if path.exists() else None
    if existing is not None:
        if existing != sealed:
            raise GoogleBindingError('immutable Google Design Change import differs from existing record')
        return existing, False
    _atomic_json(path, sealed, exclusive=True)
    reread = load_design_import(root, change_id)
    if reread != sealed:
        raise GoogleBindingError('Google Design Change import failed reread verification')
    return reread, True


def design_imports(root):
    _, directory = google_store(root)
    if not directory.exists():
        return []
    if not directory.is_dir() or _is_link(directory):
        raise GoogleBindingError('Google Design Change store is unsafe')
    result = []
    for path in sorted(directory.iterdir()):
        match = re.fullmatch(r'(GDES-[0-9]{8}-[0-9a-f]{8})\.json', path.name)
        if not match:
            raise GoogleBindingError('Google Design Change store contains an unexpected entry')
        result.append(load_design_import(root, match.group(1)))
    return result
