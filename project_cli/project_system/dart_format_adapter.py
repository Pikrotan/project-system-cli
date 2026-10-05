"""Read-only, project-owned Dart formatting verification (semantic schema v1)."""

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import stat

from .dart_analyze_adapter import _dart_version
from .process_runner import run_process
from .rule_scope import RuleScopeError, canonical_rule_path


EXCLUDED_DIRECTORIES = frozenset({
    '.git', '.generated', '.dart_tool', '.pub-cache', 'build', 'node_modules',
    '.venv', 'venv', '__pycache__', 'vendor', 'dist', '.pytest_cache',
    '.mypy_cache', '.ruff_cache', '.cache', '.tox', '.nox', 'coverage',
})
DART_FORMAT_TIMEOUT_SECONDS = 120
DART_FORMAT_MAX_CAPTURE_BYTES = 16 * 1024 * 1024
HASH_CHUNK_BYTES = 1024 * 1024


def _error(message):
    from .verification_adapters import VerificationAdapterError

    raise VerificationAdapterError(message)


def _lstat(path):
    try:
        info = os.lstat(path)
    except OSError:
        _error('Dart source path cannot be inspected reliably')
    if (stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0)
            & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)):
        _error('Dart source path is a symlink or reparse point')
    return info


def _root(value):
    selected = Path(value).absolute()
    for path in reversed((selected, *selected.parents)):
        if not stat.S_ISDIR(_lstat(path).st_mode):
            _error('Dart project root is not a regular directory')
    try:
        return selected.resolve(strict=True)
    except (OSError, RuntimeError):
        _error('Dart project root cannot be resolved reliably')


def _safe_source(root, relative):
    if _root(root) != root:
        _error('Dart source root containment changed')
    try:
        if canonical_rule_path(relative, 'Dart source', pattern=False) != relative:
            _error('Dart source path is not canonical')
    except RuleScopeError:
        _error('Dart source path is unsafe')
    if (not relative.endswith('.dart') or len(relative) > 4096
            or any(ord(char) < 32 or ord(char) == 127 for char in relative)):
        _error('Dart source path is unsupported')
    current = root
    parts = relative.split('/')
    for index, part in enumerate(parts):
        current /= part
        info = _lstat(current)
        required = stat.S_ISREG if index == len(parts) - 1 else stat.S_ISDIR
        if not required(info.st_mode):
            _error('Dart source path is not regular')
    try:
        if current.resolve(strict=True) != current:
            _error('Dart source path containment changed')
        current.relative_to(root)
    except (OSError, RuntimeError, ValueError):
        _error('Dart source path escapes the project')
    return current, info


def discover_dart_sources(project_root):
    """Own the complete sorted .dart inventory; never traverse reparse paths."""
    root = _root(project_root)
    pending, found = [root], []
    while pending:
        directory = pending.pop()
        if not stat.S_ISDIR(_lstat(directory).st_mode):
            _error('Dart source directory is not regular')
        try:
            with os.scandir(directory) as scan:
                entries = sorted(scan, key=lambda entry: entry.name)
        except OSError:
            _error('Dart source directory cannot be enumerated')
        for entry in entries:
            if entry.name.casefold() in EXCLUDED_DIRECTORIES:
                continue
            path = Path(entry.path)
            info = _lstat(path)
            if stat.S_ISDIR(info.st_mode):
                pending.append(path)
            elif stat.S_ISREG(info.st_mode):
                if entry.name.endswith('.dart'):
                    relative = path.relative_to(root).as_posix()
                    _safe_source(root, relative)
                    found.append(relative)
            else:
                _error('Dart discovery encountered an unsafe non-regular path')
    return tuple(sorted(found))


def _open_source(path):
    # Reuse the platform no-follow *read primitive* only, not OSV discovery,
    # scanner policy, executable/config attestation or semantic state.
    from .osv_scan_adapter import _open_binary

    return _open_binary(path)


def _regular_state(info):
    if (not stat.S_ISREG(info.st_mode) or getattr(info, 'st_file_attributes', 0)
            & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)):
        _error('Dart source handle is not a regular non-reparse file')
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _source_digest(root, relative):
    path, info = _safe_source(root, relative)
    before = _regular_state(info)
    digest, size = hashlib.sha256(), 0
    try:
        with _open_source(path) as stream:
            if _regular_state(os.fstat(stream.fileno())) != before:
                _error('Dart source changed while being opened')
            while chunk := stream.read(HASH_CHUNK_BYTES):
                size += len(chunk)
                if size > before[2]:
                    _error('Dart source changed while being read')
                digest.update(chunk)
            if (_regular_state(os.fstat(stream.fileno())) != before
                    or _regular_state(_safe_source(root, relative)[1]) != before
                    or size != before[2]):
                _error('Dart source changed while being read')
    except OSError:
        _error('Dart source cannot be read reliably')
    return digest.hexdigest()


def _source_state(root, paths):
    return tuple((relative, _source_digest(root, relative)) for relative in paths)


def _raw_update(digest, relative, text):
    # Framed, incremental provenance: do not retain every process transcript.
    for value in (relative, text):
        encoded = value.encode('utf-8')
        digest.update(len(encoded).to_bytes(8, 'big'))
        digest.update(encoded)


def _result(mode, sources=(), unformatted=(), *, tool_version='not_executed',
            stdout_sha256=None, stderr_sha256=None):
    from .verification_adapters import (
        VerificationAdapterResult, VerificationFinding, verification_result_sha256,
    )

    semantic = json.dumps({
        'schema_version': 1, 'source_paths': list(sources),
        'unformatted_paths': list(unformatted),
    }, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)
    empty = hashlib.sha256(b'').hexdigest()
    base = VerificationAdapterResult(
        adapter_id='dart.format', adapter_version='1', tool_name='dart',
        tool_version=tool_version, evaluation_mode=mode,
        verification_status=('FAIL' if unformatted else 'PASS') if sources else 'NOT_APPLICABLE',
        inspected_paths=(), findings=tuple(VerificationFinding(
            relative, None, None, 'ERROR', 'dart.format.required',
            'Dart source is not formatter-clean',
        ) for relative in unformatted),
        exit_code=int(bool(unformatted)), stdout_sha256=stdout_sha256 or empty,
        stderr_sha256=stderr_sha256 or empty, result_sha256='0' * 64,
        semantic_sha256=hashlib.sha256(semantic.encode('utf-8')).hexdigest(),
    )
    return replace(base, result_sha256=verification_result_sha256(base))


def run_dart_format(project_root, evaluation_paths, evaluation_mode):
    """Fixed per-file read-only checks; complete invalidation, never autofix."""
    if evaluation_mode == 'bounded':
        if evaluation_paths != ():
            _error('bounded dart.format requires empty paths')
        return _result(evaluation_mode)
    if evaluation_mode not in {'project_wide', 'project_wide_invalidation'} or evaluation_paths is not None:
        _error('unsupported dart.format evaluation mode or paths')
    root = _root(project_root)
    sources = discover_dart_sources(root)
    if not sources:
        return _result(evaluation_mode)
    before = _source_state(root, sources)  # F0: complete inventory + streamed bytes.
    tool_version = _dart_version(root)
    unformatted = []
    stdout_hash, stderr_hash = hashlib.sha256(), hashlib.sha256()
    for relative in sources:
        path, _ = _safe_source(root, relative)
        completed = run_process(
            ['dart', 'format', '--output=none', '--set-exit-if-changed', str(path)],
            cwd=root, capture_output=True, text=True, encoding='utf-8', check=False,
            shell=False, timeout=DART_FORMAT_TIMEOUT_SECONDS,
            max_capture_bytes=DART_FORMAT_MAX_CAPTURE_BYTES,
        )
        if (type(completed.returncode) is not int or completed.returncode not in {0, 1}
                or not isinstance(completed.stdout, str) or not isinstance(completed.stderr, str)):
            _error('Dart formatter did not establish a trustworthy result')
        if completed.returncode == 1:
            unformatted.append(relative)
        _raw_update(stdout_hash, relative, completed.stdout)
        _raw_update(stderr_hash, relative, completed.stderr)
    after_sources = discover_dart_sources(root)
    if after_sources != sources or _source_state(root, after_sources) != before:  # F1.
        _error('Dart source inventory or content changed during verification')
    return _result(evaluation_mode, sources, tuple(unformatted), tool_version=tool_version,
                   stdout_sha256=stdout_hash.hexdigest(), stderr_sha256=stderr_hash.hexdigest())
