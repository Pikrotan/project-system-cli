"""Bounded Git-visible task-start state; derived, never canonical authority."""

from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import stat

from jsonschema import Draft202012Validator

from .process_runner import run_process
from .task_specification import COMMIT_RE, SHA_RE, TaskSpecificationError, _path
from .utils import atomic_write_text, distribution_root, ID_RE

PROFILE = 'project-system-task-baseline-v1'
MAX_BASELINE_BYTES = 8 * 1024 * 1024
GIT_MAX_CAPTURE_BYTES = 8 * 1024 * 1024
GIT_TIMEOUT_SECONDS = 30
TASK_FILES = ('context.md', 'manifest.json', 'task-spec.json', 'task-obligations.json',
              'task-baseline.json', 'task-verification.json', 'task-verification.md')


class TaskBaselineError(RuntimeError):
    """Task lifecycle state cannot be safely bound or reset implicitly."""


def safe_task_path(root, relative):
    """Containment and lstat of every component, including the project root."""
    root = Path(root).absolute()
    if not isinstance(relative, str):
        raise TaskBaselineError('unsafe task lifecycle path')
    pure = PurePosixPath(relative)
    if (pure.is_absolute() or pure.as_posix() != relative
            or not pure.parts or any(part in {'.', '..'} for part in pure.parts)
            or any(char in relative for char in '\\:\x00')):
        raise TaskBaselineError('unsafe task lifecycle path')
    current = root
    for part in (None, *pure.parts):
        if part is not None:
            current = current / part
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise TaskBaselineError('task path cannot be inspected') from exc
        if (stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0)
                & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)):
            raise TaskBaselineError('task path contains a symlink or reparse point')
        if current != root / relative and not stat.S_ISDIR(info.st_mode):
            raise TaskBaselineError('task path parent is not a directory')
    return root.joinpath(*pure.parts)


def task_directory(root, target, budget):
    if not isinstance(target, str) or not ID_RE.fullmatch(target):
        raise TaskBaselineError('task target must be a canonical object ID')
    if budget not in {'small', 'medium', 'large'}:
        raise TaskBaselineError('invalid task budget')
    relative = f'.generated/context/TASK-{target}-{budget}'
    output = safe_task_path(root, relative)
    for name in TASK_FILES:
        safe_task_path(root, relative + '/' + name)
    return output


def read_bounded(path, limit, label):
    try:
        with Path(path).open('rb') as stream:
            raw = stream.read(limit + 1)
    except OSError as exc:
        raise TaskBaselineError(f'{label} cannot be loaded') from exc
    if len(raw) > limit:
        raise TaskBaselineError(f'{label} exceeds size limit')
    return raw


def load_json(path, limit, label):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise TaskBaselineError(f'duplicate {label} JSON key')
            result[key] = value
        return result

    def nonfinite(value):
        raise TaskBaselineError(f'non-finite {label} JSON value')

    raw = read_bounded(path, limit, label)
    try:
        return json.loads(raw.decode('utf-8'), object_pairs_hook=unique, parse_constant=nonfinite)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise TaskBaselineError(f'{label} cannot be parsed') from exc


def validate_schema(document, name):
    schema = json.loads((distribution_root() / 'schemas' / name).read_text('utf-8'))
    try:
        error = next(Draft202012Validator(schema).iter_errors(document), None)
    except (RecursionError, TypeError, ValueError) as exc:
        raise TaskBaselineError('task artifact structure exceeds limits') from exc
    if error:
        location = '.'.join(map(str, error.absolute_path)) or '<root>'
        raise TaskBaselineError(f'invalid {name} at {location}')


def validate_entries(entries):
    paths = [entry['path'] for entry in entries]
    if paths != sorted(set(paths)):
        raise TaskBaselineError('task entries must be sorted and unique')
    for entry in entries:
        try:
            _path(entry['path'], 'task entry', pattern=False)
        except TaskSpecificationError as exc:
            raise TaskBaselineError(str(exc)) from exc
        if entry['state'] == 'file' and type(entry['bytes']) is not int:
            raise TaskBaselineError('entry bytes must be an integer')
        if entry['state'] == 'file' and not SHA_RE.fullmatch(entry['sha256']):
            raise TaskBaselineError('entry hash must be exact lowercase SHA-256')


def validate_task_baseline(document):
    validate_schema(document, 'task-baseline.schema.json')
    if type(document['schema_version']) is not int:
        raise TaskBaselineError('invalid task baseline version')
    if (not COMMIT_RE.fullmatch(document['base_commit'])
            or not SHA_RE.fullmatch(document['task_spec_sha256'])
            or not SHA_RE.fullmatch(document['task_obligations_sha256'])):
        raise TaskBaselineError('invalid exact task baseline Git/hash identity')
    validate_entries(document['entries'])
    return document


def serialize_json(document):
    return json.dumps(document, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False) + '\n'


def serialize_task_baseline(document):
    validate_task_baseline(document)
    text = serialize_json(document)
    if len(text.encode('utf-8')) > MAX_BASELINE_BYTES:
        raise TaskBaselineError('Task Baseline exceeds size limit')
    return text


def load_task_baseline(path):
    return validate_task_baseline(load_json(path, MAX_BASELINE_BYTES, 'Task Baseline'))


def write_task_artifact(root, path, text):
    try:
        relative = Path(path).absolute().relative_to(Path(root).absolute()).as_posix()
        if not relative.startswith('.generated/'):
            raise TaskBaselineError('task artifact write is outside .generated')
        safe_task_path(root, relative)
        atomic_write_text(path, text)
    except (OSError, ValueError, RuntimeError) as exc:
        if isinstance(exc, TaskBaselineError):
            raise
        raise TaskBaselineError('task artifact cannot be persisted safely') from exc


def _git(root, arguments):
    try:
        result = run_process(['git', *arguments], cwd=Path(root), shell=False,
                             capture_output=True, text=False, check=False,
                             timeout=GIT_TIMEOUT_SECONDS, max_capture_bytes=GIT_MAX_CAPTURE_BYTES)
    except Exception:
        raise TaskBaselineError('cannot collect bounded task Git state') from None
    if (type(getattr(result, 'returncode', None)) is not int or result.returncode != 0
            or not isinstance(result.stdout, bytes)):
        raise TaskBaselineError('cannot collect trustworthy task Git state')
    return result.stdout


def snapshot_paths(root, paths):
    entries = []
    for relative in sorted(set(paths)):
        try:
            _path(relative, 'Git-visible path', pattern=False)
        except TaskSpecificationError as exc:
            raise TaskBaselineError(str(exc)) from exc
        path = safe_task_path(root, relative)
        try:
            before = os.lstat(path)
            if not stat.S_ISREG(before.st_mode):
                raise TaskBaselineError('Git-visible task input is not a regular file')
            digest = sha256()
            size = 0
            with path.open('rb') as stream:
                opened = os.fstat(stream.fileno())
                if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                    raise TaskBaselineError('task input changed while opening')
                for chunk in iter(lambda: stream.read(128 * 1024), b''):
                    digest.update(chunk)
                    size += len(chunk)
            after = os.lstat(path)
            if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_mode) != (
                    after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_mode):
                raise TaskBaselineError('task input changed during snapshot')
            entries.append({'path': relative, 'state': 'file', 'sha256': digest.hexdigest(), 'bytes': size})
        except FileNotFoundError:
            entries.append({'path': relative, 'state': 'absent', 'sha256': None, 'bytes': None})
        except OSError as exc:
            raise TaskBaselineError('task input cannot be read safely') from exc
    return entries


def capture_task_state(root, head):
    # Ensure Git's root-relative names belong to this project, not an enclosing repo.
    try:
        top = _git(root, ['rev-parse', '--show-toplevel']).decode('utf-8').rstrip('\r\n')
        if Path(top).resolve() != Path(root).resolve():
            raise TaskBaselineError('task project must be the Git repository root')
        diff = ['diff', '--no-ext-diff', '--no-textconv', '--no-renames', '--name-only', '-z']
        # HEAD vs worktree alone misses staged B when filesystem bytes are back
        # at HEAD A. Include index-visible paths, but snapshot filesystem bytes.
        raw_paths = (_git(root, [*diff, head, '--'])
                     + _git(root, [*diff, '--cached', head, '--'])
                     + _git(root, ['ls-files', '--others', '--exclude-standard', '-z']))
        if raw_paths and not raw_paths.endswith(b'\x00'):
            raise TaskBaselineError('malformed Git task path stream')
        paths = {path.decode('utf-8') for path in raw_paths.split(b'\x00') if path}
    except (ValueError, UnicodeError) as exc:
        raise TaskBaselineError('Git task paths are not canonical UTF-8') from exc
    paths = {path for path in paths if PurePosixPath(path).parts[0].casefold() not in {'.git', '.generated'}}
    return snapshot_paths(root, paths)


def build_task_baseline(head, entries, spec_raw, obligations_raw):
    return validate_task_baseline({
        'schema_version': 1, 'profile': PROFILE, 'base_commit': head, 'entries': entries,
        'task_spec_sha256': sha256(spec_raw).hexdigest(),
        'task_obligations_sha256': sha256(obligations_raw).hexdigest(),
    })


def task_delta(baseline, current):
    before = {entry['path']: entry for entry in baseline}
    after = {entry['path']: entry for entry in current}
    return sorted(path for path in before.keys() | after.keys() if before.get(path) != after.get(path))
