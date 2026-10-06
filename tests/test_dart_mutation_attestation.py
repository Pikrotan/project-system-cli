"""Stage 11B: independent evidence, never an engine status alone."""

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import stat
import subprocess

import pytest

from project_system import dart_mutation_adapter as mutation
from project_system import verification_adapters as adapters
from project_system.process_runner import ProcessOutputLimitExceeded
from test_dart_test_adapter import (
    _event, _json_stream, _passing_output, _start, _suite, _test_done, _test_start,
)


SOURCE = b'int add(int a, int b) => a + b;\n'


def engine_report(source=SOURCE, *, original=b'+', replacement='-', status='Killed', raw_path='./lib/main.dart'):
    offset = source.index(original)
    line = source[:offset].count(b'\n') + 1
    column = offset - source.rfind(b'\n', 0, offset)
    original_text = original.decode('utf-8')
    identifier = hashlib.md5(f'{raw_path}:{line}:{original_text}:{replacement}'.encode('utf-8')).hexdigest()
    return {'schemaVersion': '1', 'files': {raw_path: {'language': 'dart', 'mutants': [{
        'id': identifier, 'mutatorName': 'Arithmetic', 'replacement': replacement, 'status': status,
        'description': 'NOT AUTHORITY', 'location': {
            'start': {'line': line, 'column': column},
            'end': {'line': line, 'column': column + len(original)},
        },
    }]}}}


def test_stream(root, verdict='FAIL', *, runtime=False, volatile='SECRET'):
    suite = str(root / 'test/example_test.dart')
    if verdict == 'PASS':
        return _passing_output(suite)
    if verdict == 'NOT_APPLICABLE':
        return _json_stream(_start(), _event('allSuites', count=0), _event('done', success=True))
    return _json_stream(
        _start(), _event('allSuites', count=1), _suite(suite), _test_start('detected'),
        _event('error', testID=1, error=volatile, stackTrace=volatile, isFailure=not runtime),
        _test_done('error' if runtime else 'failure'), _event('done', success=False),
    )


# Not a pytest test: this helper intentionally begins with the protocol name.
test_stream.__test__ = False


def prepare(root, source=SOURCE):
    (root / 'lib').mkdir(parents=True, exist_ok=True)
    (root / 'lib/main.dart').write_bytes(source)
    (root / 'test').mkdir(exist_ok=True)
    (root / 'test/example_test.dart').write_text('void main() {}\n', encoding='utf-8')


def transport(monkeypatch, payload=None, *, hook=None, replay='FAIL', volatile='SECRET'):
    state = {'payload': payload or engine_report(), 'calls': [], 'replay': replay, 'volatile': volatile}

    def run(argv, **policy):
        state['calls'].append((tuple(argv), policy))
        if hook:
            response = hook(argv, policy)
            if response is not None:
                return response
        if argv == ['dart_mutant', '--version']:
            return subprocess.CompletedProcess(argv, 0, 'dart_mutant 0.1.0\n', '')
        if argv == ['dart', 'test', '--reporter=compact']:
            return subprocess.CompletedProcess(argv, 0, state['volatile'], state['volatile'])
        if argv == ['dart', 'analyze', '--format=machine', '--no-plugins', '.']:
            return subprocess.CompletedProcess(argv, 0, '', state['volatile'])
        if argv == ['dart', 'test', '--reporter=json']:
            if state['replay'] == 'TIMEOUT':
                raise subprocess.TimeoutExpired(argv, policy['timeout'], output=state['volatile'])
            output = test_stream(policy['cwd'], state['replay'], volatile=state['volatile'])
            return subprocess.CompletedProcess(argv, 1 if state['replay'] == 'FAIL' else 0, output, state['volatile'])
        assert argv[:3] == ['dart_mutant', '--path', '.']
        (Path(argv[-1]) / 'mutation-report.json').write_text(json.dumps(state['payload']), encoding='utf-8')
        return subprocess.CompletedProcess(argv, 0, state['volatile'], state['volatile'])

    monkeypatch.setattr(mutation, 'run_process', run)
    return state


def evaluate(root):
    return adapters.evaluate_verification_adapter('dart.mutation.strict', root)


@pytest.mark.parametrize('original_error', [False, True])
def test_baseline_validity_anchors_fresh_snapshot_before_test_config_side_effects(
    tmp_path, monkeypatch, original_error,
):
    prepare(tmp_path)
    original = b'analyzer:\n  strong-mode:\n    implicit-casts: false\n'
    altered = b'analyzer:\n  exclude: ["lib/**"]\n'
    config = tmp_path / 'analysis_options.yaml'
    config.write_bytes(original)
    before = mutation._inventory(tmp_path)
    baseline_observations, compact_observations, engine_observations = [], [], []

    def hook(argv, policy):
        root = policy['cwd']
        if argv == ['dart', 'analyze', '--format=machine', '--no-plugins', '.']:
            if (root / 'lib/main.dart').read_bytes() == SOURCE:
                current = (root / 'analysis_options.yaml').read_bytes()
                baseline_observations.append((root, current, mutation._inventory(root)))
                # If project tests run first they can hide the original error;
                # only analysis of the fresh canonical-derived state is authority.
                if original_error and current == original:
                    path = str(root / 'lib/main.dart').replace('\\', '\\\\')
                    return subprocess.CompletedProcess(
                        argv, 3, f'ERROR|STATIC_TYPE_ERROR|CODE|{path}|1|1|1|Baseline error\n', '',
                    )
        elif argv == ['dart', 'test', '--reporter=compact']:
            compact_observations.append((root / 'analysis_options.yaml').read_bytes())
            (root / 'analysis_options.yaml').write_bytes(altered)
        elif '--output' in argv:
            engine_observations.append((root / 'analysis_options.yaml').read_bytes())

    state = transport(monkeypatch, hook=hook)
    if original_error:
        with pytest.raises(adapters.VerificationAdapterError):
            evaluate(tmp_path)
        assert [argv for argv, _ in state['calls']] == [
            ('dart_mutant', '--version'),
            ('dart', 'analyze', '--format=machine', '--no-plugins', '.'),
        ]
        assert not compact_observations and not engine_observations
    else:
        assert evaluate(tmp_path).verification_status == 'PASS'
        assert compact_observations == [original]
        # This is not a new blanket non-target immutability policy: the test's
        # side effect may exist later, but cannot rewrite baseline validity.
        assert engine_observations == [altered]
    assert len(baseline_observations) == 1
    shadow, observed_config, observed_inventory = baseline_observations[0]
    assert shadow != tmp_path and not shadow.exists()
    assert observed_config == original and observed_inventory == before
    assert mutation._inventory(tmp_path) == before


def test_v2_registry_semantics_and_v1_not_interchangeable(tmp_path, monkeypatch):
    prepare(tmp_path)
    transport(monkeypatch)
    result = evaluate(tmp_path)
    assert adapters.VERIFICATION_ADAPTER_REGISTRY['dart.mutation.strict'].version == '2'
    assert result.adapter_version == '2'
    record = {'path': 'lib/main.dart', 'start_line': 1, 'start_column': 28,
              'end_line': 1, 'end_column': 29, 'mutator_name': 'Arithmetic', 'replacement': '-', 'status': 'Killed'}
    semantic = {'schema_version': 2, 'attestation': 'independent_replay_v1', 'mutants': [record]}
    digest = lambda value: hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
    assert result.semantic_sha256 == digest(semantic)
    assert result.semantic_sha256 != digest({'schema_version': 1, 'mutants': [record]})
    old = replace(result, adapter_version='1')
    old = replace(old, result_sha256=adapters.verification_result_sha256(old))
    assert old.result_sha256 != result.result_sha256
    with pytest.raises(adapters.VerificationAdapterError):
        adapters.validate_verification_adapter_result(old, expected_adapter_id='dart.mutation.strict')


@pytest.mark.parametrize('source,original,replacement', [
    (b'int x = (1\n + 2);\n', b'(1\n + 2)', '3'),
    (b'// prefix\r\nint x = 1 + 2;\r\n', b'+', '-'),
    ('// é\nvar café = "λ";\n'.encode(), 'λ'.encode(), 'Ω'),
    ('var café = "λ";\n'.encode(), 'λ'.encode(), 'Ω'),
    (b'// first\nvar x = true;', b'true', 'false'),
])
def test_exact_reconstruction_vectors(tmp_path, monkeypatch, source, original, replacement):
    prepare(tmp_path, source)
    expected = source.replace(original, replacement.encode(), 1)
    observed = []

    def hook(argv, policy):
        if argv == ['dart', 'test', '--reporter=json']:
            observed.append((policy['cwd'] / 'lib/main.dart').read_bytes())

    transport(monkeypatch, engine_report(source, original=original, replacement=replacement), hook=hook)
    assert evaluate(tmp_path).verification_status == 'PASS'
    assert observed == [expected]
    assert (tmp_path / 'lib/main.dart').read_bytes() == source


@pytest.mark.parametrize('fault', ['missing_id', 'id_type', 'id_upper', 'id_mismatch', 'raw_spelling',
                                 'end_line', 'zero', 'negative', 'eof', 'line', 'column', 'row_spill', 'utf8_start', 'utf8_end'])
@pytest.mark.parametrize('status', ['Killed', 'Survived', 'NoCoverage'])
def test_impossible_reconstruction_rejected_before_replay(tmp_path, monkeypatch, fault, status):
    source = 'var café = "λ";\n'.encode() if fault.startswith('utf8') else SOURCE
    if fault == 'row_spill':
        source *= 2
    prepare(tmp_path, source)
    payload = engine_report(source, original='λ'.encode() if fault.startswith('utf8') else b'+', status=status)
    mutant = payload['files']['./lib/main.dart']['mutants'][0]
    start, end = mutant['location']['start'], mutant['location']['end']
    if fault == 'missing_id':
        del mutant['id']
    elif fault == 'id_type':
        mutant['id'] = 1
    elif fault == 'id_upper':
        mutant['id'] = mutant['id'].upper()
    elif fault == 'id_mismatch':
        mutant['id'] = '0' * 32
    elif fault == 'raw_spelling':
        payload['files']['lib/main.dart'] = payload['files'].pop('./lib/main.dart')
    elif fault == 'end_line':
        end['line'] += 1
    elif fault in ('zero', 'negative'):
        end['column'] = start['column'] - (fault == 'negative')
    elif fault == 'eof':
        end['column'] = 99999
    elif fault == 'line':
        start['line'] = end['line'] = 99
    elif fault == 'column':
        start['column'], end['column'] = 99998, 99999
    elif fault == 'row_spill':
        start['column'], end['column'] = len(SOURCE) + 1, len(SOURCE) + 2
        # This span fits the buffer and has a matching ID, but belongs to the
        # next row. Only the row/byte-column consistency check can reject it.
        mutant['id'] = hashlib.md5(b'./lib/main.dart:1:i:-').hexdigest()
    elif fault == 'utf8_start':
        start['column'] += 1
    else:
        end['column'] -= 1
    state = transport(monkeypatch, payload)
    with pytest.raises(adapters.VerificationAdapterError):
        evaluate(tmp_path)
    assert not any(argv == ('dart', 'test', '--reporter=json') for argv, _ in state['calls'])


@pytest.mark.parametrize('status,replay,accepted', [
    ('Killed', 'FAIL', True), ('Killed', 'PASS', False), ('Killed', 'NOT_APPLICABLE', False),
    ('Killed', 'TIMEOUT', False), ('Timeout', 'TIMEOUT', True),
    ('Timeout', 'PASS', False), ('Timeout', 'FAIL', False),
])
def test_independent_status_attestation(tmp_path, monkeypatch, status, replay, accepted):
    prepare(tmp_path)
    state = transport(monkeypatch, engine_report(status=status), replay=replay)
    if accepted:
        assert evaluate(tmp_path).verification_status == 'PASS'
    else:
        with pytest.raises(adapters.VerificationAdapterError):
            evaluate(tmp_path)
    replay_calls = [(argv, policy) for argv, policy in state['calls'] if argv == ('dart', 'test', '--reporter=json')]
    assert len(replay_calls) == 1
    assert replay_calls[0][1]['timeout'] == 300
    assert replay_calls[0][1]['shell'] is False
    assert replay_calls[0][1]['max_capture_bytes'] == 16 * 1024 * 1024


@pytest.mark.parametrize('severity', ['ERROR', 'WARNING', 'INFO'])
@pytest.mark.parametrize('phase', ['baseline', 'mutant'])
def test_analyzer_error_free_precondition(tmp_path, monkeypatch, severity, phase):
    prepare(tmp_path)
    observed = []

    def hook(argv, policy):
        if argv[1] == 'analyze':
            observed.append(policy['cwd'])
            if (len(observed) == 1) == (phase == 'baseline'):
                path = str(policy['cwd'] / 'lib/main.dart').replace('\\', '\\\\')
                output = f'{severity}|STATIC_WARNING|CODE|{path}|1|1|1|SECRET\n'
                return subprocess.CompletedProcess(argv, {'ERROR': 3, 'WARNING': 2, 'INFO': 0}[severity], output, 'SECRET')

    state = transport(monkeypatch, hook=hook)
    if severity == 'ERROR':
        with pytest.raises(adapters.VerificationAdapterError):
            evaluate(tmp_path)
        assert len(observed) == (1 if phase == 'baseline' else 2)
        assert not any(argv[-1] == '--reporter=json' for argv, _ in state['calls'])
    else:
        assert evaluate(tmp_path).verification_status == 'PASS'
        assert len(observed) == 2


@pytest.mark.parametrize('change', ['bytes', 'mode', 'added', 'deleted'])
def test_engine_restoration_is_proved_before_report_trust(tmp_path, monkeypatch, change):
    prepare(tmp_path)

    def hook(argv, policy):
        if argv[0] == 'dart_mutant' and '--output' in argv:
            path = policy['cwd'] / 'lib/main.dart'
            if change == 'bytes':
                path.write_bytes(b'left mutated')
            elif change == 'mode':
                path.chmod(stat.S_IREAD)
            elif change == 'added':
                (policy['cwd'] / 'new.dart').write_bytes(b'new')
            else:
                path.unlink()

    state = transport(monkeypatch, hook=hook)
    with pytest.raises(adapters.VerificationAdapterError):
        evaluate(tmp_path)
    assert not any(argv[-1] == '--reporter=json' for argv, _ in state['calls'])
    assert (tmp_path / 'lib/main.dart').read_bytes() == SOURCE


@pytest.mark.parametrize('status', ['Survived', 'NoCoverage'])
def test_undetected_reconstructs_without_replay(tmp_path, monkeypatch, status):
    prepare(tmp_path)
    state = transport(monkeypatch, engine_report(status=status))
    assert evaluate(tmp_path).verification_status == 'FAIL'
    assert not any(argv[-1] == '--reporter=json' for argv, _ in state['calls'])
    assert sum(argv[1] == 'analyze' for argv, _ in state['calls']) == 1


def test_cross_mutant_fresh_copies_and_canonical_identity(tmp_path, monkeypatch):
    prepare(tmp_path)
    payload = engine_report()
    payload['files']['./lib/main.dart']['mutants'] += engine_report(original=b'b;', replacement='b * 1;', status='Killed')['files']['./lib/main.dart']['mutants']
    before = mutation._inventory(tmp_path)
    replays, engine_roots = [], []

    def hook(argv, policy):
        root = policy['cwd']
        if '--output' in argv:
            engine_roots.append(root)
            (root / 'engine-only.txt').write_text('not replay authority')
        if argv[1] == 'analyze' and (root / 'lib/main.dart').read_bytes() != SOURCE:
            assert root != tmp_path and root not in engine_roots and root not in replays
            assert not (root / 'engine-only.txt').exists()
            assert not (root / 'previous-replay.txt').exists()
            replays.append(root)
        if argv[-1] == '--reporter=json':
            (root / 'previous-replay.txt').write_text('not reused')

    transport(monkeypatch, payload, hook=hook)
    assert evaluate(tmp_path).verification_status == 'PASS'
    assert len(replays) == 2 and mutation._inventory(tmp_path) == before
    assert all(not root.exists() for root in replays + engine_roots)


def test_equivalent_replay_volatility_is_only_raw_provenance(tmp_path, monkeypatch):
    prepare(tmp_path)
    state = transport(monkeypatch)
    first = evaluate(tmp_path)
    state['volatile'] = 'OTHER SECRET /private/path 1234'
    state['payload']['files']['./lib/main.dart']['mutants'][0]['description'] = 'original SECRET'
    second = evaluate(tmp_path)
    assert first.semantic_sha256 == second.semantic_sha256 and first.result_sha256 == second.result_sha256
    assert first.stdout_sha256 != second.stdout_sha256 and first.stderr_sha256 != second.stderr_sha256
    assert 'SECRET' not in json.dumps(second.__dict__, default=str)


@pytest.mark.parametrize('phase', ['analyzer', 'replay'])
@pytest.mark.parametrize('fault', ['malformed', 'exit', 'bool_exit', 'bytes', 'missing', 'capture', 'timeout'])
def test_attestation_transport_is_fail_closed_and_sanitized(tmp_path, monkeypatch, phase, fault):
    prepare(tmp_path)
    reached = []

    def hook(argv, policy):
        selected = argv[1] == 'analyze' if phase == 'analyzer' else argv[-1] == '--reporter=json'
        if not selected:
            return
        reached.append(True)
        if fault == 'missing':
            raise FileNotFoundError('SECRET')
        if fault == 'capture':
            raise ProcessOutputLimitExceeded('stdout', 1)
        if fault == 'timeout':
            raise subprocess.TimeoutExpired(argv, policy['timeout'], output='SECRET')
        return subprocess.CompletedProcess(argv, True if fault == 'bool_exit' else 77 if fault == 'exit' else 0,
                                           b'SECRET' if fault == 'bytes' else 'SECRET', '')

    transport(monkeypatch, hook=hook)
    with pytest.raises(adapters.VerificationAdapterError) as caught:
        evaluate(tmp_path)
    assert reached and 'SECRET' not in str(caught.value)


def test_structured_runtime_failure_after_static_validity_counts(tmp_path, monkeypatch):
    prepare(tmp_path)
    transport(monkeypatch, hook=lambda argv, policy: subprocess.CompletedProcess(
        argv, 1, test_stream(policy['cwd'], runtime=True), '') if argv[-1] == '--reporter=json' else None)
    assert evaluate(tmp_path).verification_status == 'PASS'


def test_integrity_error_dominates_a_survivor(tmp_path, monkeypatch):
    prepare(tmp_path)
    payload = engine_report(status='Survived')
    payload['files']['./lib/main.dart']['mutants'] += engine_report(original=b'b;', replacement='b * 1;')['files']['./lib/main.dart']['mutants']
    transport(monkeypatch, payload, replay='PASS')
    with pytest.raises(adapters.VerificationAdapterError):
        evaluate(tmp_path)


@pytest.mark.parametrize('phase', ['analyze', 'test'])
@pytest.mark.parametrize('change', ['target', 'second_target', 'reparse', 'special'])
def test_replay_commands_cannot_rewrite_or_replace_target_ancestry(tmp_path, monkeypatch, phase, change):
    prepare(tmp_path)
    (tmp_path / 'lib/second.dart').write_bytes(b'void second() {}\n')
    marked, reached = set(), []
    real_lstat = mutation.os.lstat

    def inspected(path, *args, **options):
        info = real_lstat(path, *args, **options)
        if Path(path) in marked:
            from types import SimpleNamespace
            return SimpleNamespace(st_mode=stat.S_IFIFO if change == 'special' else info.st_mode,
                                   st_file_attributes=0x400 if change == 'reparse' else 0)
        return info

    monkeypatch.setattr(mutation.os, 'lstat', inspected)

    def hook(argv, policy):
        root = policy['cwd']
        target = root / 'lib/main.dart'
        if argv[1] != phase or root == tmp_path or not target.exists() or target.read_bytes() == SOURCE:
            return
        reached.append(root)
        if change in {'reparse', 'special'}:
            marked.add(root / 'lib' if change == 'reparse' else target)
        else:
            (target if change == 'target' else root / 'lib/second.dart').write_bytes(b'UNTRUSTED')

    transport(monkeypatch, hook=hook)
    before = mutation._inventory(tmp_path)
    with pytest.raises(adapters.VerificationAdapterError):
        evaluate(tmp_path)
    assert reached and mutation._inventory(tmp_path) == before
    assert all(not root.exists() for root in reached)


@pytest.mark.parametrize('stage', ['baseline', 'mutant'])
def test_analyzer_diagnostic_exit_contradiction_is_not_validity(tmp_path, monkeypatch, stage):
    prepare(tmp_path)
    reached = []

    def hook(argv, policy):
        if argv[1] == 'analyze' and ((policy['cwd'] / 'lib/main.dart').read_bytes() == SOURCE) == (stage == 'baseline'):
            reached.append(True)
            path = str(policy['cwd'] / 'lib/main.dart').replace('\\', '\\\\')
            return subprocess.CompletedProcess(argv, 0, f'WARNING|TYPE|CODE|{path}|1|1|0|SECRET\n', '')

    transport(monkeypatch, hook=hook)
    with pytest.raises(adapters.VerificationAdapterError):
        evaluate(tmp_path)
    assert reached


@pytest.mark.parametrize('fault', ['wrong_budget', 'wrong_command', 'capture_overflow'])
def test_timeout_authority_requires_the_fixed_independent_process(tmp_path, monkeypatch, fault):
    prepare(tmp_path)

    def hook(argv, policy):
        if argv[-1] == '--reporter=json':
            raise subprocess.TimeoutExpired(['other'] if fault == 'wrong_command' else argv,
                                            1 if fault == 'wrong_budget' else 300,
                                            output=b'x' * (mutation.MAX_CAPTURE_BYTES + 1) if fault == 'capture_overflow' else b'')

    transport(monkeypatch, engine_report(status='Timeout'), hook=hook)
    with pytest.raises(adapters.VerificationAdapterError):
        evaluate(tmp_path)


def test_reconstruction_read_is_bound_to_initial_source_snapshot(tmp_path, monkeypatch):
    prepare(tmp_path)
    engine_roots, reached = [], []

    def hook(argv, policy):
        if '--output' in argv:
            engine_roots.append(policy['cwd'])

    transport(monkeypatch, engine_report(status='Survived'), hook=hook)
    actual_read = mutation._read_file

    def changed_after_restoration(*args, **options):
        result = actual_read(*args, **options)
        if options.get('report'):
            reached.append(True)
            (engine_roots[0] / 'lib/main.dart').write_bytes(SOURCE.replace(b'int', b'var', 1))
        return result

    monkeypatch.setattr(mutation, '_read_file', changed_after_restoration)
    with pytest.raises(adapters.VerificationAdapterError):
        evaluate(tmp_path)
    assert reached and (tmp_path / 'lib/main.dart').read_bytes() == SOURCE


@pytest.mark.parametrize('relative', ['test/example_test.dart', './test/example_test.dart'])
def test_qualified_replay_protocol_accepts_contained_relative_suite_paths(tmp_path, monkeypatch, relative):
    prepare(tmp_path)

    def hook(argv, policy):
        if argv[-1] == '--reporter=json':
            output = test_stream(policy['cwd']).replace(str(policy['cwd'] / 'test/example_test.dart').replace('\\', '\\\\'), relative)
            return subprocess.CompletedProcess(argv, 1, output, '')

    transport(monkeypatch, hook=hook)
    assert evaluate(tmp_path).verification_status == 'PASS'


@pytest.mark.parametrize('fault,message', [
    ('end_line', 'pinned byte-span'), ('zero', 'pinned byte-span'),
    ('negative', 'pinned byte-span'), ('line', 'pinned byte-span'),
    ('eof', 'outside the trusted baseline'), ('column', 'outside the trusted baseline'),
    ('utf8_start', 'not valid UTF-8'), ('utf8_end', 'not valid UTF-8'),
])
def test_span_guards_are_not_false_positive_id_failures(tmp_path, fault, message):
    source = 'var café = "λ";\n'.encode() if fault.startswith('utf8') else SOURCE
    prepare(tmp_path, source)
    mutant = engine_report(source, original='λ'.encode() if fault.startswith('utf8') else b'+')['files']['./lib/main.dart']['mutants'][0]
    start, end = mutant['location']['start'], mutant['location']['end']
    if fault == 'end_line':
        end['line'] += 1
    elif fault in {'zero', 'negative'}:
        end['column'] = start['column'] - (fault == 'negative')
    elif fault == 'line':
        start['line'] = end['line'] = 999
    elif fault == 'eof':
        end['column'] = 999
    elif fault == 'column':
        start['column'], end['column'] = 998, 999
    elif fault == 'utf8_start':
        start['column'] += 1
    else:
        end['column'] -= 1
    record = dict(zip(mutation.MUTANT_FIELDS, (
        'lib/main.dart', start['line'], start['column'], end['line'], end['column'],
        mutant['mutatorName'], mutant['replacement'], mutant['status'],
    )))
    before = mutation._inventory(tmp_path)
    trusted = {path: (mode, digest) for path, mode, digest in before[1]}
    with pytest.raises(adapters.VerificationAdapterError, match=message):
        mutation._reconstruct(tmp_path, './lib/main.dart', mutant, record, trusted)
    assert mutation._inventory(tmp_path) == before


@pytest.mark.parametrize('drift', ['equivalent', 'Survived', 'NoCoverage', 'replay_pass', 'analyzer_error'])
def test_v2_real_sync_fresh_finalization_binding_and_isolation(tmp_path, monkeypatch, drift):
    from project_system import sync_finalization
    from project_system.sync_finalization import SyncFinalizeIntegrityError, finalize_sync
    from test_sync_finalize import (
        _assert_osv_blocked, _osv_fresh_binding, _osv_repository_identity,
        _setup_verified, _write_rule_layer,
    )

    payload = engine_report()
    payload['files']['./lib/main.dart']['mutants'] += engine_report(original=b'b;', replacement='b * 1;')['files']['./lib/main.dart']['mutants']

    def hook(argv, policy):
        if argv[1] == 'analyze' and state.get('fresh'):
            path = str(policy['cwd'] / 'lib/main.dart').replace('\\', '\\\\')
            mutated = (policy['cwd'] / 'lib/main.dart').read_bytes() != SOURCE
            severity = 'ERROR' if drift == 'analyzer_error' and mutated else 'WARNING'
            return subprocess.CompletedProcess(argv, 3 if severity == 'ERROR' else 2,
                                               f'{severity}|TYPE|CODE|{path}|1|1|1|PRIVATE ANALYZER\n', 'PRIVATE STDERR')

    state = transport(monkeypatch, payload, hook=hook)

    def configure(root, path, object_id):
        prepare(root)
        _write_rule_layer(root, {'QUALITY-001': {
            'title': 'Independent mutation', 'status': 'active', 'category': 'testing',
            'description': 'Attest exact mutants independently.',
            'verification': {'method': 'deterministic', 'checker': 'code.verification',
                             'parameters': {'adapter': 'dart.mutation.strict'}},
            'enforcement': {'severity': 'WARNING', 'checkpoints': ['project_validate', 'sync_verify']},
            'exception_policy': 'forbidden',
        }})

    root, _, _, head, _, pack_path, output, _ = _setup_verified(tmp_path, configure=configure)
    verified = json.loads((output / 'verification.json').read_text(encoding='utf-8'))
    assert verified['verification_result'] == 'passed' and verified['validation']['generation_ran']
    evidence = verified['rule_evidence_binding']['evidence']
    assert evidence['checkpoint'] == 'sync_verify'
    assert len(evidence['results']) == 1
    item = evidence['results'][0]
    assert item['raw_status'] == 'PASS' and item['details']['adapter_version'] == '2'
    expected_mutants = []
    for mutant in payload['files']['./lib/main.dart']['mutants']:
        start, end = mutant['location']['start'], mutant['location']['end']
        expected_mutants.append(dict(zip(mutation.MUTANT_FIELDS, (
            'lib/main.dart', start['line'], start['column'], end['line'], end['column'],
            mutant['mutatorName'], mutant['replacement'], mutant['status'],
        ))))
    expected_mutants.sort(key=lambda record: tuple(record[key] for key in mutation.MUTANT_FIELDS))
    expected_semantic = {'schema_version': 2, 'attestation': 'independent_replay_v1', 'mutants': expected_mutants}
    assert item['details']['semantic_sha256'] == hashlib.sha256(json.dumps(
        expected_semantic, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
    identity = _osv_repository_identity(root, pack_path)
    assert identity[2] == (verified['verified_working_tree_state'], verified['verification_fingerprint'])
    before_calls = len(state['calls'])
    real_validate = sync_finalization.validate_report
    state['rechecks'] = []

    def observe(root, **options):
        current = real_validate(root, **options)
        state['rechecks'].append((options, current))
        return current

    monkeypatch.setattr(sync_finalization, 'validate_report', observe)
    state['fresh'] = True
    state['volatile'] = 'PRIVATE changed logs /volatile/temp 42'
    state['payload']['files']['./lib/main.dart']['mutants'].reverse()
    if drift in {'Survived', 'NoCoverage'}:
        state['payload']['files']['./lib/main.dart']['mutants'][0]['status'] = drift
    if drift == 'replay_pass':
        state['replay'] = 'PASS'
    if drift == 'equivalent':
        _, report = finalize_sync(root, pack_path)
        assert report['state'] == 'prepared' and report['commit_sha'] is None
        assert not report['commit_requested'] and not report['push_requested']
    else:
        with pytest.raises(SyncFinalizeIntegrityError):
            finalize_sync(root, pack_path)
        _assert_osv_blocked(output, head, root)
    assert len(state['calls']) > before_calls
    assert any(argv == ('dart_mutant', '--version') for argv, _ in state['calls'][before_calls:])
    assert any(argv[1] == 'analyze' for argv, _ in state['calls'][before_calls:])
    current, rebuilt = _osv_fresh_binding(verified, state)
    new_item = rebuilt['evidence']['results'][0]
    assert new_item['raw_status'] == ('PASS' if drift == 'equivalent' else 'FAIL' if drift in {'Survived', 'NoCoverage'} else 'ERROR')
    if drift == 'equivalent':
        assert rebuilt == verified['rule_evidence_binding']
    elif drift in {'Survived', 'NoCoverage'}:
        assert new_item['details']['semantic_sha256'] != item['details']['semantic_sha256']
    else:
        assert new_item['details'] == {'adapter_id': 'dart.mutation.strict', 'adapter_version': '2'}
    assert 'PRIVATE' not in json.dumps(rebuilt)
    assert _osv_repository_identity(root, pack_path) == identity
