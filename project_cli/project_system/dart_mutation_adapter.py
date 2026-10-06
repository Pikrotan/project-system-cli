"""Strict Dart mutation verification, pinned to dart_mutant release v0.1.0.

Only disposable shadow files are intentionally mutated. This is observable
copy consistency, not a filesystem snapshot or a security sandbox for tests.
"""

from dataclasses import dataclass, replace
from fnmatch import fnmatchcase
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
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
ANALYZE_TIMEOUT_SECONDS = 120
MUTATION_TIMEOUT_SECONDS = 7200
MAX_CAPTURE_BYTES = 16 * 1024 * 1024
MAX_REPORT_BYTES = 64 * 1024 * 1024
CHUNK_BYTES = 1024 * 1024
MUTANT_FIELDS = ('path', 'start_line', 'start_column', 'end_line', 'end_column',
                 'mutator_name', 'replacement', 'status')
SUPPORTED_STATUSES = frozenset({'Killed', 'Timeout', 'Survived', 'NoCoverage', 'CompileError'})


def _error(message):
    from .verification_adapters import VerificationAdapterError

    raise VerificationAdapterError(message) from None


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


def _read_file(root, relative, destination=None, *, report=False, content=False):
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
                    if report or content:
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
    return b''.join(chunks) if report or content else (before[4], digest.hexdigest())


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


def _hash_transport(stdout_hash, stderr_hash, label, stdout, stderr):
    for digest, text in ((stdout_hash, stdout), (stderr_hash, stderr)):
        for value in (label, text):
            data = value.encode('utf-8')
            if len(data) > MAX_CAPTURE_BYTES:
                _error('Mutation process transport exceeds its byte budget')
            digest.update(len(data).to_bytes(8, 'big'))
            digest.update(data)


def _execute(argv, root, timeout, stdout_hash, stderr_hash, label, *, allowed_exits=(0,), repeated_timeout=False):
    try:
        completed = run_process(argv, cwd=root, capture_output=True, text=True,
                                encoding='utf-8', check=False, shell=False,
                                timeout=timeout, max_capture_bytes=MAX_CAPTURE_BYTES)
        if (type(completed.returncode) is not int or completed.returncode not in allowed_exits
                or not isinstance(completed.stdout, str) or not isinstance(completed.stderr, str)):
            _error(label + ' did not establish a trustworthy successful process result')
        _hash_transport(stdout_hash, stderr_hash, label, completed.stdout, completed.stderr)
        return completed
    except subprocess.TimeoutExpired as exc:
        if not repeated_timeout or exc.timeout != BASELINE_TIMEOUT_SECONDS or exc.cmd != argv:
            _error(label + ' did not establish the required independent outcome')
        # A bounded runner timeout is authority only for an engine Timeout.
        # Python may provide bytes even with text=True; never decode or expose
        # potentially truncated transcripts as protocol or semantic evidence.
        for digest, value in ((stdout_hash, exc.stdout), (stderr_hash, exc.stderr)):
            if value is None:
                value = b''
            if isinstance(value, str):
                value = value.encode('utf-8')
            if not isinstance(value, bytes) or len(value) > MAX_CAPTURE_BYTES:
                _error('Mutation timeout transport exceeds its byte budget')
            digest.update(b'independent-timeout\0')
            digest.update(len(value).to_bytes(8, 'big'))
            digest.update(value)
        return None
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


@dataclass(frozen=True)
class _ReplayMutation:
    # Execution-only data: none of these bytes/offsets/IDs enter Evidence.
    record: dict
    original_bytes: bytes
    mutated_bytes: bytes
    mode: int


def _reconstruct(source_root, raw_path, mutant, record, trusted_sources):
    path = record['path']
    source = _read_file(source_root, path, content=True)
    mode = stat.S_IMODE(_safe_file(source_root, path)[1].st_mode)
    if (mode, hashlib.sha256(source).hexdigest()) != trusted_sources[path]:
        _error('Mutation reconstruction source does not match the trusted snapshot')
    try:
        source.decode('utf-8')
        replacement = record['replacement'].encode('utf-8')
        starts = [0] + [index + 1 for index, byte in enumerate(source) if byte == 10]
        line, column = record['start_line'], record['start_column']
        length = record['end_column'] - column
        if record['end_line'] != line or length <= 0 or line > len(starts):
            _error('Mutation source location violates the pinned byte-span contract')
        start = starts[line - 1] + column - 1
        # The start must belong to its encoded row, never spill into the next.
        line_end = starts[line] if line < len(starts) else len(source)
        if start >= line_end or start + length > len(source):
            _error('Mutation source byte span is outside the trusted baseline')
        source[:start].decode('utf-8')
        original = source[start:start + length].decode('utf-8')
        source[start + length:].decode('utf-8')
        identifier = mutant.get('id')
        expected = hashlib.md5(f'{raw_path}:{line}:{original}:{record["replacement"]}'.encode('utf-8')).hexdigest()
        if not isinstance(identifier, str) or not re.fullmatch('[0-9a-f]{32}', identifier) or identifier != expected:
            _error('Mutation engine ID does not match the reconstructed pinned identity')
        return _ReplayMutation(record, source, source[:start] + replacement + source[start + length:], mode)
    except UnicodeError:
        _error('Mutation source byte span or replacement is not valid UTF-8')


def _parse_report(report_root, targets, source_root, trusted_sources):
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
            record = dict(zip(MUTANT_FIELDS, (*identity, status_value)))
            records.append(_reconstruct(source_root, raw_path, mutant, record, trusted_sources))
    if not records:
        _error('Mutation targets exist but no trustworthy mutant inventory was established')
    return tuple(sorted(records, key=lambda item: tuple(item.record[key] for key in MUTANT_FIELDS)))


def _assert_targets(root, baseline, *, changed=None):
    current = _inventory(root)
    targets = _targets(baseline)
    if _targets(current) != targets:
        _error('Mutation process changed the applicable source inventory')
    expected = {relative: (mode, digest) for relative, mode, digest in baseline[1] if relative in targets}
    if changed is not None:
        expected[changed.record['path']] = (changed.mode, hashlib.sha256(changed.mutated_bytes).hexdigest())
    actual = {relative: (mode, digest) for relative, mode, digest in current[1] if relative in targets}
    if actual != expected:
        _error('Mutation process did not preserve the required source bytes and modes')


def _protocol_file(root, raw_path, *, absolute=False):
    if not isinstance(raw_path, str) or not raw_path:
        _error('Mutation attestation protocol path is malformed')
    path = Path(raw_path)
    if absolute and not path.is_absolute():
        _error('Mutation analyzer protocol requires an absolute diagnostic path')
    if path.is_absolute():
        try:
            relative = path.relative_to(root).as_posix()
        except ValueError:
            _error('Mutation attestation protocol path escapes its shadow')
    else:
        relative = _report_path(raw_path)
    # Inspect ancestry without following links before qualified parsers resolve.
    _safe_file(root, relative)


def _analyze(root, stdout_hash, stderr_hash):
    from .dart_analyze_adapter import (
        _decode_machine_fields, _expected_analyze_exit_code, _parse_machine_output,
    )

    completed = _execute(['dart', 'analyze', '--format=machine', '--no-plugins', '.'], root,
                         ANALYZE_TIMEOUT_SECONDS, stdout_hash, stderr_hash,
                         'Dart mutation static validity', allowed_exits=(0, 1, 2, 3))
    _inventory(root)  # Detect command-created unsafe ancestry before resolution.
    try:
        for index, line in enumerate(completed.stdout.splitlines(), 1):
            if line:
                fields = _decode_machine_fields(line, output_line=index)
                _protocol_file(root, fields[3], absolute=True)
        findings = _parse_machine_output(root, completed.stdout)
        if completed.returncode != _expected_analyze_exit_code(findings):
            _error('Mutation analyzer exit disagrees with its machine diagnostics')
        if any(finding.severity == 'ERROR' for finding in findings):
            _error('Mutation attestation requires an analyzer ERROR-free project')
    except Exception:
        _error('Mutation analyzer did not establish trustworthy static validity')


def _write_reconstructed(root, item):
    """Owned shadow write with no-follow handle, checked before truncation."""
    path, info = _safe_file(root, item.record['path'])
    before = _file_state(info)
    if before[4] != item.mode or _read_file(root, item.record['path'], content=True) != item.original_bytes:
        _error('Mutation replay target does not match the trusted baseline')
    fd = None
    try:
        if os.name == 'nt':
            import ctypes
            from ctypes import wintypes
            import msvcrt

            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            create = kernel.CreateFileW
            create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                               wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
            create.restype = wintypes.HANDLE
            close = kernel.CloseHandle
            close.argtypes = [wintypes.HANDLE]
            close.restype = wintypes.BOOL
            # GENERIC_WRITE, share read/write/delete, OPEN_EXISTING,
            # OPEN_REPARSE_POINT: open the object, never a final link target.
            handle = create(str(path), 0x40000000, 7, None, 3, 0x00200000, None)
            if handle == wintypes.HANDLE(-1).value:
                raise OSError('Cannot open mutation shadow for writing')
            try:
                fd = msvcrt.open_osfhandle(handle, os.O_WRONLY | os.O_BINARY | os.O_NOINHERIT)
            except BaseException:
                close(handle)
                raise
        else:
            if not hasattr(os, 'O_NOFOLLOW'):
                _error('Mutation replay requires a no-follow write primitive')
            fd = os.open(path, os.O_WRONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        if (_file_state(os.fstat(fd)) != before
                or _file_state(_safe_file(root, item.record['path'])[1]) != before):
            _error('Mutation replay target changed while being opened')
        os.ftruncate(fd, 0)
        view = memoryview(item.mutated_bytes)
        while view:
            written = os.write(fd, view[:CHUNK_BYTES])
            if written <= 0:
                _error('Mutation replay write did not complete')
            view = view[written:]
        os.fsync(fd)
    except OSError:
        _error('Mutation replay cannot write its shadow reliably')
    finally:
        if fd is not None:
            os.close(fd)
    if (_read_file(root, item.record['path'], content=True) != item.mutated_bytes
            or stat.S_IMODE(_safe_file(root, item.record['path'])[1].st_mode) != item.mode):
        _error('Mutation replay write did not establish the exact mutation')


def _attest(root, before, temporary_root, index, item, stdout_hash, stderr_hash):
    from .dart_test_adapter import _parse_json_reporter_output

    if _inventory(root) != before:
        _error('Canonical project changed before independent replay copying')
    replay = temporary_root / f'replay-{index}'
    replay.mkdir()
    if _inventory(root, replay) != before or _inventory(root) != before:
        _error('Canonical project changed during independent replay copying')
    _write_reconstructed(replay, item)
    _assert_targets(replay, before, changed=item)
    _analyze(replay, stdout_hash, stderr_hash)
    _assert_targets(replay, before, changed=item)
    timeout_status = item.record['status'] == 'Timeout'
    completed = _execute(['dart', 'test', '--reporter=json'], replay, BASELINE_TIMEOUT_SECONDS,
                         stdout_hash, stderr_hash, 'Dart mutation independent replay',
                         allowed_exits=(0, 1), repeated_timeout=timeout_status)
    _assert_targets(replay, before, changed=item)
    if _inventory(root) != before:
        _error('Canonical project changed during independent replay')
    if completed is None:
        return  # Only the explicit repeated Timeout contract reaches here.
    try:
        for line in completed.stdout.splitlines():
            if not line.strip():
                continue
            event = json.loads(line, object_pairs_hook=_pairs, parse_float=_float, parse_constant=_constant)
            if isinstance(event, dict) and event.get('type') == 'suite':
                suite = event.get('suite')
                if not isinstance(suite, dict):
                    _error('Mutation replay suite is malformed')
                _protocol_file(replay, suite.get('path'))
        parsed = _parse_json_reporter_output(replay, completed.stdout)
        if completed.returncode != (1 if parsed.verification_status == 'FAIL' else 0):
            _error('Mutation replay exit disagrees with its structured protocol')
        if timeout_status or parsed.verification_status != 'FAIL':
            _error('Mutation engine status contradicts independent behavioral replay')
    except Exception:
        _error('Mutation behavioral replay did not establish the required structured outcome')


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
    semantic = json.dumps({'schema_version': 2, 'attestation': 'independent_replay_v1',
                           'mutants': list(mutants)}, sort_keys=True,
                          separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode('utf-8')
    base = VerificationAdapterResult(
        'dart.mutation.strict', '2', 'dart_mutant', tool_version, mode,
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
        # Establish validity of the fresh canonical-derived snapshot before
        # project tests can alter retained non-target analysis inputs.
        _analyze(shadow, stdout_hash, stderr_hash)
        _assert_targets(shadow, before)
        _execute(['dart', 'test', '--reporter=compact'], shadow, BASELINE_TIMEOUT_SECONDS,
                 stdout_hash, stderr_hash, 'Dart mutation green baseline')
        _assert_targets(shadow, before)
        _execute(['dart_mutant', '--path', '.', '--parallel', '1', '--timeout', str(BASELINE_TIMEOUT_SECONDS),
                  '--threshold', '0', '--quiet', '--json', '--ai', 'none', '--output', str(report_root)],
                 shadow, MUTATION_TIMEOUT_SECONDS, stdout_hash, stderr_hash, 'Dart mutation engine')
        _assert_targets(shadow, before)  # Engine restoration precedes report trust.
        trusted_sources = {relative: (mode, digest) for relative, mode, digest in before[1] if relative in targets}
        mutants = _parse_report(report_root, targets, shadow, trusted_sources)
        _assert_targets(shadow, before)
        for index, item in enumerate(mutants):
            if item.record['status'] in {'Killed', 'Timeout'}:
                _attest(root, before, temporary_root, index, item, stdout_hash, stderr_hash)
        if _inventory(root) != before:
            _error('Canonical project inventory or bytes changed during mutation verification')
        return _result(evaluation_mode, tuple(item.record for item in mutants), tool_version=ENGINE_VERSION,
                       stdout_hash=stdout_hash, stderr_hash=stderr_hash)
