"""B2 private local byte storage, not execution/sealing authority or Evidence.

Trusted ancestors/cooperative writers only; no power-loss directory guarantee.
Ignore files are protective conventions, not access control or an OS sandbox.
"""

from hashlib import sha256
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile

from .extraction_pack import _read_exact
from .process_runner import run_process
from .source_layer import SourceError, checked_path


MAX_RESPONSE_BYTES = 65536


class PayloadError(SourceError):
    """Local payload cannot be safely stored or consumed."""


def payload_relative(attempt_id):
    if not isinstance(attempt_id, str) or not re.fullmatch(r'ATTEMPT-[0-9a-f]{32}', attempt_id):
        raise PayloadError('invalid local payload attempt identity')
    return f'.project-local/storage/executions/{attempt_id}/response.bin'


def _payload_path(root, attempt_id):
    return checked_path(Path(root).absolute() / payload_relative(attempt_id))


def _git(root, *arguments):
    try:
        return run_process(['git', *arguments], cwd=root, capture_output=True,
                           timeout=30, max_capture_bytes=64 * 1024, check=False)
    except (OSError, subprocess.SubprocessError, RuntimeError, ValueError) as exc:
        raise PayloadError('cannot verify local storage Git protection') from exc


def _ignore_rule(root, filename, accepted):
    try:
        raw, _, _ = _read_exact(checked_path(Path(root).absolute() / filename),
                                64 * 1024, 'local storage protective configuration')
        lines = [line.strip() for line in raw.decode('utf-8').splitlines()
                 if line.strip() and not line.lstrip().startswith('#')]
        indices = [index for index, line in enumerate(lines) if line in accepted]
        # Conservative policy: do not attempt a proprietary git/AI glob interpreter.
        if not indices or any(line.startswith('!') for line in lines[max(indices) + 1:]):
            raise PayloadError('local storage protective configuration missing or ambiguous')
    except (SourceError, OSError, UnicodeError, ValueError) as exc:
        raise PayloadError('local storage protective configuration missing or unsafe') from exc


def require_protection(root):
    """Read-only gate; never repair user config or policies automatically."""
    root = checked_path(Path(root).absolute())
    _ignore_rule(root, '.gitignore', {'/.project-local/', '.project-local/'})
    _ignore_rule(root, '.llmignore', {'.project-local/**', '/.project-local/**'})
    repository = _git(root, 'rev-parse', '--show-toplevel')
    if repository.returncode:
        if (repository.returncode == 128 and b'not a git repository' in repository.stderr.lower()
                and not any(os.path.lexists(parent / '.git') for parent in (root, *root.parents))):
            return
        raise PayloadError('cannot establish repository for local storage protection')
    try:
        root.relative_to(Path(os.fsdecode(repository.stdout.strip())).absolute())
    except ValueError as exc:
        raise PayloadError('local storage Git repository mismatch') from exc
    tracked = _git(root, 'ls-files', '-z', '--', ':(icase).project-local', ':(icase).project-local/**')
    if tracked.returncode:
        raise PayloadError('cannot verify tracked local storage')
    if tracked.stdout:
        raise PayloadError('local storage is already tracked by Git; explicit operator intervention required')
    probes = ('.project-local/storage/executions/ATTEMPT-' + '0' * 32 + '/response.bin',
              '.project-local/execution-staging/probe.tmp')
    ignored = _git(root, 'check-ignore', '--no-index', '--', *probes)
    if ignored.returncode or set(ignored.stdout.decode('utf-8').splitlines()) != set(probes):
        raise PayloadError('Git does not protect local response storage and staging')


def _read_payload(root, attempt_id, commitment):
    expected_path = payload_relative(attempt_id)
    if (not isinstance(commitment, dict) or commitment.get('path') != expected_path
            or type(commitment.get('bytes')) is not int
            or not 0 < commitment['bytes'] <= MAX_RESPONSE_BYTES):
        raise PayloadError('invalid local payload commitment')
    raw, digest, size = _read_exact(_payload_path(root, attempt_id), MAX_RESPONSE_BYTES, 'sealed response')
    if (digest, size) != (commitment['sha256'], commitment['bytes']):
        raise PayloadError('local payload SHA-256/byte count mismatch')
    return raw


def inspect_payload(root, attempt_id, commitment=None):
    """Current local availability only; never infer immutable execution history."""
    try:
        path = _payload_path(root, attempt_id)
        if path.parent.exists():
            if not stat.S_ISDIR(os.lstat(path.parent).st_mode):
                return 'UNSAFE'
            if any(child.name != 'response.bin' for child in path.parent.iterdir()):
                return 'UNSAFE'
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            return 'MISSING' if commitment is not None else 'UNRESOLVED'
        if not stat.S_ISREG(info.st_mode):
            return 'UNSAFE'
    except (SourceError, OSError, ValueError):
        return 'UNSAFE'
    if commitment is None:
        # Even expected fake bytes without a referencing outcome are not a result.
        return 'ORPHAN'
    try:
        _read_payload(root, attempt_id, commitment)
    except (SourceError, OSError, ValueError, KeyError):
        return 'CORRUPT'
    return 'AVAILABLE'


def _read_verified_payload_bytes(root, attempt_id, commitment):
    """Internal bytes-only reader; execution owner must first verify provenance."""
    if inspect_payload(root, attempt_id, commitment) != 'AVAILABLE':
        raise PayloadError('sealed payload is not safely available')
    return _read_payload(root, attempt_id, commitment)


def inspect_local_orphans(root, attempts):
    """Inventory only this local storage namespace; never mutate/reconstruct receipts."""
    relative = '.project-local/storage/executions'
    issues = []
    try:
        directory = checked_path(Path(root).absolute() / relative)
        try:
            info = os.lstat(directory)
        except FileNotFoundError:
            return ()
        if not stat.S_ISDIR(info.st_mode):
            raise PayloadError('unsafe local execution storage')
        for child in directory.iterdir():
            rel = relative + '/' + child.name
            try:
                payload_relative(child.name)
                safe = checked_path(child)
                if not stat.S_ISDIR(os.lstat(safe).st_mode):
                    raise PayloadError('unsafe local execution storage entry')
                known = attempts.get(child.name)
                if known is not None and known.schema_version == 3:
                    continue  # Independently inspected with its exact v3 history.
                status = inspect_payload(root, child.name)
                if status == 'UNSAFE':
                    issues.append(('ERROR', rel, 'PAYLOAD_UNSAFE; no valid referencing execution'))
                else:
                    issues.append(('WARNING', rel, 'PAYLOAD_ORPHAN; no valid referencing execution; explicit review required'))
            except (SourceError, OSError, ValueError):
                issues.append(('ERROR', rel, 'PAYLOAD_UNSAFE; unknown local execution storage entry'))
    except (SourceError, OSError, ValueError):
        issues.append(('ERROR', relative, 'PAYLOAD_UNSAFE; local storage cannot be inspected safely'))
    return tuple(sorted(set(issues)))


def _publish_payload(root, attempt_id, response, commitment, boundary):
    """No-clobber byte publication; does not create an XINV or authorize execution."""
    require_protection(root)
    if (type(response) is not bytes or not 0 < len(response) <= MAX_RESPONSE_BYTES
            or commitment != {'path': payload_relative(attempt_id),
                              'sha256': sha256(response).hexdigest(), 'bytes': len(response)}):
        raise PayloadError('invalid observed payload commitment')
    path = _payload_path(root, attempt_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path = _payload_path(root, attempt_id)
    staging = checked_path(Path(root).absolute() / '.project-local/execution-staging')
    staging.mkdir(parents=True, exist_ok=True)
    staging = checked_path(staging)
    descriptor, name = tempfile.mkstemp(prefix='.response-', suffix='.tmp', dir=staging)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(response)
            stream.flush()
            os.fsync(stream.fileno())
        _payload_path(root, attempt_id)
        boundary('before_blob_publication')
        try:
            os.link(temporary, path)
        except FileExistsError as exc:
            raise PayloadError('local payload already exists; no replacement or reuse') from exc
        except (OSError, NotImplementedError) as exc:
            raise PayloadError('atomic local payload hard link unsupported; fail closed') from exc
        boundary('after_blob_publication')
        _read_payload(root, attempt_id, commitment)
    finally:
        temporary.unlink(missing_ok=True)
