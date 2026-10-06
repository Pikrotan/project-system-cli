"""Stage 11C: four independent Rules through existing production boundaries."""

from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess

import pytest

from project_system import (
    dart_analyze_adapter, dart_format_adapter, dart_mutation_adapter,
    dart_test_adapter, sync_verification, validation, verification_adapters,
)
from project_system.rule_engine import RuleEvaluationContext, evaluate_rules
from project_system.rule_evidence import (
    RuleEvidenceError, build_rule_evidence, rule_evidence_to_dict,
)
from project_system.rule_exceptions import resolve_rule_exceptions
from project_system.sync_finalization import SyncFinalizeIntegrityError, finalize_sync
from test_dart_analyze_strict_adapter import machine
from test_dart_mutation_attestation import engine_report, prepare, test_stream
from test_dart_test_adapter import _passing_output
from test_rules_schema import validator
from test_sync_finalize import (
    _assert_osv_blocked, _git, _load_finalization, _osv_repository_identity,
    _setup_verified, _write_rule_layer,
)


STACK = ('dart.format', 'dart.analyze.strict', 'dart.test', 'dart.mutation.strict')
RULE_IDS = ('QUALITY-001', 'QUALITY-002', 'QUALITY-003', 'QUALITY-004')
VERSIONS = ('1', '1', '1', '2')


def registry(*, reverse=False, severity='WARNING'):
    rules = {
        rule_id: {
            'title': adapter, 'status': 'active', 'category': 'testing',
            'description': 'Independently verify one regression-quality claim.',
            'verification': {'method': 'deterministic', 'checker': 'code.verification',
                             'parameters': {'adapter': adapter}},
            'enforcement': {'severity': severity,
                            'checkpoints': ['project_validate', 'sync_verify']},
            'exception_policy': 'forbidden',
        }
        for rule_id, adapter in zip(RULE_IDS, STACK)
    }
    if reverse:
        rules = dict(reversed(tuple(rules.items())))
    result = {'schema_version': 1, 'profile': 'project-system-rules-v1', 'rules': rules}
    assert not list(validator('rules.schema.json').iter_errors(result))
    return result


def exceptions(values=None):
    return {'schema_version': 1, 'profile': 'project-system-rule-exceptions-v1',
            'exceptions': values or {}}


def transport(monkeypatch):
    """Replace only tool transports: parsers, adapters and lifecycle stay real."""
    state = {
        'format_dirty': False, 'strict_info': False, 'test_inventory': False,
        'direct_red': False, 'baseline_red': False, 'mutation_status': 'Killed',
        'replay': 'FAIL', 'volatile': 0, 'error_adapter': None,
        'calls': {adapter: [] for adapter in STACK}, 'probes': [],
    }

    def complete(argv, exit_code, stdout='', stderr=''):
        return subprocess.CompletedProcess(argv, exit_code, stdout, stderr)

    def json_noise(output):
        events = [json.loads(line) for line in output.splitlines()]
        for event in events:
            event['time'] += state['volatile'] * 100
            if event['type'] == 'start':
                event['pid'] += state['volatile']
        return ''.join(json.dumps(event, indent=None) + '\n' for event in events)

    def run(argv, **options):
        root = Path(options['cwd'])
        if argv == ['dart', '--version']:
            state['probes'].append((tuple(argv), root))
            return complete(argv, 0, '', 'Dart SDK version: 3.13.1 (stable)')
        if argv[:2] == ['dart', 'format']:
            adapter = STACK[0]
        elif '--fatal-infos' in argv:
            adapter = STACK[1]
        elif argv == ['dart', 'test', '--reporter', 'json']:
            adapter = STACK[2]
        else:
            adapter = STACK[3]
        state['calls'][adapter].append((tuple(argv), root))
        if state['error_adapter'] == adapter:
            raise OSError('PRIVATE-TRANSPORT /private/tool?token=secret')
        raw = f'PRIVATE-TRANSPORT volatile {state["volatile"]} {root}'
        stderr = raw if state['volatile'] else ''
        if adapter == STACK[0]:
            assert argv[:4] == ['dart', 'format', '--output=none', '--set-exit-if-changed']
            assert Path(argv[4]).is_file()
            return complete(argv, int(state['format_dirty']), raw, stderr)
        if adapter == STACK[1]:
            assert argv == ['dart', 'analyze', '--format=machine', '--no-plugins', '--fatal-infos', '.']
            stdout = machine(root, 'INFO') if state['strict_info'] else ''
            return complete(argv, int(state['strict_info']), stdout, stderr)
        if adapter == STACK[2]:
            stdout = (test_stream(root) if state['direct_red'] else
                      _passing_output(str(root / 'test/example_test.dart')))
            if state['test_inventory']:
                events = [json.loads(line) for line in stdout.splitlines()]
                for event in events:
                    if event['type'] == 'testStart':
                        event['test']['name'] = 'additional regression contract'
                stdout = ''.join(json.dumps(event) + '\n' for event in events)
            return complete(argv, int(state['direct_red']), json_noise(stdout), stderr)
        if argv == ['dart_mutant', '--version']:
            return complete(argv, 0, 'dart_mutant 0.1.0\n', stderr)
        if argv == ['dart', 'analyze', '--format=machine', '--no-plugins', '.']:
            return complete(argv, 0, '\n' if state['volatile'] else '', stderr)
        if argv == ['dart', 'test', '--reporter=compact']:
            return complete(argv, int(state['baseline_red']), raw, stderr)
        if argv == ['dart', 'test', '--reporter=json']:
            stdout = test_stream(root, state['replay'], volatile=raw)
            return complete(argv, int(state['replay'] == 'FAIL'), json_noise(stdout), stderr)
        assert argv[:3] == ['dart_mutant', '--path', '.'] and argv[-2] == '--output'
        payload = engine_report(status=state['mutation_status'])
        mutants = payload['files']['./lib/main.dart']['mutants']
        mutants += engine_report(original=b'b;', replacement='b * 1;')['files']['./lib/main.dart']['mutants']
        if state['volatile']:
            mutants.reverse()
        (Path(argv[-1]) / 'mutation-report.json').write_text(
            json.dumps(payload, indent=2 if state['volatile'] else None), encoding='utf-8',
        )
        return complete(argv, 0, raw, stderr)

    for module in (dart_format_adapter, dart_analyze_adapter, dart_test_adapter, dart_mutation_adapter):
        monkeypatch.setattr(module, 'run_process', run)
    return state


def evidence(root, rules=None, *, checkpoint='sync_verify', results=None,
             exception_registry=None, resolution=None):
    rules = registry() if rules is None else rules
    context = RuleEvaluationContext(root, checkpoint, {}, True, evaluation_paths=('README.md',))
    selected = evaluate_rules(rules, context) if results is None else results
    return rule_evidence_to_dict(build_rule_evidence(
        project_id='demo', git_head='1' * 40, base_commit='1' * 40, cli_version='test',
        rules_registry=rules, context=context, results=selected,
        exception_registry=exceptions() if exception_registry is None else exception_registry,
        exception_resolution=resolution,
    ))


def assert_stack(payload):
    assert payload['schema_version'] == 1
    assert [item['rule_id'] for item in payload['results']] == list(RULE_IDS)
    for item, adapter, version in zip(payload['results'], STACK, VERSIONS):
        assert item['checker'] == 'code.verification' and item['checker_version'] == '1'
        assert item['details']['adapter_id'] == adapter
        assert item['details']['adapter_version'] == version
        assert 'stdout_sha256' not in item['details'] and 'stderr_sha256' not in item['details']
        if item['raw_status'] != 'ERROR':
            for field in ('semantic_sha256', 'result_sha256'):
                assert len(item['details'][field]) == 64
            assert item['details']['verification_status'] == item['raw_status']
    assert 'PRIVATE-TRANSPORT' not in json.dumps(payload)


@pytest.mark.parametrize('checkpoint', ['project_validate', 'sync_verify'])
def test_four_independent_rules_registry_order_and_raw_volatility(tmp_path, monkeypatch, checkpoint):
    prepare(tmp_path)
    state = transport(monkeypatch)
    first = evidence(tmp_path, checkpoint=checkpoint)
    state['volatile'] = 1
    second = evidence(tmp_path, registry(reverse=True), checkpoint=checkpoint)
    assert_stack(first)
    assert first == second
    assert all(item['raw_status'] == item['effective_status'] == 'PASS' for item in first['results'])
    assert all(state['calls'].values())
    assert len({item['details']['result_sha256'] for item in first['results']}) == 4
    assert len({item['details']['semantic_sha256'] for item in first['results']}) == 4
    # Internal compact baseline and independently parsed direct suite both ran.
    assert len(state['calls']['dart.test']) == 2
    assert sum(argv == ('dart', 'test', '--reporter=compact')
               for argv, _ in state['calls']['dart.mutation.strict']) == 2


@pytest.mark.parametrize('status', ['Survived', 'NoCoverage'])
def test_direct_green_suite_does_not_imply_mutation_strength(tmp_path, monkeypatch, status):
    prepare(tmp_path)
    state = transport(monkeypatch)
    state['mutation_status'] = status
    payload = evidence(tmp_path)
    assert_stack(payload)
    assert [item['raw_status'] for item in payload['results']] == ['PASS', 'PASS', 'PASS', 'FAIL']
    findings = payload['results'][3]['details']['findings']
    assert [item['code'] for item in findings] == [
        'dart.mutation.survived' if status == 'Survived' else 'dart.mutation.no_coverage',
    ]
    # Only the other Killed mutant is replayed; negative engine claims are not replay authority.
    assert sum(argv == ('dart', 'test', '--reporter=json')
               for argv, _ in state['calls']['dart.mutation.strict']) == 1
    assert payload['results'][2]['details']['semantic_sha256'] != payload['results'][3]['details']['semantic_sha256']


@pytest.mark.parametrize('internal_red', [False, True])
def test_red_direct_suite_and_internal_baseline_are_separate_claims(tmp_path, monkeypatch, internal_red):
    prepare(tmp_path)
    state = transport(monkeypatch)
    state.update(direct_red=True, baseline_red=internal_red)
    payload = evidence(tmp_path)
    assert_stack(payload)
    assert [item['raw_status'] for item in payload['results']] == [
        'PASS', 'PASS', 'FAIL', 'ERROR' if internal_red else 'PASS',
    ]
    if internal_red:
        assert not any(argv[0] == 'dart_mutant' and '--path' in argv
                       for argv, _ in state['calls']['dart.mutation.strict'])
        assert payload['results'][3]['exception_id'] is None
    # No dependency edge: mutation PASS cannot replace a red direct-test Rule.
    assert len(state['calls']['dart.test']) == 1


@pytest.mark.parametrize('changed', STACK)
def test_isolated_semantic_drift_changes_only_one_rule(tmp_path, monkeypatch, changed):
    prepare(tmp_path)
    state = transport(monkeypatch)
    first = evidence(tmp_path)
    state[{'dart.format': 'format_dirty', 'dart.analyze.strict': 'strict_info',
           'dart.test': 'test_inventory', 'dart.mutation.strict': 'mutation_status'}[changed]] = (
        'Survived' if changed == 'dart.mutation.strict' else True
    )
    second = evidence(tmp_path)
    assert_stack(second)
    for old, new, adapter in zip(first['results'], second['results'], STACK):
        if adapter == changed:
            assert old['details']['semantic_sha256'] != new['details']['semantic_sha256']
            assert old['details']['result_sha256'] != new['details']['result_sha256']
        else:
            assert old == new
    assert first['evidence_fingerprint'] != second['evidence_fingerprint']
    if changed == 'dart.test':
        assert second['results'][2]['raw_status'] == second['results'][3]['raw_status'] == 'PASS'


@pytest.mark.parametrize('recipient', range(4))
@pytest.mark.parametrize('field', ['details', 'result_sha256', 'adapter_version', 'semantic_sha256'])
def test_cross_rule_identity_version_and_hash_tampering_rejected(tmp_path, monkeypatch, recipient, field):
    prepare(tmp_path)
    transport(monkeypatch)
    rules = registry()
    results = evaluate_rules(rules, RuleEvaluationContext(tmp_path, 'sync_verify', {}, True,
                                                        evaluation_paths=('README.md',)))
    assert_stack(evidence(tmp_path, rules, results=results))
    # Explicitly swap test/mutation in both directions, format/analysis likewise.
    donor = results[recipient ^ 1].details
    details = dict(donor) if field == 'details' else dict(results[recipient].details)
    if field == 'adapter_version':
        details[field] = '999'
    elif field == 'semantic_sha256':
        details[field] = '0' * 64
    elif field == 'result_sha256':
        details[field] = donor[field]
    forged = list(results)
    forged[recipient] = replace(results[recipient], details=details)
    with pytest.raises(RuleEvidenceError, match='result details are malformed'):
        evidence(tmp_path, rules, results=forged)


@pytest.mark.parametrize('severity', ['INFO', 'WARNING', 'ERROR', 'BLOCKING'])
@pytest.mark.parametrize('infrastructure', [False, True])
def test_real_project_validate_multifail_severity_and_error_dominance(
    tmp_path, monkeypatch, severity, infrastructure,
):
    state = transport(monkeypatch)

    def configure(root, path, object_id):
        prepare(root)
        _write_rule_layer(root, registry(severity=severity)['rules'])

    root, _, _, _, _, _, _, _ = _setup_verified(tmp_path, configure=configure)
    before = {adapter: len(calls) for adapter, calls in state['calls'].items()}
    state.update(format_dirty=True, strict_info=True, mutation_status='NoCoverage',
                 error_adapter='dart.test' if infrastructure else None)
    report = validation.validate_report(root)
    payload = rule_evidence_to_dict(report.rule_evidence)
    assert_stack(payload)
    assert payload['checkpoint'] == 'project_validate'
    assert [item['raw_status'] for item in payload['results']] == [
        'FAIL', 'FAIL', 'ERROR' if infrastructure else 'PASS', 'FAIL',
    ]
    assert all(len(state['calls'][adapter]) > count for adapter, count in before.items())
    levels = {location: level for level, location, _ in report.issues}
    assert levels == {rule_id: 'ERROR' if index == 2 else severity
                      for index, rule_id in enumerate(RULE_IDS)
                      if index != 2 or infrastructure}
    assert any(level in {'BLOCKING', 'ERROR'} for level in levels.values()) == (
        infrastructure or severity in {'ERROR', 'BLOCKING'}
    )
    assert payload['applied_exception_ids'] == []


def test_infrastructure_error_cannot_be_waived_by_existing_governance(tmp_path, monkeypatch):
    prepare(tmp_path)
    state = transport(monkeypatch)
    state['error_adapter'] = 'dart.mutation.strict'
    rules = registry()
    rules['rules'][RULE_IDS[3]]['exception_policy'] = 'decision_required'
    decision_id = 'DEC-20260927-deadbeef'
    context = RuleEvaluationContext(tmp_path, 'sync_verify', {
        decision_id: {'path': tmp_path / f'knowledge/decisions/{decision_id}.md',
                      'data': {'id': decision_id, 'type': 'decision'}, 'body': 'Approved fixture.'},
    }, True, evaluation_paths=('README.md',))
    values = exceptions({'EXC-20260927-deadbeef': {
        'rule_id': RULE_IDS[3], 'state': 'active', 'mode': 'permanent',
        'reason': 'Human-approved fixture waiver', 'scope': {'paths': ['lib/**']},
        'decision_id': decision_id, 'approved_by': 'project-owner',
        'approved_at': '2026-09-27T10:00:00Z',
    }})
    assert not list(validator('rule-exceptions.schema.json').iter_errors(values))
    results = evaluate_rules(rules, context)
    resolution = resolve_rule_exceptions(
        rules, values, context, results, as_of=datetime(2026, 9, 27, 12, tzinfo=timezone.utc),
    )
    assert resolution.applications == ()
    payload = rule_evidence_to_dict(build_rule_evidence(
        project_id='demo', git_head='1' * 40, cli_version='test', rules_registry=rules,
        exception_registry=values, context=context, results=results,
        exception_resolution=resolution,
    ))
    assert_stack(payload)
    assert payload['results'][3]['raw_status'] == payload['results'][3]['effective_status'] == 'ERROR'
    assert payload['results'][3]['exception_id'] is None


def verified_project(tmp_path, monkeypatch):
    state = transport(monkeypatch)

    def configure(root, path, object_id):
        prepare(root)
        _write_rule_layer(root, registry()['rules'])

    root, _, _, head, _, pack_path, output, _ = _setup_verified(tmp_path, configure=configure)
    verified = json.loads((output / 'verification.json').read_text(encoding='utf-8'))
    assert verified['verification_result'] == 'passed' and verified['errors'] == []
    assert verified['validation']['generation_ran'] is True
    assert_stack(verified['rule_evidence_binding']['evidence'])
    assert verified['rule_evidence_binding']['evidence']['checkpoint'] == 'sync_verify'
    assert all(item['raw_status'] == 'PASS'
               for item in verified['rule_evidence_binding']['evidence']['results'])
    return root, head, pack_path, output, verified, state


def fresh_binding(root, verified):
    current = validation.validate_report(
        root, rule_checkpoint='sync_verify', rule_base_commit=verified['base_commit'],
        rule_evaluation_paths=tuple(verified['actual_changed_canonical_paths']),
    )
    return current, sync_verification.build_rule_evidence_binding(
        current, verified['verification_fingerprint'],
    )


@pytest.mark.parametrize('drift', ['equivalent', *STACK, 'NoCoverage', 'replay_pass'])
def test_real_sync_fresh_finalization_four_independent_bindings(tmp_path, monkeypatch, drift):
    root, head, pack_path, output, verified, state = verified_project(tmp_path, monkeypatch)
    identity = _osv_repository_identity(root, pack_path)
    assert identity[2] == (verified['verified_working_tree_state'], verified['verification_fingerprint'])
    before = {adapter: len(calls) for adapter, calls in state['calls'].items()}
    state['volatile'] = 1
    if drift == 'dart.format':
        state['format_dirty'] = True
    elif drift == 'dart.analyze.strict':
        state['strict_info'] = True
    elif drift == 'dart.test':
        state['test_inventory'] = True
    elif drift in {'dart.mutation.strict', 'NoCoverage'}:
        state['mutation_status'] = 'NoCoverage' if drift == 'NoCoverage' else 'Survived'
    elif drift == 'replay_pass':
        state['replay'] = 'PASS'
    if drift == 'equivalent':
        _, report = finalize_sync(root, pack_path)
        assert report['state'] == _load_finalization(output)['state'] == 'prepared'
        assert report['commit_requested'] is report['push_requested'] is False
        assert report['commit_sha'] is None
        assert report['commit_result'] == report['push_result'] == 'not_requested'
    else:
        expected = 'current sync_verify Rules do not pass' if drift == 'replay_pass' else 'Rule Evidence changed'
        with pytest.raises(SyncFinalizeIntegrityError, match=expected):
            finalize_sync(root, pack_path)
        _assert_osv_blocked(output, head, root)
    # Before any extra inspection, prove actual finalize re-executed every Rule.
    assert all(len(state['calls'][adapter]) > count for adapter, count in before.items())
    mutation_calls = state['calls']['dart.mutation.strict'][before['dart.mutation.strict']:]
    replay_roots = [cwd for argv, cwd in mutation_calls if argv == ('dart', 'test', '--reporter=json')]
    assert replay_roots and all(cwd != root and not cwd.exists() for cwd in replay_roots)
    old_roots = {cwd for _, cwd in state['calls']['dart.mutation.strict'][:before['dart.mutation.strict']]}
    assert not set(replay_roots) & old_roots
    current, rebuilt = fresh_binding(root, verified)
    assert_stack(rebuilt['evidence'])
    changed = ('dart.mutation.strict' if drift in {'NoCoverage', 'replay_pass'} else drift)
    for old, new, adapter in zip(verified['rule_evidence_binding']['evidence']['results'],
                                 rebuilt['evidence']['results'], STACK):
        if adapter == changed:
            if drift == 'replay_pass':
                assert new['raw_status'] == new['effective_status'] == 'ERROR'
                assert new['details'] == {'adapter_id': adapter, 'adapter_version': '2'}
            else:
                assert old['details']['semantic_sha256'] != new['details']['semantic_sha256']
                assert old['details']['result_sha256'] != new['details']['result_sha256']
        else:
            assert old == new
    assert any(level in {'ERROR', 'BLOCKING'} for level, _, _ in current.issues) == (drift == 'replay_pass')
    assert (rebuilt == verified['rule_evidence_binding']) == (drift == 'equivalent')
    if drift == 'equivalent':
        assert report['rule_evidence_fingerprint'] == rebuilt['evidence']['evidence_fingerprint']
    assert _osv_repository_identity(root, pack_path) == identity
    assert _git(root, 'rev-parse', 'HEAD') == head and _git(root, 'diff', '--cached') == ''


@pytest.mark.parametrize('error_adapter', STACK)
def test_sync_infrastructure_error_is_isolated_and_blocks_finalization(tmp_path, monkeypatch, error_adapter):
    root, head, pack_path, output, verified, state = verified_project(tmp_path, monkeypatch)
    identity = _osv_repository_identity(root, pack_path)
    before = {adapter: len(calls) for adapter, calls in state['calls'].items()}
    state.update(error_adapter=error_adapter, volatile=1)
    with pytest.raises(SyncFinalizeIntegrityError, match='current sync_verify Rules do not pass'):
        finalize_sync(root, pack_path)
    assert all(len(state['calls'][adapter]) > count for adapter, count in before.items())
    current, rebuilt = fresh_binding(root, verified)
    assert_stack(rebuilt['evidence'])
    for old, new, adapter in zip(verified['rule_evidence_binding']['evidence']['results'],
                                 rebuilt['evidence']['results'], STACK):
        if adapter == error_adapter:
            assert new['raw_status'] == new['effective_status'] == 'ERROR'
            assert new['exception_id'] is None
        else:
            assert old == new
    assert rebuilt['evidence']['applied_exception_ids'] == []
    assert any(level == 'ERROR' for level, _, _ in current.issues)
    assert 'PRIVATE-TRANSPORT' not in (output / 'finalization.json').read_text(encoding='utf-8')
    _assert_osv_blocked(output, head, root)
    assert _osv_repository_identity(root, pack_path) == identity


def test_stage11_closure_contract_requires_independent_review():
    document = (Path(__file__).resolve().parents[1] / 'EXECUTABLE_RULES_V1.md').read_text(
        encoding='utf-8',
    )
    heading = '### Stage 11C: Regression-quality composition and closure'
    assert heading in document
    section = document.split(heading, 1)[1].split('\n## ', 1)[0]
    assert '**Stage 11 — COMPLETE**' in section
    assert 'conditional on independent main-chat integrity review' in section
    assert '**Next: Stage 12 — Task Specification, Risk, and Task Obligations.**' in section
