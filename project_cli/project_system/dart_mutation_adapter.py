"""Strict Dart mutation verification, pinned to dart_mutant release v0.1.0.

Only disposable shadow files are intentionally mutated. This is observable
copy consistency, not a filesystem snapshot or a security sandbox for tests.
"""

from dataclasses import replace
from fnmatch import fnmatchcase
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import tempfile

from .process_runner import run_process
from .rule_scope import RuleScopeError, canonical_rule_path


ENGINE_VERSION = '0.1.0'
ENGINE_RELEASE_COMMIT = '231c599c14e3a1bcafeb408a042f98cad15ea1f9'
ENGINE_EXCLUSIONS = (
    '**/*.g.dart', '**/*.freezed.dart', '**/*.mocks.dart',
    '**/generated/**', '**/test/**', '**/*_test.dart',
)
SHADOW_EXCLUSIONS = frozenset({
    '.git', '.generated', 'build', 'node_modules', '__pycache__',
    '.pytest_cache', '.mypy_cache', '.ruff_cache', '.cache', '.tox', '.nox',
    'coverage', 'mutation-reports',
})
VERSION_TIMEOUT_SECONDS = 30
BASELINE_TIMEOUT_SECONDS = 300
MUTATION_TIMEOUT_SECONDS = 7200
MAX_CAPTURE_BYTES = 16 * 1024 * 1024
MAX_REPORT_BYTES = 64 * 1024 * 1024
CHUNK_BYTES = 1024 * 1024
MUTANT_FIELDS = ('path', 'start_line', 'start_column', 'end_line', 'end_column',
                 'mutator_name', 'replacement', 'status')
SUPPORTED_STATUSES = frozenset({'Killed', 'Timeout', 'Survived', 'NoCoverage', 'CompileError'})


def _error(message):
    from .verification_adapters import VerificationAdapterError

    raise VerificationAdapterError(message)


def _lstat(path):
    try:
        info = os.lstat(path)
    except OSError:
        _error('Mutation workspace path cannot be inspected')
    if (stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0)
            & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)):
        _error('Mutation workspace contains a symlink or reparse point')
    return info


def _root(value):
    path = Path(value).absolute()
    for ancestor in reversed((path, *path.parents)):
        if not stat.S_ISDIR(_lstat(ancestor).st_mode):
            _error('Mutation workspace root is not a regular directory')
    if path.resolve(strict=True) != path:
        _error('Mutation workspace root containment changed')
    return path


def _canonical(relative):
    try:
        canonical_rule_path(relative, 'mutation path', pattern=False)
    except RuleScopeError:
        _error('Mutation path is not canonical or is unsafe')
    if len(relative) > 4096 or any(ord(c) < 32 or ord(c) == 127 for c in relative):
        _error('Mutation path is unsupported')
    return relative


def _safe_file(root, relative):
    _root(root)
    _canonical(relative)
    path = root
    parts = relative.split('/')
    for index, part in enumerate(parts):
        path /= part
        info = _lstat(path)
        if not (stat.S_ISREG if index == len(parts) - 1 else stat.S_ISDIR)(info.st_mode):
            _error('Mutation workspace path is not regular')
    if path.resolve(strict=True) != path:
        _error('Mutation workspace file containment changed')
    return path, info


def _safe_directory(root, directory):
    _root(root)
    current = root
    for part in directory.relative_to(root).parts:
        current /= part
        if not stat.S_ISDIR(_lstat(current).st_mode):
            _error('Mutation snapshot directory ancestry is not regular')
    if current.resolve(strict=True) != directory:
        _error('Mutation snapshot directory containment changed')
    info = _lstat(directory)
    if not stat.S_ISDIR(info.st_mode):
        _error('Mutation snapshot directory is not regular')
    return info.st_dev, info.st_ino


def _file_state(info):
    if (not stat.S_ISREG(info.st_mode) or getattr(info, 'st_file_attributes', 0)
            & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)):
        _error('Mutation workspace handle is not a regular non-reparse file')
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, stat.S_IMODE(info.st_mode)


def _read_file(root, relative, destination=None, *, report=False):
    # Reuse only the existing platform-specific no-follow read primitive.
    from .osv_scan_adapter import _open_binary

    path, info = _safe_file(root, relative)
    before = _file_state(info)
    if report and before[2] > MAX_REPORT_BYTES:
        _error('Mutation JSON report exceeds its byte budget')
    digest, size, chunks = hashlib.sha256(), 0, []
    try:
        with _open_binary(path) as source:
            if _file_state(os.fstat(source.fileno())) != before:
                _error('Mutation workspace file changed while being opened')
            with_destination = destination.open('xb') if destination is not None else None
            try:
                while chunk := source.read(CHUNK_BYTES):
                    size += len(chunk)
                    if size > before[2] or (report and size > MAX_REPORT_BYTES):
                        _error('Mutation workspace file changed while being read')
                    digest.update(chunk)
                    if with_destination is not None:
                        with_destination.write(chunk)
                    if report:
                        chunks.append(chunk)
                if (_file_state(os.fstat(source.fileno())) != before
                        or _file_state(_safe_file(root, relative)[1]) != before or size != before[2]):
                    _error('Mutation workspace file changed while being read')
            finally:
                if with_destination is not None:
                    with_destination.close()
        if destination is not None:
            os.chmod(destination, before[4])
    except OSError:
        _error('Mutation workspace file cannot be read or copied reliably')
    return b''.join(chunks) if report else (before[4], digest.hexdigest())


def _inventory(root, destination=None):
    """Complete regular-file copy/inventory, with no links or special entries."""
    root = _root(root)
    pending, files, directories = [root], [], []
    while pending:
        directory = pending.pop()
        before_directory = _safe_directory(root, directory)
        try:
            with os.scandir(directory) as scan:
                entries = sorted(scan, key=lambda entry: entry.name)
        except OSError:
            _error('Mutation snapshot directory cannot be enumerated')
        if _safe_directory(root, directory) != before_directory:
            _error('Mutation snapshot directory changed while being enumerated')
        for entry in entries:
            if entry.name.casefold() in SHADOW_EXCLUSIONS:
                continue
            path = Path(entry.path)
            relative = _canonical(path.relative_to(root).as_posix())
            info = _lstat(path)
            if stat.S_ISDIR(info.st_mode):
                directories.append(relative)
                if destination is not None:
                    (destination / relative).mkdir()
                pending.append(path)
            elif stat.S_ISREG(info.st_mode):
                copied = destination / relative if destination is not None else None
                files.append((relative, *_read_file(root, relative, copied)))
            else:
                _error('Mutation snapshot contains an unsafe special entry')
        if _safe_directory(root, directory) != before_directory:
            _error('Mutation snapshot directory changed during copying')
    return tuple(sorted(directories)), tuple(sorted(files))


def _targets(inventory):
    # Rust glob 0.3 defaults are case-sensitive and do not require literal
    # separators. WalkDir(".") paths have a ./ prefix, including root files.
    return tuple(relative for relative, *_ in inventory[1]
                 if relative.endswith('.dart')
                 and not any(fnmatchcase('./' + relative, pattern) for pattern in ENGINE_EXCLUSIONS))


def discover_mutation_sources(project_root):
    return _targets(_inventory(project_root))


def _execute(argv, root, timeout, stdout_hash, stderr_hash, label):
    try:
        completed = run_process(argv, cwd=root, capture_output=True, text=True,
                                encoding='utf-8', check=False, shell=False,
                                timeout=timeout, max_capture_bytes=MAX_CAPTURE_BYTES)
        if (type(completed.returncode) is not int or completed.returncode != 0
                or not isinstance(completed.stdout, str) or not isinstance(completed.stderr, str)):
            _error(label + ' did not establish a trustworthy successful process result')
        for digest, text in ((stdout_hash, completed.stdout), (stderr_hash, completed.stderr)):
            for value in (label, text):
                data = value.encode('utf-8')
                digest.update(len(data).to_bytes(8, 'big'))
                digest.update(data)
        return completed
    except Exception:
        # No raw transcripts, exception text, source or temporary paths escape.
        _error(label + ' execution failed or returned an invalid process result')


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _error('Mutation JSON report contains duplicate keys')
        result[key] = value
    return result


def _float(value):
    selected = float(value)
    if not math.isfinite(selected):
        _error('Mutation JSON report contains a non-finite number')
    return selected


def _constant(_value):
    _error('Mutation JSON report contains a non-finite number')


def _position(value):
    if (not isinstance(value, dict) or set(value) != {'line', 'column'}
            or any(type(value[key]) is not int or value[key] < 1 for key in ('line', 'column'))):
        _error('Mutation JSON report contains a malformed location')
    return value['line'], value['column']


def _report_path(value):
    if not isinstance(value, str):
        _error('Mutation JSON report path is malformed')
    # The pinned engine emits native PathBuf display (./ on POSIX, .\ on
    # Windows); normalize only these supported relative spellings, not .. .
    value = value.replace('\\', '/')
    if value.startswith('./'):
        value = value[2:]
    return _canonical(value)


def _parse_report(report_root, targets):
    try:
        raw = _read_file(report_root, 'mutation-report.json', report=True)
        payload = json.loads(raw.decode('utf-8'), object_pairs_hook=_pairs,
                             parse_float=_float, parse_constant=_constant)
    except (ValueError, UnicodeError, RecursionError):
        _error('Mutation JSON report is malformed')
    if (not isinstance(payload, dict) or payload.get('schemaVersion') != '1'
            or not isinstance(payload.get('files'), dict)):
        _error('Mutation JSON report structure or schema is unsupported')
    records, seen, file_paths = [], set(), set()
    for raw_path, file in payload['files'].items():
        path = _report_path(raw_path)
        if path not in targets or path in file_paths:
            _error('Mutation JSON report path is not a unique applicable target')
        file_paths.add(path)
        if (not isinstance(file, dict) or file.get('language') != 'dart'
                or not isinstance(file.get('mutants'), list)):
            _error('Mutation JSON report file structure or language is unsupported')
        for mutant in file['mutants']:
            if not isinstance(mutant, dict):
                _error('Mutation JSON report mutant is malformed')
            location = mutant.get('location')
            if not isinstance(location, dict) or set(location) != {'start', 'end'}:
                _error('Mutation JSON report location is malformed')
            start, end = _position(location['start']), _position(location['end'])
            if end < start:
                _error('Mutation JSON report location is reversed')
            name, replacement, status_value = (mutant.get(key) for key in ('mutatorName', 'replacement', 'status'))
            if (not isinstance(name, str) or not name or name != name.strip()
                    or any(ord(c) < 32 or ord(c) == 127 for c in name)
                    or not isinstance(replacement, str)):
                _error('Mutation JSON report operator or replacement is malformed')
            if not isinstance(status_value, str) or status_value not in SUPPORTED_STATUSES:
                _error('Mutation JSON report status is unsupported')
            identity = (path, *start, *end, name, replacement)
            if identity in seen:
                _error('Mutation JSON report contains duplicate or contradictory mutants')
            seen.add(identity)
            if status_value == 'CompileError':
                _error('Mutation JSON report contains an infrastructure-status mutant')
            records.append(dict(zip(MUTANT_FIELDS, (*identity, status_value))))
    if not records:
        _error('Mutation targets exist but no trustworthy mutant inventory was established')
    return tuple(sorted(records, key=lambda record: tuple(record[key] for key in MUTANT_FIELDS)))


def _result(mode, mutants=(), *, tool_version='not_executed', stdout_hash=None, stderr_hash=None):
    from .verification_adapters import (
        VerificationAdapterResult, VerificationFinding, verification_result_sha256,
    )

    findings = []
    for mutant in mutants:
        if mutant['status'] in {'Survived', 'NoCoverage'}:
            survived = mutant['status'] == 'Survived'
            findings.append(VerificationFinding(
                mutant['path'], mutant['start_line'], mutant['start_column'], 'ERROR',
                'dart.mutation.survived' if survived else 'dart.mutation.no_coverage',
                ('Mutation survived the Dart test suite' if survived else 'Mutation has no test coverage')
                + f" ({mutant['mutator_name']})",
            ))
    semantic = json.dumps({'schema_version': 1, 'mutants': list(mutants)}, sort_keys=True,
                          separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode('utf-8')
    base = VerificationAdapterResult(
        'dart.mutation.strict', '1', 'dart_mutant', tool_version, mode,
        ('FAIL' if findings else 'PASS') if mutants else 'NOT_APPLICABLE', (),
        tuple(sorted(findings, key=lambda finding: (finding.path, finding.line, finding.column,
                                                  finding.severity, finding.code, finding.message))),
        0, (stdout_hash or hashlib.sha256()).hexdigest(), (stderr_hash or hashlib.sha256()).hexdigest(),
        '0' * 64, hashlib.sha256(semantic).hexdigest(),
    )
    return replace(base, result_sha256=verification_result_sha256(base))


def run_dart_mutation(project_root, evaluation_paths, evaluation_mode):
    if evaluation_mode == 'bounded':
        if evaluation_paths != ():
            _error('Bounded mutation verification requires empty paths')
        return _result(evaluation_mode)
    if evaluation_mode not in {'project_wide', 'project_wide_invalidation'} or evaluation_paths is not None:
        _error('Unsupported mutation evaluation mode or paths')
    root = _root(project_root)
    before = _inventory(root)
    targets = _targets(before)
    if not targets:
        return _result(evaluation_mode)
    stdout_hash, stderr_hash = hashlib.sha256(), hashlib.sha256()
    version = _execute(['dart_mutant', '--version'], root, VERSION_TIMEOUT_SECONDS,
                       stdout_hash, stderr_hash, 'Mutation version probe')
    if not re.fullmatch(r'dart_mutant 0\.1\.0(?:\r?\n)?', version.stdout):
        _error('Mutation engine must be exactly dart_mutant 0.1.0')
    temporary_base = _root(tempfile.gettempdir())
    if temporary_base == root or root in temporary_base.parents:
        _error('Mutation temporary workspace must be outside the canonical project')
    with tempfile.TemporaryDirectory(prefix='project-system-dart-mutation-', dir=temporary_base) as temporary:
        temporary_root = _root(temporary)
        if temporary_root == root or root in temporary_root.parents:
            _error('Mutation temporary workspace must be outside the canonical project')
        shadow, report_root = temporary_root / 'project', temporary_root / 'report'
        shadow.mkdir()
        report_root.mkdir()
        if _inventory(root, shadow) != before or _inventory(root) != before:
            _error('Project inventory or bytes changed during mutation snapshot copying')
        _execute(['dart', 'test', '--reporter=compact'], shadow, BASELINE_TIMEOUT_SECONDS,
                 stdout_hash, stderr_hash, 'Dart mutation green baseline')
        # A baseline must not silently rewrite the actual applicable sources.
        after_baseline = _inventory(shadow)
        if _targets(after_baseline) != targets:
            _error('Dart baseline changed the applicable mutation target inventory')
        baseline_files = {relative: (mode, digest) for relative, mode, digest in after_baseline[1]}
        for relative, mode, digest in before[1]:
            if relative in targets and baseline_files[relative] != (mode, digest):
                _error('Dart baseline changed an applicable mutation source')
        _execute(['dart_mutant', '--path', '.', '--parallel', '1', '--timeout', str(BASELINE_TIMEOUT_SECONDS),
                  '--threshold', '0', '--quiet', '--json', '--ai', 'none', '--output', str(report_root)],
                 shadow, MUTATION_TIMEOUT_SECONDS, stdout_hash, stderr_hash, 'Dart mutation engine')
        mutants = _parse_report(report_root, targets)
        if _inventory(root) != before:
            _error('Canonical project inventory or bytes changed during mutation verification')
        return _result(evaluation_mode, mutants, tool_version=ENGINE_VERSION,
                       stdout_hash=stdout_hash, stderr_hash=stderr_hash)
