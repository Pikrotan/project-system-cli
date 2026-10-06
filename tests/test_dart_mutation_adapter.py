import copy
from dataclasses import replace
import hashlib
import importlib
import json
from pathlib import Path
import stat
import subprocess
from types import SimpleNamespace

import pytest
from test_dart_mutation_attestation import test_stream

from project_system import verification_adapters as adapters
from project_system.process_runner import ProcessOutputLimitExceeded


def module():
    return importlib.import_module('project_system.dart_mutation_adapter')


def write(root, relative='lib/main.dart', data=b'int add(int a, int b) => a + b;\n'):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def report(statuses=('Killed',), path='./lib/main.dart'):
    return {'schemaVersion': '1', 'projectRoot': '/volatile/shadow',
            'thresholds': {'high': 80, 'low': 60}, 'mutationScore': 0,
            'files': {path: {'language': 'dart', 'mutants': [
                {'id': 'fixture', 'description': 'volatile description',
                 'mutatorName': 'Arithmetic', 'replacement': '-', 'status': status,
                 'location': {'start': {'line': 1, 'column': index + 25},
                              'end': {'line': 1, 'column': index + 26}}}
                for index, status in enumerate(statuses)]}}}


def fake(monkeypatch, payload=None, *, hook=None, stdout='', stderr=''):
    adapter = module()
    calls = []
    timeout_mutations = []

    def run(argv, **policy):
        calls.append((tuple(argv), policy))
        if hook:
            selected = hook(argv, policy)
            if selected is not None:
                return selected
        if argv == ['dart_mutant', '--version']:
            return subprocess.CompletedProcess(argv, 0, 'dart_mutant 0.1.0\n', '')
        if argv == ['dart', 'test', '--reporter=compact']:
            return subprocess.CompletedProcess(argv, 0, stdout, stderr)
        if argv == ['dart', 'analyze', '--format=machine', '--no-plugins', '.']:
            return subprocess.CompletedProcess(argv, 0, '', stderr)
        if argv == ['dart', 'test', '--reporter=json']:
            if any((policy['cwd'] / path).read_bytes() == mutated for path, mutated in timeout_mutations):
                raise subprocess.TimeoutExpired(argv, policy['timeout'])
            return subprocess.CompletedProcess(argv, 1, test_stream(policy['cwd']), stderr)
        assert argv[:3] == ['dart_mutant', '--path', '.']
        selected = payload if payload is not None else report()
        if isinstance(selected.get('files'), dict):
            for raw_path, file in selected['files'].items():
                path = raw_path.replace('\\', '/').removeprefix('./')
                if not isinstance(file, dict) or not isinstance(file.get('mutants'), list):
                    continue
                for mutant in file['mutants']:
                    # Fixture IDs are signed from the actual engine baseline,
                    # not assumed arbitrary IDs as in the provisional v1 tests.
                    if not isinstance(mutant, dict) or mutant.get('id') != 'fixture':
                        continue
                    try:
                        source = (policy['cwd'] / path).read_bytes()
                        start, end = mutant['location']['start'], mutant['location']['end']
                        starts = [0] + [i + 1 for i, byte in enumerate(source) if byte == 10]
                        offset = starts[start['line'] - 1] + start['column'] - 1
                        stop = offset + end['column'] - start['column']
                        original = source[offset:stop].decode('utf-8')
                        replacement = mutant['replacement']
                        mutant['id'] = hashlib.md5(f'{raw_path}:{start["line"]}:{original}:{replacement}'.encode()).hexdigest()
                        if mutant['status'] == 'Timeout':
                            timeout_mutations.append((path, source[:offset] + replacement.encode() + source[stop:]))
                    except (OSError, KeyError, TypeError, IndexError, UnicodeError):
                        pass  # Deliberately malformed report remains malformed.
        output = Path(argv[-1]) / 'mutation-report.json'
        output.write_text(json.dumps(selected), encoding='utf-8')
        return subprocess.CompletedProcess(argv, 0, stdout, stderr)

    monkeypatch.setattr(adapter, 'run_process', run)
    return calls


def evaluate(root, paths=None):
    module()  # Missing implementation is RED, not a false expected ERROR.
    if not (root / 'test/example_test.dart').exists():
        write(root, 'test/example_test.dart')
    return adapters.evaluate_verification_adapter('dart.mutation.strict', root, evaluation_paths=paths)


def test_registry_and_frozen_old_contracts():
    spec = adapters.VERIFICATION_ADAPTER_REGISTRY['dart.mutation.strict']
    assert spec.implementation is module().run_dart_mutation
    assert (spec.adapter_id, spec.version, spec.global_input_patterns) == ('dart.mutation.strict', '2', ('**',))
    assert spec.executes_project_code and spec.uses_semantic_hash and not spec.uses_network
    expected = {'dart.analyze': (False, False, False, ('**/*.dart', '**/*.yaml', '**/pubspec.lock')),
                'dart.analyze.strict': (False, True, False, ('**',)),
                'dart.test': (True, True, False, ('**',)),
                'dart.format': (False, True, False, ('**',)),
                'osv.scan': (False, True, True, ('**',))}
    for name, contract in expected.items():
        old = adapters.VERIFICATION_ADAPTER_REGISTRY[name]
        assert old.version == '1'
        assert (old.executes_project_code, old.uses_semantic_hash, old.uses_network, old.global_input_patterns) == contract


@pytest.mark.parametrize('paths,mode', [((), 'bounded'), (None, 'project_wide'),
                                      (('README.md',), 'project_wide_invalidation'),
                                      (('test/a_test.dart',), 'project_wide_invalidation')])
def test_applicability(paths, mode):
    module()
    assert adapters.resolve_verification_adapter_evaluation('dart.mutation.strict', paths) == (None if paths else paths, mode)


@pytest.mark.parametrize('paths,source', [((), 'lib/main.dart'), (None, None),
                                       (None, 'test/main.dart'), (None, 'foo_test.dart'),
                                       (None, 'lib/generated/a.dart'), (None, 'lib/a.g.dart'),
                                       (None, 'lib/a.freezed.dart'), (None, 'lib/a.mocks.dart')])
def test_no_target_no_process(tmp_path, monkeypatch, paths, source):
    if source:
        write(tmp_path, source)
    calls = fake(monkeypatch)
    result = evaluate(tmp_path, paths)
    assert result.verification_status == 'NOT_APPLICABLE'
    assert result.tool_version == 'not_executed' and result.findings == ()
    assert not calls


@pytest.mark.parametrize('source', ['lib/main.dart', 'bin/main.dart', 'tool/main.dart',
                                  'example/main.dart', '.dart_tool/main.dart', 'main.dart'])
def test_actual_non_lib_domain(tmp_path, monkeypatch, source):
    write(tmp_path, source)
    fake(monkeypatch, report(path='./' + source))
    assert evaluate(tmp_path).verification_status == 'PASS'


def test_exact_order_bounds_shadow_retention_and_cleanup(tmp_path, monkeypatch):
    write(tmp_path)
    write(tmp_path, 'test/example_test.dart', b'fixture\x00bytes')
    retained = ('test/a_test.dart', '.dart_tool/package_config.json', 'pubspec.yaml', 'fixtures/input.bin')
    excluded = ('.git', '.generated', 'build', 'node_modules', '__pycache__', '.pytest_cache',
                '.mypy_cache', '.ruff_cache', '.cache', '.tox', '.nox', 'coverage', 'mutation-reports')
    for relative in retained:
        write(tmp_path, relative, b'fixture\x00bytes')
    for directory in excluded:
        write(tmp_path, directory + '/output.dart')
    before = {p.relative_to(tmp_path).as_posix(): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    roots = []

    def hook(argv, policy):
        if argv == ['dart_mutant', '--version']:
            return
        shadow = policy['cwd']
        assert shadow != tmp_path.resolve() and tmp_path not in shadow.parents
        roots.append(shadow.parent)
        for relative in retained:
            assert (shadow / relative).read_bytes() == b'fixture\x00bytes'
        for directory in excluded:
            assert not (shadow / directory).exists()

    calls = fake(monkeypatch, hook=hook)
    result = evaluate(tmp_path, ('README.md',))
    assert result.verification_status == 'PASS' and result.evaluation_mode == 'project_wide_invalidation'
    assert [argv for argv, _ in calls] == [
        ('dart_mutant', '--version'),
        ('dart', 'analyze', '--format=machine', '--no-plugins', '.'),
        ('dart', 'test', '--reporter=compact'),
        ('dart_mutant', '--path', '.', '--parallel', '1', '--timeout', '300',
         '--threshold', '0', '--quiet', '--json', '--ai', 'none', '--output', calls[3][0][-1]),
        ('dart', 'analyze', '--format=machine', '--no-plugins', '.'),
        ('dart', 'test', '--reporter=json'),
    ]
    assert calls[3][0] == ('dart_mutant', '--path', '.', '--parallel', '1', '--timeout', '300',
                          '--threshold', '0', '--quiet', '--json', '--ai', 'none', '--output', calls[3][0][-1])
    for index, (_, policy) in enumerate(calls):
        assert policy == dict(cwd=policy['cwd'], capture_output=True, text=True, encoding='utf-8',
                              check=False, shell=False, timeout=(30, 120, 300, 7200, 120, 300)[index],
                              max_capture_bytes=16 * 1024 * 1024)
    assert all(not root.exists() for root in roots)
    assert before == {p.relative_to(tmp_path).as_posix(): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}


@pytest.mark.parametrize('stage', ['version', 'baseline', 'mutation'])
@pytest.mark.parametrize('failure', ['exit1', 'negative', 'bool', 'bytes', 'missing', 'timeout', 'capture', 'malformed'])
def test_process_failure_is_sanitized_error_and_cleanup(tmp_path, monkeypatch, stage, failure):
    module()
    write(tmp_path)
    roots, reached = [], []
    secret = 'SECRET PRIVATE TRANSCRIPT'

    def hook(argv, policy):
        current = ('version' if argv[-1] == '--version' else
                   'baseline' if argv[-1] == '--reporter=compact' else
                   'analysis' if argv[0] == 'dart' else 'mutation')
        reached.append(current)
        if current != 'version':
            roots.append(policy['cwd'].parent)
        if current != stage:
            return
        if failure == 'missing':
            raise FileNotFoundError(secret)
        if failure == 'timeout':
            raise subprocess.TimeoutExpired(argv, 1, output=secret)
        if failure == 'capture':
            raise ProcessOutputLimitExceeded('stdout', 1)
        if failure == 'malformed':
            return object()
        return subprocess.CompletedProcess(argv, {'exit1': 1, 'negative': -1, 'bool': True}.get(failure, 0),
                                           b'bytes' if failure == 'bytes' else secret, '')

    fake(monkeypatch, hook=hook)
    with pytest.raises(adapters.VerificationAdapterError) as caught:
        evaluate(tmp_path)
    assert secret not in str(caught.value)
    assert all(not root.exists() for root in roots)
    if stage == 'baseline':
        assert reached == ['version', 'analysis', 'baseline']


@pytest.mark.parametrize('version', ['dart_mutant 0.1.1', 'dart_mutant 0.1.0 junk',
                                   '0.1.0', 'dart_mutant 0.1.0\nSECRET', ''])
def test_exact_version_rejection(tmp_path, monkeypatch, version):
    module()
    write(tmp_path)
    calls = fake(monkeypatch, hook=lambda argv, _: subprocess.CompletedProcess(argv, 0, version, '')
                 if argv[-1] == '--version' else None)
    with pytest.raises(adapters.VerificationAdapterError):
        evaluate(tmp_path)
    assert len(calls) == 1


@pytest.mark.parametrize('statuses,verdict', [(('Killed',), 'PASS'), (('Killed', 'Timeout'), 'PASS'),
                                           (('Survived',), 'FAIL'), (('NoCoverage',), 'FAIL'),
                                           (('Survived', 'NoCoverage'), 'FAIL')])
def test_statuses_and_complete_fixed_findings(tmp_path, monkeypatch, statuses, verdict):
    write(tmp_path)
    write(tmp_path, 'test/example_test.dart')
    fake(monkeypatch, report(statuses), stdout='SECRET', stderr='SECRET')
    result = evaluate(tmp_path)
    assert result.verification_status == verdict and result.exit_code == 0
    assert [f.code for f in result.findings] == ['dart.mutation.' + ('survived' if s == 'Survived' else 'no_coverage')
                                               for s in statuses if s in ('Survived', 'NoCoverage')]
    assert all(f.severity == 'ERROR' and f.path == 'lib/main.dart' and f.line == 1 for f in result.findings)
    assert 'SECRET' not in json.dumps([f.__dict__ for f in result.findings])
    assert all(f.message in {'Mutation survived the Dart test suite (Arithmetic)',
                            'Mutation has no test coverage (Arithmetic)'} for f in result.findings)


@pytest.mark.parametrize('bad', ['CompileError', 'RuntimeError', 'Pending', 'Ignored', 'unknown', None, 1])
@pytest.mark.parametrize('prefix', [(), ('Survived',), ('Killed',)])
def test_infrastructure_status_overrides_quality_fail(tmp_path, monkeypatch, bad, prefix):
    module()
    write(tmp_path)
    fake(monkeypatch, report(prefix + (bad,)))
    with pytest.raises(adapters.VerificationAdapterError):
        evaluate(tmp_path)


@pytest.mark.parametrize('path', ['/outside.dart', '../outside.dart', 'lib/../main.dart',
                                'C:\\outside.dart', '//server/a.dart', 'test/main.dart',
                                'lib/a.g.dart', 'lib/missing.dart', 'lib//main.dart', 'lib/main.dart:stream'])
def test_invalid_or_non_target_report_paths(tmp_path, monkeypatch, path):
    module()
    write(tmp_path)
    write(tmp_path, 'test/main.dart')
    write(tmp_path, 'lib/a.g.dart')
    fake(monkeypatch, report(path=path))
    with pytest.raises(adapters.VerificationAdapterError):
        evaluate(tmp_path)


@pytest.mark.parametrize('position', [{'line': 0, 'column': 1}, {'line': True, 'column': 1},
                                    {'line': 1, 'column': -1}, {'line': '1', 'column': 1},
                                    {'line': 1}, None, {'line': 1.5, 'column': 1}])
def test_malformed_location(tmp_path, monkeypatch, position):
    module()
    write(tmp_path)
    payload = report()
    payload['files']['./lib/main.dart']['mutants'][0]['location']['start'] = position
    fake(monkeypatch, payload)
    with pytest.raises(adapters.VerificationAdapterError):
        evaluate(tmp_path)


@pytest.mark.parametrize('fault', ['schema', 'files', 'language', 'mutants', 'empty', 'duplicate',
                                 'reverse', 'replacement', 'operator', 'alias'])
def test_untrustworthy_structure(tmp_path, monkeypatch, fault):
    module()
    write(tmp_path)
    payload = report()
    file = payload['files']['./lib/main.dart']
    mutant = file['mutants'][0]
    if fault == 'schema':
        payload['schemaVersion'] = 1
    elif fault == 'files':
        payload['files'] = []
    elif fault == 'language':
        file['language'] = 'javascript'
    elif fault == 'mutants':
        file['mutants'] = {}
    elif fault == 'empty':
        file['mutants'] = []
    elif fault == 'duplicate':
        file['mutants'].append(copy.deepcopy(mutant))
    elif fault == 'reverse':
        mutant['location']['end']['column'] = 1
    elif fault in ('replacement', 'operator'):
        mutant['replacement' if fault == 'replacement' else 'mutatorName'] = None
    else:
        payload['files']['lib/main.dart'] = copy.deepcopy(file)
    fake(monkeypatch, payload)
    with pytest.raises(adapters.VerificationAdapterError):
        evaluate(tmp_path)


@pytest.mark.parametrize('raw', ['{', '{"schemaVersion":"1","schemaVersion":"1","files":{}}',
                               '{"schemaVersion":"1","files":{},"score":NaN}',
                               '{"schemaVersion":"1","files":{},"score":1e999}', '[]', None])
def test_malformed_or_missing_json(tmp_path, monkeypatch, raw):
    module()
    write(tmp_path)

    def hook(argv, _):
        if argv[0] == 'dart_mutant' and '--output' in argv:
            if raw is not None:
                (Path(argv[-1]) / 'mutation-report.json').write_text(raw, encoding='utf-8')
            return subprocess.CompletedProcess(argv, 0, '', '')

    fake(monkeypatch, hook=hook)
    with pytest.raises(adapters.VerificationAdapterError):
        evaluate(tmp_path)


def test_semantic_inventory_exact_and_volatile_fields(tmp_path, monkeypatch):
    write(tmp_path)
    write(tmp_path, 'test/example_test.dart')
    payload = report(('Survived', 'Killed'))
    fake(monkeypatch, payload, stdout='log1')
    first = evaluate(tmp_path)
    expected = {'schema_version': 2, 'attestation': 'independent_replay_v1', 'mutants': [
        {'path': 'lib/main.dart', 'start_line': 1, 'start_column': i + 25, 'end_line': 1,
         'end_column': i + 26, 'mutator_name': 'Arithmetic', 'replacement': '-', 'status': status}
        for i, status in enumerate(('Survived', 'Killed'))]}
    assert first.semantic_sha256 == hashlib.sha256(json.dumps(expected, sort_keys=True, separators=(',', ':'),
                                                             ensure_ascii=False, allow_nan=False).encode()).hexdigest()
    payload['projectRoot'] = 'different temporary root'
    payload['mutationScore'] = 100
    payload['thresholds'] = {'high': 0, 'low': 0}
    payload['files']['./lib/main.dart']['mutants'].reverse()
    for mutant in payload['files']['./lib/main.dart']['mutants']:
        mutant['description'] = 'SECRET'
    fake(monkeypatch, payload, stdout='log2', stderr='noise')
    second = evaluate(tmp_path)
    assert first.semantic_sha256 == second.semantic_sha256 and first.result_sha256 == second.result_sha256
    assert first.stdout_sha256 != second.stdout_sha256 and first.stderr_sha256 != second.stderr_sha256


@pytest.mark.parametrize('field', ['path', 'start_line', 'start_column', 'end_line', 'end_column',
                                 'mutator_name', 'replacement', 'status'])
def test_every_semantic_field_changes_hash(tmp_path, monkeypatch, field):
    write(tmp_path, data=b'int add(int a, int b) => a + b;\n' * 3)
    write(tmp_path, 'test/example_test.dart')
    write(tmp_path, 'bin/a.dart')
    payload = report()
    fake(monkeypatch, payload)
    first = evaluate(tmp_path)
    mutant = payload['files']['./lib/main.dart']['mutants'][0]
    if field == 'path':
        payload['files']['bin/a.dart'] = payload['files'].pop('./lib/main.dart')
    elif field in ('start_line', 'end_line'):
        # Pinned v2 end_line is constrained to start_line, not independently
        # selectable. Both encoded coordinates move to a real baseline row.
        mutant['location']['start']['line'] = mutant['location']['end']['line'] = 2 if field == 'start_line' else 3
    elif field in ('start_column', 'end_column'):
        mutant['location']['start' if field.startswith('start') else 'end']['column'] -= 1 if field.startswith('start') else -1
    else:
        mutant[{'mutator_name': 'mutatorName'}.get(field, field)] = {'status': 'Timeout', 'replacement': '+', 'mutator_name': 'Other'}[field]
    mutant['id'] = 'fixture'
    fake(monkeypatch, payload)
    second = evaluate(tmp_path)
    assert first.semantic_sha256 != second.semantic_sha256 and first.result_sha256 != second.result_sha256


@pytest.mark.parametrize('kind', ['symlink', 'reparse', 'special'])
def test_unsafe_snapshot_input(tmp_path, monkeypatch, kind):
    adapter = module()
    path = write(tmp_path)
    if kind == 'symlink':
        try:
            (tmp_path / 'alias.dart').symlink_to(path)
        except OSError:
            pytest.skip('host cannot create symlinks')
    else:
        actual = adapter.os.lstat

        def unsafe(value, *args, **kwargs):
            info = actual(value, *args, **kwargs)
            if Path(value) == path:
                return SimpleNamespace(st_mode=stat.S_IFIFO if kind == 'special' else info.st_mode,
                                       st_file_attributes=0x400 if kind == 'reparse' else 0)
            return info

        monkeypatch.setattr(adapter.os, 'lstat', unsafe)
    calls = fake(monkeypatch)
    with pytest.raises(adapters.VerificationAdapterError):
        evaluate(tmp_path)
    assert not calls


@pytest.mark.parametrize('field,value', [('semantic_sha256', None), ('semantic_sha256', 'bad'),
                                       ('semantic_sha256', '0' * 64), ('result_sha256', '0' * 64)])
def test_tampered_result_rejected(tmp_path, monkeypatch, field, value):
    module()
    write(tmp_path)
    write(tmp_path, 'test/example_test.dart')
    fake(monkeypatch)
    result = replace(evaluate(tmp_path), **{field: value})
    with pytest.raises(adapters.VerificationAdapterError):
        adapters.validate_verification_adapter_result(result, expected_adapter_id='dart.mutation.strict')


@pytest.mark.parametrize('parameter', ['args', 'threshold', 'glob', 'excludes', 'command', 'sample'])
def test_no_project_supplied_flags(parameter):
    from project_system.rule_checkers import checker_contract_messages

    module()
    assert checker_contract_messages('code.verification', {'adapter': 'dart.mutation.strict', parameter: 'x'}) == ('unknown parameter: ' + parameter,)


def test_rule_evidence_semantics_and_governance(tmp_path, monkeypatch):
    from project_system.rule_engine import RuleEvaluationContext, evaluate_rules
    from project_system.rule_evidence import build_rule_evidence, rule_evidence_to_dict

    write(tmp_path)
    payload = report(('Survived',))
    payload['files']['./lib/main.dart']['mutants'][0]['replacement'] = 'SECRET_SOURCE'
    registry = {'schema_version': 1, 'profile': 'project-system-rules-v1', 'rules': {'QUALITY-001': {
        'title': 'Mutation', 'status': 'active', 'category': 'testing', 'description': 'Check mutation.',
        'verification': {'method': 'deterministic', 'checker': 'code.verification', 'parameters': {'adapter': 'dart.mutation.strict'}},
        'enforcement': {'severity': 'WARNING', 'checkpoints': ['sync_verify']}, 'exception_policy': 'forbidden'}}}
    context = RuleEvaluationContext(tmp_path, 'sync_verify', {}, True, evaluation_paths=('README.md',))

    def evidence(log):
        fake(monkeypatch, payload, stdout=log)
        results = evaluate_rules(registry, context)
        assert results[0].raw_status == 'FAIL'
        return rule_evidence_to_dict(build_rule_evidence(
            project_id='demo', git_head='1' * 40, cli_version='test', rules_registry=registry,
            context=context, results=results, exception_registry={
                'schema_version': 1, 'profile': 'project-system-rule-exceptions-v1', 'exceptions': {}}))

    first, second = evidence('SECRET_LOG'), evidence('different')
    assert first == second
    serialized = json.dumps(first)
    assert 'semantic_sha256' in serialized and 'stdout_sha256' not in serialized and 'stderr_sha256' not in serialized
    assert 'SECRET_SOURCE' not in serialized and 'SECRET_LOG' not in serialized and str(tmp_path) not in serialized


@pytest.mark.parametrize('change', ['rewrite', 'add', 'delete'])
def test_baseline_cannot_change_target_domain_or_bytes(tmp_path, monkeypatch, change):
    module()
    write(tmp_path)

    def hook(argv, policy):
        if argv[-1] == '--reporter=compact':
            if change == 'delete':
                (policy['cwd'] / 'lib/main.dart').unlink()
            else:
                write(policy['cwd'], 'new.dart' if change == 'add' else 'lib/main.dart', b'changed')

    calls = fake(monkeypatch, hook=hook)
    with pytest.raises(adapters.VerificationAdapterError):
        evaluate(tmp_path)
    assert len(calls) == 3
    assert (tmp_path / 'lib/main.dart').read_bytes() == b'int add(int a, int b) => a + b;\n'


@pytest.mark.parametrize('change', ['bytes', 'added', 'mode'])
def test_canonical_drift_during_version_is_rejected_before_baseline(tmp_path, monkeypatch, change):
    module()
    path = write(tmp_path)

    def hook(argv, _):
        if argv[-1] == '--version':
            if change == 'bytes':
                path.write_bytes(b'changed')
            elif change == 'added':
                write(tmp_path, 'new.dart')
            else:
                path.chmod(stat.S_IREAD)

    calls = fake(monkeypatch, hook=hook)
    try:
        with pytest.raises(adapters.VerificationAdapterError):
            evaluate(tmp_path)
        assert len(calls) == 1
    finally:
        path.chmod(stat.S_IREAD | stat.S_IWRITE)


def test_contradictory_status_for_one_identity_is_error(tmp_path, monkeypatch):
    module()
    write(tmp_path)
    payload = report()
    mutants = payload['files']['./lib/main.dart']['mutants']
    mutants.append(dict(mutants[0], status='Survived'))
    fake(monkeypatch, payload)
    with pytest.raises(adapters.VerificationAdapterError):
        evaluate(tmp_path)


def test_native_windows_relative_report_path(tmp_path, monkeypatch):
    write(tmp_path)
    write(tmp_path, 'test/example_test.dart')
    fake(monkeypatch, report(path='.\\lib\\main.dart'))
    assert evaluate(tmp_path).verification_status == 'PASS'


@pytest.mark.parametrize('kind', ['file', 'directory'])
def test_report_symlink_rejected_and_canonical_unchanged(tmp_path, monkeypatch, kind):
    module()
    write(tmp_path)
    outside = write(tmp_path, 'outside.json', json.dumps(report()).encode())
    # Establish capability before evaluation, so a swallowed hook exception
    # cannot masquerade as a successful negative test.
    probe = tmp_path / 'probe'
    try:
        probe.symlink_to(outside)
        probe.unlink()
    except OSError:
        pytest.skip('host cannot create symlinks')

    def hook(argv, _):
        if '--output' in argv:
            report_root = Path(argv[-1])
            if kind == 'directory':
                report_root.rmdir()
                report_root.symlink_to(tmp_path, target_is_directory=True)
            else:
                (report_root / 'mutation-report.json').symlink_to(outside)
            return subprocess.CompletedProcess(argv, 0, '', '')

    fake(monkeypatch, hook=hook)
    with pytest.raises(adapters.VerificationAdapterError):
        evaluate(tmp_path)
    assert outside.read_bytes() == json.dumps(report()).encode()


def test_report_budget_is_enforced(tmp_path, monkeypatch):
    adapter = module()
    write(tmp_path)
    monkeypatch.setattr(adapter, 'MAX_REPORT_BYTES', 16)
    fake(monkeypatch)
    with pytest.raises(adapters.VerificationAdapterError):
        evaluate(tmp_path)


def test_large_copy_uses_bounded_reads_and_preserves_mode(tmp_path, monkeypatch):
    from project_system import osv_scan_adapter

    adapter = module()
    source = write(tmp_path, data=b' ' * (2 * adapter.CHUNK_BYTES + 17))
    before = stat.S_IMODE(source.stat().st_mode)
    actual_open, reads = osv_scan_adapter._open_binary, []

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
            assert size == adapter.CHUNK_BYTES
            reads.append(size)
            return self.stream.read(size)

    monkeypatch.setattr(osv_scan_adapter, '_open_binary', ObservedStream)

    def hook(argv, policy):
        if argv[-1] == '--reporter=compact':
            assert (policy['cwd'] / 'lib/main.dart').read_bytes() == source.read_bytes()
            assert stat.S_IMODE((policy['cwd'] / 'lib/main.dart').stat().st_mode) == before

    write(tmp_path, 'test/example_test.dart')
    calls = fake(monkeypatch, hook=hook)
    assert evaluate(tmp_path).verification_status == 'PASS'
    assert len(calls) == 6
    assert len(reads) >= 16


def test_unreadable_input_is_error_without_execution(tmp_path, monkeypatch):
    from project_system import osv_scan_adapter

    module()
    write(tmp_path)
    calls = fake(monkeypatch)

    def denied(_):
        raise PermissionError('SECRET FILE ACCESS')

    monkeypatch.setattr(osv_scan_adapter, '_open_binary', denied)
    with pytest.raises(adapters.VerificationAdapterError) as caught:
        evaluate(tmp_path)
    assert not calls and 'SECRET' not in str(caught.value)


def test_rule_boundary_maps_red_baseline_to_error(tmp_path, monkeypatch):
    from project_system.rule_checkers import evaluate_checker
    from project_system.rule_engine import RuleEvaluationContext

    module()
    write(tmp_path)
    calls = fake(monkeypatch, hook=lambda argv, _: subprocess.CompletedProcess(argv, 1, 'SECRET', '')
                 if argv[-1] == '--reporter=compact' else None)
    result = evaluate_checker('code.verification', RuleEvaluationContext(tmp_path, 'sync_verify', {}, True),
                              {'adapter': 'dart.mutation.strict'})
    assert result.raw_status == 'ERROR'
    assert result.details == {'adapter_id': 'dart.mutation.strict', 'adapter_version': '2'}
    assert len(calls) == 3


def test_copy_detects_mid_read_changes_before_any_tool(tmp_path, monkeypatch):
    from project_system import osv_scan_adapter

    module()
    source = write(tmp_path)
    actual = osv_scan_adapter._open_binary

    def raced(path):
        stream = actual(path)
        source.write_bytes(source.read_bytes() + b'changed')
        return stream

    monkeypatch.setattr(osv_scan_adapter, '_open_binary', raced)
    calls = fake(monkeypatch)
    with pytest.raises(adapters.VerificationAdapterError):
        evaluate(tmp_path)
    assert not calls


def test_temporary_base_inside_canonical_root_is_rejected_before_creation(tmp_path, monkeypatch):
    adapter = module()
    write(tmp_path)
    fake(monkeypatch)
    monkeypatch.setattr(adapter.tempfile, 'gettempdir', lambda: str(tmp_path))
    actual = adapter.tempfile.TemporaryDirectory
    created = []

    def observe(*args, **kwargs):
        created.append(True)
        return actual(*args, **kwargs)

    monkeypatch.setattr(adapter.tempfile, 'TemporaryDirectory', observe)
    with pytest.raises(adapters.VerificationAdapterError):
        evaluate(tmp_path)
    assert not created


def test_baseline_and_mutant_have_the_same_fixed_test_suite_budget(tmp_path, monkeypatch):
    write(tmp_path)
    write(tmp_path, 'test/example_test.dart')
    calls = fake(monkeypatch)
    assert evaluate(tmp_path).verification_status == 'PASS'
    baseline_argv, baseline_policy = calls[2]
    mutation_argv, mutation_policy = calls[3]
    assert baseline_argv == ('dart', 'test', '--reporter=compact')
    assert baseline_policy['timeout'] == 300
    assert mutation_argv == ('dart_mutant', '--path', '.', '--parallel', '1', '--timeout', '300',
                             '--threshold', '0', '--quiet', '--json', '--ai', 'none', '--output', mutation_argv[-1])
    assert int(mutation_argv[mutation_argv.index('--timeout') + 1]) == baseline_policy['timeout']
    assert mutation_policy['timeout'] == 7200


@pytest.mark.parametrize('status', ['Survived', 'NoCoverage'])
def test_same_location_distinct_mutants_have_complete_findings_and_private_evidence(tmp_path, monkeypatch, status):
    from project_system.rule_engine import RuleEvaluationContext, evaluate_rules
    from project_system.rule_evidence import build_rule_evidence, rule_evidence_to_dict

    write(tmp_path)
    payload = report((status,))
    mutants = payload['files']['./lib/main.dart']['mutants']
    mutants[0].update(mutatorName='Arithmetic add-to-sub', replacement='PRIVATE_REPLACEMENT_A',
                      description='PRIVATE_ORIGINAL_SOURCE', id='fixture')
    mutants.append(copy.deepcopy(mutants[0]))
    mutants[1].update(mutatorName='Arithmetic add-to-mul', replacement='PRIVATE_REPLACEMENT_B', id='fixture')
    fake(monkeypatch, payload, stdout='PRIVATE_STDOUT', stderr='PRIVATE_STDERR')
    result = evaluate(tmp_path)  # Includes real generic result validation.
    assert result.verification_status == 'FAIL'
    assert len(result.findings) == len(set(result.findings)) == 2
    prefix = 'Mutation survived the Dart test suite' if status == 'Survived' else 'Mutation has no test coverage'
    assert {finding.message for finding in result.findings} == {
        prefix + ' (Arithmetic add-to-sub)', prefix + ' (Arithmetic add-to-mul)'}
    assert all((finding.path, finding.line, finding.column) == ('lib/main.dart', 1, 25)
               for finding in result.findings)
    assert {finding.code for finding in result.findings} == {
        'dart.mutation.survived' if status == 'Survived' else 'dart.mutation.no_coverage'}
    normalized = [{'path': 'lib/main.dart', 'start_line': 1, 'start_column': 25,
                   'end_line': 1, 'end_column': 26, 'mutator_name': mutant['mutatorName'],
                   'replacement': mutant['replacement'], 'status': status} for mutant in mutants]
    normalized.sort(key=lambda mutant: tuple(mutant[key] for key in module().MUTANT_FIELDS))
    expected = json.dumps({'schema_version': 2, 'attestation': 'independent_replay_v1', 'mutants': normalized}, sort_keys=True,
                          separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()
    assert result.semantic_sha256 == hashlib.sha256(expected).hexdigest()
    registry = {'schema_version': 1, 'profile': 'project-system-rules-v1', 'rules': {'QUALITY-001': {
        'title': 'Mutation', 'status': 'active', 'category': 'testing', 'description': 'Check mutation.',
        'verification': {'method': 'deterministic', 'checker': 'code.verification',
                         'parameters': {'adapter': 'dart.mutation.strict'}},
        'enforcement': {'severity': 'BLOCKING', 'checkpoints': ['sync_verify']}, 'exception_policy': 'forbidden'}}}
    context = RuleEvaluationContext(tmp_path, 'sync_verify', {}, True)
    results = evaluate_rules(registry, context)
    assert results[0].raw_status == 'FAIL'
    evidence = rule_evidence_to_dict(build_rule_evidence(
        project_id='demo', git_head='1' * 40, cli_version='test', rules_registry=registry,
        context=context, results=results, exception_registry={
            'schema_version': 1, 'profile': 'project-system-rule-exceptions-v1', 'exceptions': {}}))
    assert len(evidence['results'][0]['details']['findings']) == 2
    serialized = json.dumps(evidence)
    for private in ('PRIVATE_REPLACEMENT_A', 'PRIVATE_REPLACEMENT_B', 'PRIVATE_ORIGINAL_SOURCE',
                    'PRIVATE_TOOL_ID', 'OTHER_TOOL_ID', 'PRIVATE_STDOUT', 'PRIVATE_STDERR', str(tmp_path)):
        assert private not in serialized


@pytest.mark.parametrize('status', ['Survived', 'NoCoverage'])
def test_indistinguishable_findings_fail_closed_instead_of_losing_a_mutant(tmp_path, monkeypatch, status):
    module()
    write(tmp_path)
    payload = report((status,))
    mutants = payload['files']['./lib/main.dart']['mutants']
    mutants.append(dict(mutants[0], replacement='PRIVATE_DIFFERENT_REPLACEMENT'))
    fake(monkeypatch, payload)
    with pytest.raises(adapters.VerificationAdapterError):
        evaluate(tmp_path)
