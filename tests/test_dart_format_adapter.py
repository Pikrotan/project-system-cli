import hashlib
import importlib
import json
import os
from dataclasses import replace
from pathlib import Path
import subprocess

import pytest

from project_system import dart_analyze_adapter, verification_adapters as adapters
from project_system.process_runner import ProcessOutputLimitExceeded


EXCLUDED = (
    '.git', '.generated', '.dart_tool', '.pub-cache', 'build', 'node_modules',
    '.venv', 'venv', '__pycache__', 'vendor', 'dist', '.pytest_cache',
    '.mypy_cache', '.ruff_cache', '.cache', '.tox', '.nox', 'coverage',
)


def module():
    return importlib.import_module('project_system.dart_format_adapter')


def write(root, relative='lib/main.dart', text='void main() {}\n'):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')
    return path


def fake_process(monkeypatch, root, *, dirty=(), stdout='', stderr='', version='3.13.1', hook=None):
    calls = []

    def run(argv, **kwargs):
        calls.append((tuple(argv), kwargs))
        if argv == ['dart', '--version']:
            return subprocess.CompletedProcess(argv, 0, '', f'Dart SDK version: {version} (stable)\n')
        assert argv[:4] == ['dart', 'format', '--output=none', '--set-exit-if-changed']
        assert len(argv) == 5
        relative = Path(argv[4]).relative_to(root.resolve()).as_posix()
        assert kwargs['shell'] is False
        if hook:
            hook(relative)
        return subprocess.CompletedProcess(argv, int(relative in dirty), stdout, stderr)

    monkeypatch.setattr(module(), 'run_process', run)
    monkeypatch.setattr(dart_analyze_adapter, 'run_process', run)
    return calls


def evaluate(root, paths=None):
    module()  # Missing adapter must not masquerade as an expected execution ERROR.
    return adapters.evaluate_verification_adapter('dart.format', root, evaluation_paths=paths)


def test_registry_contract():
    spec = adapters.VERIFICATION_ADAPTER_REGISTRY['dart.format']
    assert (spec.adapter_id, spec.version) == ('dart.format', '1')
    assert spec.implementation is module().run_dart_format
    assert spec.global_input_patterns == ('**',)
    assert spec.executes_project_code is spec.uses_network is False
    assert spec.uses_semantic_hash is True


@pytest.mark.parametrize('paths,mode', [
    (None, 'project_wide'), (('README.md',), 'project_wide_invalidation'),
    (('lib/main.dart',), 'project_wide_invalidation'), ((), 'bounded'),
])
def test_applicability_resolution(paths, mode):
    assert adapters.resolve_verification_adapter_evaluation('dart.format', paths) == (None if paths else paths, mode)


@pytest.mark.parametrize('paths', [(), None, ('README.md',)])
def test_not_applicable_does_not_execute(tmp_path, monkeypatch, paths):
    if paths == ():
        write(tmp_path)
    calls = fake_process(monkeypatch, tmp_path)
    result = evaluate(tmp_path, paths)
    assert result.verification_status == 'NOT_APPLICABLE'
    assert result.tool_version == 'not_executed'
    assert result.findings == ()
    assert result.semantic_sha256
    assert not calls


@pytest.mark.parametrize('directory', EXCLUDED)
def test_excluded_subtrees(tmp_path, monkeypatch, directory):
    write(tmp_path, directory + '/nested/dirty.dart')
    calls = fake_process(monkeypatch, tmp_path)
    assert evaluate(tmp_path).verification_status == 'NOT_APPLICABLE'
    assert not calls


def test_discovery_checks_project_owned_generated_suffixes(tmp_path, monkeypatch):
    expected = ('generated.dart', 'lib/model.freezed.dart', 'lib/model.g.dart', 'test/test.dart')
    for path in reversed(expected):
        write(tmp_path, path)
    calls = fake_process(monkeypatch, tmp_path)
    assert module().discover_dart_sources(tmp_path) == expected
    assert evaluate(tmp_path).verification_status == 'PASS'
    assert [Path(argv[-1]).relative_to(tmp_path).as_posix() for argv, _ in calls[1:]] == list(expected)


@pytest.mark.parametrize('dirty', [(), ('lib/a.dart',), ('lib/a.dart', 'lib/z.dart')])
def test_complete_exit_semantics_and_exact_findings(tmp_path, monkeypatch, dirty):
    for path in ('lib/z.dart', 'lib/a.dart', 'test/a.dart'):
        write(tmp_path, path)
    before = {path: (tmp_path / path).read_bytes() for path in module().discover_dart_sources(tmp_path)}
    calls = fake_process(monkeypatch, tmp_path, dirty=dirty, stdout='unstable human output')
    result = evaluate(tmp_path, ('docs/01_VISION.md',))
    assert result.verification_status == ('FAIL' if dirty else 'PASS')
    assert result.exit_code == int(bool(dirty))
    assert result.evaluation_mode == 'project_wide_invalidation'
    assert result.inspected_paths == ()
    assert result.findings == tuple(adapters.VerificationFinding(
        path, None, None, 'ERROR', 'dart.format.required', 'Dart source is not formatter-clean',
    ) for path in sorted(dirty))
    assert len(calls) == 4
    assert before == {path: (tmp_path / path).read_bytes() for path in before}
    for argv, policy in calls[1:]:
        assert argv == ('dart', 'format', '--output=none', '--set-exit-if-changed', argv[-1])
        assert Path(argv[-1]).is_absolute() and Path(argv[-1]).is_file()
        assert policy == dict(cwd=tmp_path.resolve(), capture_output=True, text=True,
                              encoding='utf-8', check=False, shell=False,
                              timeout=120, max_capture_bytes=16 * 1024 * 1024)


@pytest.mark.parametrize('failure', [
    'missing', 'version_exit', 'bad_version', 'timeout', 'process', 'capture', 'unsupported', 'nontext',
])
def test_execution_errors_fail_closed_and_sanitized(tmp_path, monkeypatch, failure):
    write(tmp_path)
    secret = 'SECRET raw private transport detail'
    adapter = module()

    def run(argv, **kwargs):
        if failure == 'missing':
            raise FileNotFoundError(secret)
        if argv == ['dart', '--version']:
            return subprocess.CompletedProcess(argv, 1 if failure == 'version_exit' else 0,
                                               secret if failure == 'bad_version' else 'Dart SDK version: 3.13.1', '')
        if failure == 'timeout':
            raise subprocess.TimeoutExpired(argv, 120, output=secret)
        if failure == 'process':
            raise OSError(secret)
        if failure == 'capture':
            raise ProcessOutputLimitExceeded('stdout', 16 * 1024 * 1024)
        return subprocess.CompletedProcess(argv, 65 if failure == 'unsupported' else 0,
                                           b'bytes' if failure == 'nontext' else secret, '')

    monkeypatch.setattr(adapter, 'run_process', run)
    monkeypatch.setattr(dart_analyze_adapter, 'run_process', run)
    with pytest.raises(adapters.VerificationAdapterError) as caught:
        evaluate(tmp_path)
    assert secret not in str(caught.value)


@pytest.mark.parametrize('drift', ['added', 'removed', 'content', 'restored_mtime'])
def test_source_drift_is_error(tmp_path, monkeypatch, drift):
    path = write(tmp_path)
    before = path.stat()

    def change(_relative):
        if drift == 'added':
            write(tmp_path, 'new.dart')
        elif drift == 'removed':
            path.unlink()
        else:
            path.write_bytes(path.read_bytes().replace(b'main', b'MAIN'))
            if drift == 'restored_mtime':
                os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))

    fake_process(monkeypatch, tmp_path, hook=change)
    with pytest.raises(adapters.VerificationAdapterError):
        evaluate(tmp_path)


@pytest.mark.parametrize('kind', ['file', 'directory', 'root'])
def test_real_symlink_is_rejected(tmp_path, monkeypatch, kind):
    adapter = module()
    root = tmp_path / 'project'
    root.mkdir()
    target = write(tmp_path, 'outside.dart')
    link = root / ('lib' if kind == 'directory' else 'linked.dart')
    try:
        if kind == 'root':
            link = tmp_path / 'alias'
            link.symlink_to(root, target_is_directory=True)
            root = link
        else:
            link.symlink_to(tmp_path if kind == 'directory' else target, target_is_directory=kind == 'directory')
    except OSError:
        pytest.skip('host cannot create symlinks')
    with pytest.raises(adapters.VerificationAdapterError):
        adapter.discover_dart_sources(root)


def test_observed_reparse_point_rejected(tmp_path, monkeypatch):
    from types import SimpleNamespace

    path = write(tmp_path)
    adapter = module()
    real_lstat = adapter.os.lstat

    def reparse(value, *args, **kwargs):
        info = real_lstat(value, *args, **kwargs)
        if Path(value) == path:
            return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400)
        return info

    monkeypatch.setattr(adapter.os, 'lstat', reparse)
    with pytest.raises(adapters.VerificationAdapterError):
        adapter.discover_dart_sources(tmp_path)


def test_unreadable_nonexcluded_directory_rejected(tmp_path, monkeypatch):
    adapter = module()
    real_scan = adapter.os.scandir
    (tmp_path / 'source').mkdir()

    def unreadable(value):
        if Path(value).name == 'source':
            raise PermissionError('private filesystem detail')
        return real_scan(value)

    monkeypatch.setattr(adapter.os, 'scandir', unreadable)
    with pytest.raises(adapters.VerificationAdapterError):
        adapter.discover_dart_sources(tmp_path)


def test_filename_cannot_inject_flags(tmp_path, monkeypatch):
    relative = 'lib/--output=write; dart fix.dart'
    write(tmp_path, relative)
    calls = fake_process(monkeypatch, tmp_path)
    assert evaluate(tmp_path).verification_status == 'PASS'
    assert calls[-1][0][-1] == str(tmp_path / relative)
    assert calls[-1][0][2] == '--output=none'


def test_semantic_hash_exact_contract_and_raw_volatility(tmp_path, monkeypatch):
    for name in ('a.dart', 'b.dart'):
        write(tmp_path, name)
    fake_process(monkeypatch, tmp_path, dirty=('a.dart',), stdout='first transcript')
    first = evaluate(tmp_path)
    fake_process(monkeypatch, tmp_path, dirty=('a.dart',), stdout='different\noutput', stderr='noise')
    second = evaluate(tmp_path)
    expected = {'schema_version': 1, 'source_paths': ['a.dart', 'b.dart'], 'unformatted_paths': ['a.dart']}
    digest = hashlib.sha256(json.dumps(expected, sort_keys=True, separators=(',', ':'),
                                      ensure_ascii=False, allow_nan=False).encode()).hexdigest()
    assert first.semantic_sha256 == second.semantic_sha256 == digest
    assert first.result_sha256 == second.result_sha256
    assert first.stdout_sha256 != second.stdout_sha256
    assert first.stderr_sha256 != second.stderr_sha256
    fake_process(monkeypatch, tmp_path, dirty=('b.dart',))
    changed = evaluate(tmp_path)
    assert changed.semantic_sha256 != first.semantic_sha256
    assert changed.result_sha256 != first.result_sha256
    fake_process(monkeypatch, tmp_path)
    assert evaluate(tmp_path).semantic_sha256 != first.semantic_sha256


def test_source_bytes_are_runtime_state_not_semantics(tmp_path, monkeypatch):
    path = write(tmp_path)
    fake_process(monkeypatch, tmp_path)
    first = evaluate(tmp_path)
    path.write_text('void other() {}\n', encoding='utf-8')
    second = evaluate(tmp_path)
    assert second.semantic_sha256 == first.semantic_sha256
    assert second.result_sha256 == first.result_sha256
    write(tmp_path, 'lib/added.dart')
    assert evaluate(tmp_path).semantic_sha256 != first.semantic_sha256


@pytest.mark.parametrize('field,value', [('semantic_sha256', None), ('semantic_sha256', 'bad'),
                                       ('stdout_sha256', 'bad'), ('result_sha256', '0' * 64)])
def test_tampered_result_rejected(tmp_path, monkeypatch, field, value):
    write(tmp_path)
    fake_process(monkeypatch, tmp_path)
    result = replace(evaluate(tmp_path), **{field: value})
    with pytest.raises(adapters.VerificationAdapterError):
        adapters.validate_verification_adapter_result(result, expected_adapter_id='dart.format')


def test_rule_evidence_uses_semantics_not_runtime_hashes(tmp_path, monkeypatch):
    from project_system.rule_engine import RuleEvaluationContext, evaluate_rules
    from project_system.rule_evidence import build_rule_evidence, rule_evidence_to_dict

    source = write(tmp_path)
    fake_process(monkeypatch, tmp_path, dirty=('lib/main.dart',), stdout='RAW SECRET', stderr='RAW STDERR')
    registry = {'schema_version': 1, 'profile': 'project-system-rules-v1', 'rules': {'QUALITY-001': {
        'title': 'Formatting', 'status': 'active', 'category': 'testing', 'description': 'Check formatting.',
        'verification': {'method': 'deterministic', 'checker': 'code.verification',
                         'parameters': {'adapter': 'dart.format'}},
        'enforcement': {'severity': 'BLOCKING', 'checkpoints': ['sync_verify']},
        'exception_policy': 'forbidden',
    }}}
    ctx = RuleEvaluationContext(tmp_path, 'sync_verify', {}, True, evaluation_paths=('README.md',))
    raw = evaluate_rules(registry, ctx)
    evidence = build_rule_evidence(project_id='demo', git_head='1' * 40, cli_version='test',
                                   rules_registry=registry, context=ctx, results=raw,
                                   exception_registry={'schema_version': 1, 'profile': 'project-system-rule-exceptions-v1', 'exceptions': {}})
    payload = rule_evidence_to_dict(evidence)
    details = payload['results'][0]['details']
    assert payload['schema_version'] == 1
    assert raw[0].raw_status == 'FAIL'
    assert details['adapter_id'] == 'dart.format'
    assert 'semantic_sha256' in details
    assert 'stdout_sha256' not in details and 'stderr_sha256' not in details
    serialized = json.dumps(payload)
    assert hashlib.sha256(source.read_bytes()).hexdigest() not in serialized
    assert 'RAW SECRET' not in serialized and 'RAW STDERR' not in serialized
    assert str(tmp_path) not in serialized
    assert 'uses_network' not in serialized


def test_project_controlled_parameters_rejected():
    from project_system.rule_checkers import checker_contract_messages

    module()
    messages = checker_contract_messages('code.verification', {'adapter': 'dart.format', 'args': ['--output=write']})
    assert messages == ('unknown parameter: args',)


def test_hashing_uses_bounded_stream_reads(tmp_path, monkeypatch):
    adapter = module()
    path = write(tmp_path)
    path.write_bytes(b' ' * (3 * adapter.HASH_CHUNK_BYTES + 17))
    fake_process(monkeypatch, tmp_path)
    actual_open = adapter._open_source
    reads = []

    class ObservedStream:
        def __init__(self, path):
            self.stream = actual_open(path)

        def __enter__(self):
            self.stream.__enter__()
            return self

        def __exit__(self, *args):
            return self.stream.__exit__(*args)

        def fileno(self):
            return self.stream.fileno()

        def read(self, size):
            assert size == adapter.HASH_CHUNK_BYTES
            reads.append(size)
            return self.stream.read(size)

    monkeypatch.setattr(adapter, '_open_source', ObservedStream)
    assert evaluate(tmp_path).verification_status == 'PASS'
    assert len(reads) >= 10  # More than three chunks plus final EOF, at both F0/F1.


def test_unreadable_source_fails_before_tool_execution(tmp_path, monkeypatch):
    write(tmp_path)
    calls = fake_process(monkeypatch, tmp_path)

    def denied(_path):
        raise PermissionError('SECRET inaccessible source')

    monkeypatch.setattr(module(), '_open_source', denied)
    with pytest.raises(adapters.VerificationAdapterError) as caught:
        evaluate(tmp_path)
    assert not calls
    assert 'SECRET' not in str(caught.value)


def test_source_drift_during_version_probe_is_error(tmp_path, monkeypatch):
    path = write(tmp_path)
    fake_process(monkeypatch, tmp_path)

    def version(argv, **kwargs):
        assert argv == ['dart', '--version']
        path.write_bytes(path.read_bytes().replace(b'main', b'MAIN'))
        return subprocess.CompletedProcess(argv, 0, 'Dart SDK version: 3.13.1', '')

    monkeypatch.setattr(dart_analyze_adapter, 'run_process', version)
    with pytest.raises(adapters.VerificationAdapterError):
        evaluate(tmp_path)


@pytest.mark.parametrize('exit_code', [-1, 2, 65, True])
def test_rule_maps_unsupported_formatter_exit_to_error(tmp_path, monkeypatch, exit_code):
    from project_system.rule_checkers import evaluate_checker
    from project_system.rule_engine import RuleEvaluationContext

    write(tmp_path)
    fake_process(monkeypatch, tmp_path)
    actual_run = module().run_process

    def run(argv, **kwargs):
        result = actual_run(argv, **kwargs)
        return subprocess.CompletedProcess(argv, exit_code, result.stdout, result.stderr)

    monkeypatch.setattr(module(), 'run_process', run)
    result = evaluate_checker('code.verification',
                              RuleEvaluationContext(tmp_path, 'sync_verify', {}, True),
                              {'adapter': 'dart.format'})
    assert result.raw_status == 'ERROR'
    assert result.details == {'adapter_id': 'dart.format', 'adapter_version': '1'}


def test_infrastructure_error_overrides_prior_formatting_failure(tmp_path, monkeypatch):
    write(tmp_path, 'a.dart')
    write(tmp_path, 'b.dart')
    fake_process(monkeypatch, tmp_path, dirty=('a.dart',))
    actual_run = module().run_process

    def run(argv, **kwargs):
        result = actual_run(argv, **kwargs)
        return subprocess.CompletedProcess(argv, 65 if argv[-1].endswith('b.dart') else result.returncode,
                                           result.stdout, result.stderr)

    monkeypatch.setattr(module(), 'run_process', run)
    with pytest.raises(adapters.VerificationAdapterError):
        evaluate(tmp_path)


def test_tampered_adapter_result_maps_to_rule_error(tmp_path, monkeypatch):
    from project_system.rule_checkers import evaluate_checker
    from project_system.rule_engine import RuleEvaluationContext

    write(tmp_path)
    fake_process(monkeypatch, tmp_path)
    malformed = replace(evaluate(tmp_path), result_sha256='0' * 64)
    registry = dict(adapters.VERIFICATION_ADAPTER_REGISTRY)
    registry['dart.format'] = replace(registry['dart.format'], implementation=lambda *args: malformed)
    monkeypatch.setattr(adapters, 'VERIFICATION_ADAPTER_REGISTRY', registry)
    result = evaluate_checker('code.verification',
                              RuleEvaluationContext(tmp_path, 'sync_verify', {}, True),
                              {'adapter': 'dart.format'})
    assert result.raw_status == 'ERROR'
