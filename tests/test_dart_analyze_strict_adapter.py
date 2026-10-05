from dataclasses import asdict, replace
import hashlib
import json
import subprocess

import pytest

from project_system import dart_analyze_adapter, verification_adapters as adapters
from project_system.process_runner import ProcessOutputLimitExceeded


STRICT_ARGV = ['dart', 'analyze', '--format=machine', '--no-plugins', '--fatal-infos', '.']


def machine(root, severity='INFO', *, path='lib/main.dart', line=1, column=2,
            code='LINT', message='Use a better name.', diagnostic_type='HINT', length=1):
    def escape(value):
        return str(value).replace('\\', '\\\\').replace('|', '\\|').replace('\n', '\\n').replace('\r', '\\r')

    absolute = root / path
    return '|'.join(escape(value) for value in (
        severity, diagnostic_type, code, absolute, line, column, length, message,
    )) + '\n'


def fake_process(monkeypatch, *, stdout='', stderr='', exit_code=0, version='3.13.1'):
    calls = []

    def run(argv, **kwargs):
        calls.append((list(argv), kwargs))
        if argv == ['dart', '--version']:
            return subprocess.CompletedProcess(argv, 0, '', f'Dart SDK version: {version} (stable)')
        assert argv == STRICT_ARGV
        return subprocess.CompletedProcess(argv, exit_code, stdout, stderr)

    monkeypatch.setattr(dart_analyze_adapter, 'run_process', run)
    return calls


def evaluate(root, paths=None):
    # Unknown adapter must never masquerade as an expected execution ERROR.
    assert 'dart.analyze.strict' in adapters.VERIFICATION_ADAPTER_REGISTRY
    return adapters.evaluate_verification_adapter('dart.analyze.strict', root, evaluation_paths=paths)


def rule_registry():
    return {'schema_version': 1, 'profile': 'project-system-rules-v1', 'rules': {'QUALITY-001': {
        'title': 'Strict analysis', 'status': 'active', 'category': 'testing',
        'description': 'Check all analyzer diagnostics.',
        'verification': {'method': 'deterministic', 'checker': 'code.verification',
                         'parameters': {'adapter': 'dart.analyze.strict'}},
        'enforcement': {'severity': 'WARNING', 'checkpoints': ['sync_verify']},
        'exception_policy': 'forbidden',
    }}}


def evidence(root):
    from project_system.rule_engine import RuleEvaluationContext, evaluate_rules
    from project_system.rule_evidence import build_rule_evidence, rule_evidence_to_dict

    registry = rule_registry()
    context = RuleEvaluationContext(root, 'sync_verify', {}, True, evaluation_paths=('README.md',))
    results = evaluate_rules(registry, context)
    return rule_evidence_to_dict(build_rule_evidence(
        project_id='demo', git_head='1' * 40, cli_version='test', rules_registry=registry,
        context=context, results=results,
        exception_registry={'schema_version': 1, 'profile': 'project-system-rule-exceptions-v1', 'exceptions': {}},
    ))


def test_strict_registry_contract():
    spec = adapters.VERIFICATION_ADAPTER_REGISTRY['dart.analyze.strict']
    assert (spec.adapter_id, spec.version) == ('dart.analyze.strict', '1')
    assert spec.implementation is dart_analyze_adapter.run_dart_analyze_strict
    assert spec.global_input_patterns == ('**',)
    assert spec.executes_project_code is spec.uses_network is False
    assert spec.uses_semantic_hash is True


@pytest.mark.parametrize('paths,mode', [
    (None, 'project_wide'), ((), 'bounded'),
    (('README.md',), 'project_wide_invalidation'),
    (('lib/main.dart',), 'project_wide_invalidation'),
    (('config/team_options.yaml', 'docs/01_VISION.md'), 'project_wide_invalidation'),
])
def test_strict_applicability(paths, mode):
    assert adapters.resolve_verification_adapter_evaluation('dart.analyze.strict', paths) == (
        paths if mode == 'bounded' else None, mode,
    )


def test_strict_empty_bounded_does_not_probe_or_analyze(tmp_path, monkeypatch):
    calls = fake_process(monkeypatch)
    result = evaluate(tmp_path, ())
    assert not calls
    assert result.verification_status == 'NOT_APPLICABLE'
    assert result.tool_version == 'not_executed'
    assert result.evaluation_mode == 'bounded'
    assert result.findings == result.inspected_paths == ()
    assert result.exit_code == 0
    assert result.semantic_sha256


@pytest.mark.parametrize('severities,exit_code', [
    ((), 0), (('INFO',), 1), (('WARNING',), 2), (('ERROR',), 3),
    (('INFO', 'WARNING'), 2), (('INFO', 'ERROR'), 3),
    (('WARNING', 'ERROR'), 3), (('INFO', 'WARNING', 'ERROR'), 3),
])
def test_strict_fixed_execution_and_consistent_exits(tmp_path, monkeypatch, severities, exit_code):
    stdout = ''.join(machine(tmp_path, severity, line=index + 1) for index, severity in enumerate(severities))
    calls = fake_process(monkeypatch, stdout=stdout, exit_code=exit_code)
    result = evaluate(tmp_path, ('README.md',))
    assert result.verification_status == ('PASS' if not severities else 'FAIL')
    assert result.exit_code == exit_code
    assert result.tool_version == '3.13.1'
    assert result.evaluation_mode == 'project_wide_invalidation'
    assert result.inspected_paths == ()
    assert tuple(finding.severity for finding in result.findings) == severities
    assert all(finding.path == 'lib/main.dart' for finding in result.findings)
    assert calls == [
        (['dart', '--version'], dict(cwd=tmp_path.resolve(), capture_output=True, text=True,
                                    encoding='utf-8', check=False, timeout=10, max_capture_bytes=64 * 1024)),
        (STRICT_ARGV, dict(cwd=tmp_path.resolve(), capture_output=True, text=True,
                          encoding='utf-8', check=False, shell=False,
                          timeout=120, max_capture_bytes=16 * 1024 * 1024)),
    ]


@pytest.mark.parametrize('severities,exit_code', [
    (severities, exit_code)
    for severities, expected in [
        ((), 0), (('INFO',), 1), (('WARNING',), 2), (('ERROR',), 3),
        (('INFO', 'WARNING'), 2), (('INFO', 'ERROR'), 3),
        (('WARNING', 'ERROR'), 3), (('INFO', 'WARNING', 'ERROR'), 3),
    ]
    for exit_code in range(4) if exit_code != expected
])
def test_strict_all_exit_diagnostic_contradictions_are_error(tmp_path, monkeypatch, severities, exit_code):
    stdout = ''.join(machine(tmp_path, severity, line=index + 1) for index, severity in enumerate(severities))
    calls = fake_process(monkeypatch, stdout=stdout, exit_code=exit_code)
    with pytest.raises(adapters.VerificationAdapterError, match='execution failed'):
        evaluate(tmp_path)
    assert len(calls) == 2
    from project_system.rule_checkers import evaluate_checker
    from project_system.rule_engine import RuleEvaluationContext
    outcome = evaluate_checker('code.verification', RuleEvaluationContext(tmp_path, 'sync_verify', {}, True),
                               {'adapter': 'dart.analyze.strict'})
    assert outcome.raw_status == 'ERROR'  # Infrastructure contradiction must never become FAIL.


@pytest.mark.parametrize('exit_code', [4, 65, -1, True, False, None, '1'])
def test_strict_unsupported_exit_fails_closed(tmp_path, monkeypatch, exit_code):
    calls = fake_process(monkeypatch, exit_code=exit_code)
    with pytest.raises(adapters.VerificationAdapterError):
        evaluate(tmp_path)
    assert len(calls) == 2


@pytest.mark.parametrize('kind', ['fields', 'severity', 'line', 'column', 'length', 'escape', 'empty_type',
                                 'empty_code', 'empty_message', 'relative', 'outside', 'parent_escape'])
def test_strict_malformed_output_and_unsafe_paths_fail_closed(tmp_path, monkeypatch, kind):
    options = {
        'severity': {'severity': 'FATAL'}, 'line': {'line': 0}, 'column': {'column': 'bad'},
        'length': {'length': -1}, 'empty_type': {'diagnostic_type': ''},
        'empty_code': {'code': ''}, 'empty_message': {'message': ''},
        'outside': {'path': str(tmp_path.parent / 'outside.dart')},
        'parent_escape': {'path': '../outside.dart'},
    }
    stdout = machine(tmp_path, **options.get(kind, {}))
    if kind == 'fields':
        stdout = 'not a machine diagnostic\n'
    elif kind == 'escape':
        stdout = stdout.replace('Use a better name.', r'bad\x')
    elif kind == 'relative':
        stdout = 'INFO|HINT|LINT|lib/main.dart|1|2|1|Message\n'
    calls = fake_process(monkeypatch, stdout=stdout, exit_code=1)
    with pytest.raises(adapters.VerificationAdapterError):
        evaluate(tmp_path)
    assert len(calls) == 2


@pytest.mark.parametrize('failure', [
    'missing', 'version_exit', 'bad_version', 'version_nontext', 'version_timeout', 'version_capture',
    'timeout', 'capture', 'exception', 'stdout_nontext', 'stderr_nontext',
])
def test_strict_transport_errors_are_sanitized(tmp_path, monkeypatch, failure):
    calls = []
    secret = 'SECRET private path/token/runtime output'

    def run(argv, **kwargs):
        calls.append(argv)
        if failure == 'missing':
            raise FileNotFoundError(secret)
        version = argv == ['dart', '--version']
        if failure == ('version_timeout' if version else 'timeout'):
            raise subprocess.TimeoutExpired(argv, kwargs['timeout'], output=secret, stderr=secret)
        if failure == ('version_capture' if version else 'capture'):
            raise ProcessOutputLimitExceeded('stdout', kwargs['max_capture_bytes'])
        if version:
            return subprocess.CompletedProcess(argv, 1 if failure == 'version_exit' else 0,
                                               b'bytes' if failure == 'version_nontext' else
                                               secret if failure == 'bad_version' else 'Dart SDK version: 3.13.1', '')
        assert argv == STRICT_ARGV
        if failure == 'exception':
            raise OSError(secret)
        return subprocess.CompletedProcess(argv, 0,
                                           b'bytes' if failure == 'stdout_nontext' else '',
                                           b'bytes' if failure == 'stderr_nontext' else '')

    monkeypatch.setattr(dart_analyze_adapter, 'run_process', run)
    with pytest.raises(adapters.VerificationAdapterError, match='execution failed') as caught:
        evaluate(tmp_path)
    assert calls
    assert secret not in str(caught.value)


def test_strict_semantic_schema_and_evidence_ignore_raw_volatility(tmp_path, monkeypatch):
    first_line = machine(tmp_path, message='Use a|b\\c\nnext\rend')
    second_line = machine(tmp_path, 'WARNING', path='lib/other.dart')
    raw = first_line + second_line + first_line
    fake_process(monkeypatch, stdout=raw, stderr='RAW SECRET', exit_code=2)
    first = evaluate(tmp_path)
    first_evidence = evidence(tmp_path)
    alternate = second_line + machine(tmp_path, message='Use a|b\\c\nnext\rend',
                                      diagnostic_type='LINT', length=500)
    fake_process(monkeypatch, stdout=alternate, stderr='DIFFERENT RAW STDERR', exit_code=2)
    second = evaluate(tmp_path)
    second_evidence = evidence(tmp_path)
    expected = {'schema_version': 1, 'diagnostics': [asdict(item) for item in first.findings]}
    digest = hashlib.sha256(json.dumps(expected, sort_keys=True, separators=(',', ':'),
                                      ensure_ascii=False, allow_nan=False).encode('utf-8')).hexdigest()
    assert len(first.findings) == 2
    assert first.findings[0].message == 'Use a|b\\c\nnext\rend'
    assert first.semantic_sha256 == second.semantic_sha256 == digest
    assert first.result_sha256 == second.result_sha256
    assert first.stdout_sha256 != second.stdout_sha256
    assert first.stderr_sha256 != second.stderr_sha256
    assert first_evidence == second_evidence
    details = first_evidence['results'][0]['details']
    assert details['semantic_sha256'] == digest
    assert details['adapter_id'] == 'dart.analyze.strict'
    assert details['verification_status'] == 'FAIL'
    assert first_evidence['results'][0]['raw_status'] == 'FAIL'
    assert 'stdout_sha256' not in details and 'stderr_sha256' not in details
    serialized = json.dumps(first_evidence)
    assert str(tmp_path) not in serialized
    for forbidden in ('RAW SECRET', 'DIFFERENT RAW STDERR', first.stdout_sha256, first.stderr_sha256):
        assert forbidden not in serialized


@pytest.mark.parametrize('changed', [
    {'path': 'other.dart'}, {'line': 7}, {'column': 8}, {'severity': 'ERROR'},
    {'code': 'OTHER'}, {'message': 'Changed finding'},
])
def test_strict_every_semantic_finding_field_changes_hash(tmp_path, monkeypatch, changed):
    fake_process(monkeypatch, stdout=machine(tmp_path), exit_code=1)
    first = evaluate(tmp_path)
    fake_process(monkeypatch, stdout=machine(tmp_path, **changed),
                 exit_code=3 if changed.get('severity') == 'ERROR' else 1)
    second = evaluate(tmp_path)
    assert first.semantic_sha256 != second.semantic_sha256
    assert first.result_sha256 != second.result_sha256


@pytest.mark.parametrize('field,value', [
    ('semantic_sha256', None), ('semantic_sha256', 'bad'), ('semantic_sha256', 'a' * 64),
    ('stdout_sha256', 'bad'), ('stderr_sha256', 'bad'), ('result_sha256', '0' * 64),
])
def test_strict_tampered_result_hash_rejected(tmp_path, monkeypatch, field, value):
    fake_process(monkeypatch)
    result = replace(evaluate(tmp_path), **{field: value})
    with pytest.raises(adapters.VerificationAdapterError):
        adapters.validate_verification_adapter_result(result, expected_adapter_id='dart.analyze.strict')


def test_strict_tampered_evidence_digest_and_raw_hash_injection_rejected(tmp_path, monkeypatch):
    from project_system.rule_checkers import checker_outcome_contract_messages

    fake_process(monkeypatch)
    evaluate(tmp_path)
    details = evidence(tmp_path)['results'][0]['details']
    for tampered in [
        {**details, 'semantic_sha256': 'a' * 64}, {**details, 'result_sha256': 'a' * 64},
        {**details, 'stdout_sha256': 'b' * 64}, {**details, 'stderr_sha256': 'b' * 64},
        {key: value for key, value in details.items() if key != 'semantic_sha256'},
    ]:
        assert checker_outcome_contract_messages('code.verification', 'PASS', tampered, None,
                                                 {'adapter': 'dart.analyze.strict'})


@pytest.mark.parametrize('parameter', ['args', 'command', 'minimum_severity', 'fatal_infos'])
def test_strict_project_supplied_parameters_rejected(parameter):
    from project_system.rule_checkers import checker_contract_messages

    assert 'dart.analyze.strict' in adapters.VERIFICATION_ADAPTER_REGISTRY
    assert checker_contract_messages('code.verification', {'adapter': 'dart.analyze.strict', parameter: True}) == (
        f'unknown parameter: {parameter}',
    )


def test_legacy_analyze_info_pass_argv_and_raw_hashing_unchanged(tmp_path, monkeypatch):
    spec = adapters.VERIFICATION_ADAPTER_REGISTRY['dart.analyze']
    assert spec.uses_semantic_hash is False
    assert spec.global_input_patterns == ('**/*.dart', '**/*.yaml', '**/pubspec.lock')
    calls = []
    state = {'stderr': ''}
    stdout = machine(tmp_path)

    def run(argv, **kwargs):
        calls.append(argv)
        if argv == ['dart', '--version']:
            return subprocess.CompletedProcess(argv, 0, 'Dart SDK version: 3.13.1', '')
        assert argv == ['dart', 'analyze', '--format=machine', '--no-plugins', '.']
        return subprocess.CompletedProcess(argv, 0, stdout, state['stderr'])

    monkeypatch.setattr(dart_analyze_adapter, 'run_process', run)
    first = adapters.evaluate_verification_adapter('dart.analyze', tmp_path)
    state['stderr'] = 'noise changes legacy raw hash'
    second = adapters.evaluate_verification_adapter('dart.analyze', tmp_path)
    assert first.verification_status == second.verification_status == 'PASS'
    assert first.findings[0].severity == 'INFO'
    assert first.semantic_sha256 is second.semantic_sha256 is None
    assert first.result_sha256 != second.result_sha256
    payload = asdict(first)
    payload.pop('semantic_sha256')
    payload.pop('result_sha256')
    expected = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':'),
                                        ensure_ascii=False).encode('utf-8')).hexdigest()
    assert first.result_sha256 == expected
    assert len(calls) == 4
