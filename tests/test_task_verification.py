"""Stage 12C deterministic verification is neither acceptance nor completion."""

import json
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
import shutil

import pytest
from jsonschema import Draft202012Validator

import project_system.task_verification as module
from project_system.task_verification import (
    TaskVerifyError, TaskScopeError, TaskValidationError, verify_task,
    load_task_verification, serialize_task_verification, validate_task_verification,
)
from project_system.task_baseline import (
    TaskBaselineError, build_task_baseline, capture_task_state, serialize_task_baseline,
)
from project_system.task_obligations import build_task_obligations, serialize_task_obligations
from project_system.task_specification import serialize_task_specification
from project_system.rule_evidence import canonical_sha256, rule_evidence_to_dict
from project_system.frontmatter import read_object, write_object
from project_system.utils import distribution_root, dump_yaml

from project_system.cli import main
from project_system.init_project import init_project
from project_system.objects import create_object
from project_system.tasking import task
from test_sync_finalize import _commit_project, _git
from test_rule_engine import rule, registry


def test_task_verify_cli_consumes_existing_lifecycle(tmp_path, monkeypatch, capsys):
    root = init_project('Demo', tmp_path / 'demo')
    _, target = create_object(root, 'feature', 'Target', 'product', 'owner')
    _commit_project(root)
    output, _ = task(root, target, budget='small')
    monkeypatch.chdir(root)
    main(['task', 'verify', target, '--budget', 'small'])
    capsys.readouterr()
    report = json.loads((output / 'task-verification.json').read_bytes())
    assert report['verification_result'] == 'PASS'
    assert report['semantic_acceptance_verified'] is False
    assert report['task_completion_claimed'] is False


def _edit(path, **fields):
    data, body = read_object(path)
    data.update(fields)
    write_object(path, data, body)


@pytest.fixture(scope='module')
def prepared(tmp_path_factory):
    root = init_project('Demo', tmp_path_factory.mktemp('verification-base') / 'demo')
    objects = {}
    for name, kind in [('target', 'feature'), ('req', 'requirement'), ('risk', 'risk'),
                       ('other-req', 'requirement'), ('other-risk', 'risk')]:
        path, identity = create_object(root, kind, name, 'product', 'owner')
        path = path.rename(path.with_name(identity + '-real-slug.md'))
        objects[name] = (path.relative_to(root), identity)
    _edit(root / objects['req'][0], status='active', acceptance_criteria=['Exact criterion', 'Exact criterion'], priority='high')
    _edit(root / objects['target'][0], requirements=[objects['req'][1]])
    _edit(root / objects['risk'][0], affects=[objects['target'][1]], severity='critical', mitigation='Bound context only')
    (root / '.project/policies/impact.yaml').write_text(dump_yaml({'version': 1, 'rules': [
        {'id': 'feature-docs', 'when': {'type': 'feature'}, 'check_docs': ['docs/03_PRODUCT.md']}]}), encoding='utf-8')
    head = _commit_project(root)
    return root, objects, head


@pytest.fixture
def project(prepared, tmp_path):
    root = tmp_path / 'relocated'
    shutil.copytree(prepared[0], root)
    return root, {key: (root / path, identity) for key, (path, identity) in prepared[1].items()}, prepared[2]


def _start(project):
    return task(project[0], project[1]['target'][1], budget='small')[0]


def _verify(project):
    return verify_task(project[0], project[1]['target'][1], budget='small')


def _rescope(project, output, effective, canonical=None):
    """Test fixture models an explicitly prepared contract, never runtime expansion."""
    spec = json.loads((output / 'task-spec.json').read_bytes())
    spec['write_scope']['effective'] = sorted(effective)
    if canonical is not None: spec['write_scope']['canonical'] = sorted(canonical)
    (output / 'task-spec.json').write_bytes(serialize_task_specification(spec).encode('utf-8'))
    ob = build_task_obligations(project[0], output / 'task-spec.json', project[1]['target'][1])
    (output / 'task-obligations.json').write_bytes(serialize_task_obligations(ob).encode('utf-8'))
    baseline = json.loads((output / 'task-baseline.json').read_bytes())
    rebuilt = build_task_baseline(project[2], baseline['entries'], (output / 'task-spec.json').read_bytes(),
                                 (output / 'task-obligations.json').read_bytes())
    (output / 'task-baseline.json').write_bytes(serialize_task_baseline(rebuilt).encode('utf-8'))


def test_report_exact_bindings_common_no_rules_and_no_semantic_claim(project):
    output = _start(project)
    root = project[0]
    protected = [root / 'project.yaml', *root.joinpath('docs').rglob('*.md'), *root.joinpath('knowledge').rglob('*.md')]
    before = {path: path.read_bytes() for path in protected}
    _, report = _verify(project)
    for file, key in [('task-spec.json', 'task_spec_sha256'), ('task-obligations.json', 'task_obligations_sha256'),
                      ('task-baseline.json', 'task_baseline_sha256')]:
        assert report[key] == sha256((output / file).read_bytes()).hexdigest()
    assert report['task_changed_paths'] == [] and report['rule_evidence'] is None
    assert report['obligation_count'] == 2 and report['risk_count'] == 1
    assert report['semantic_acceptance_verified'] is report['task_completion_claimed'] is False
    assert 'obligations' not in report and 'risk_outcomes' not in report
    assert {path: path.read_bytes() for path in protected} == before
    schema = json.loads((distribution_root() / 'schemas/task-verification.schema.json').read_bytes())
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(report)
    assert load_task_verification(output / 'task-verification.json') == report


def test_report_determinism_projection_not_authority_and_relocation(project, tmp_path):
    output = _start(project)
    (project[0] / 'docs/03_PRODUCT.md').write_bytes(b'allowed edit\n')
    _, first = _verify(project)
    raw = (output / 'task-verification.json').read_bytes()
    assert serialize_task_verification(first).encode('utf-8') == raw
    (output / 'task-verification.md').write_text('untrusted projection\n')
    _, again = _verify(project)
    assert first == again and (output / 'task-verification.json').read_bytes() == raw
    assert 'untrusted projection' not in (output / 'task-verification.md').read_text()
    moved = tmp_path / 'moved'
    shutil.copytree(project[0], moved)
    _, relocated = verify_task(moved, project[1]['target'][1], budget='small')
    assert relocated == first and str(project[0]) not in json.dumps(first)
    (project[0] / 'docs/03_PRODUCT.md').write_bytes(b'another allowed edit\n')
    _, changed = _verify(project)
    assert changed['working_tree_fingerprint'] != first['working_tree_fingerprint']


@pytest.mark.parametrize('case', ['head', 'project', 'spec', 'obligations', 'baseline', 'target', 'requirement-body',
    'requirement-status', 'requirement-priority', 'requirement-criteria', 'risk-body', 'risk-status',
    'risk-severity', 'risk-mitigation', 'risk-affects', 'feature-selection', 'skills-registry', 'skill-file'])
def test_binding_and_definition_drift_fail_closed(project, case):
    root, objects, _ = project
    output = _start(project)
    if case == 'head': _git(root, 'commit', '--allow-empty', '-qm', 'moved')
    elif case == 'project':
        import yaml
        config = yaml.safe_load((root / 'project.yaml').read_text())
        config['project']['id'] = 'other'
        (root / 'project.yaml').write_text(dump_yaml(config), encoding='utf-8')
    elif case in {'spec', 'obligations', 'baseline'}:
        path = output / {'spec': 'task-spec.json', 'obligations': 'task-obligations.json', 'baseline': 'task-baseline.json'}[case]
        doc = json.loads(path.read_bytes())
        if case == 'spec': doc['mode'] = 'tampered'
        elif case == 'obligations': doc['task_spec_sha256'] = '0' * 64
        else: doc['task_obligations_sha256'] = '0' * 64
        path.write_text(json.dumps(doc), encoding='utf-8')
    elif case in {'target', 'requirement-body', 'risk-body'}:
        path = objects[{'target': 'target', 'requirement-body': 'req', 'risk-body': 'risk'}[case]][0]
        path.write_bytes(path.read_bytes() + b'\nbody drift\n')
    elif case.startswith('requirement-'):
        field = case.split('-')[1]
        _edit(objects['req'][0], **{('acceptance_criteria' if field == 'criteria' else field):
               {'status': 'draft', 'priority': 'low', 'criteria': ['changed']}[field]})
    elif case.startswith('risk-'):
        field = case.split('-')[1]
        _edit(objects['risk'][0], **{field: {'status': 'accepted', 'severity': 'low',
                                           'mitigation': 'changed', 'affects': [objects['other-req'][1]]}[field]})
    elif case == 'feature-selection': _edit(objects['target'][0], requirements=[objects['other-req'][1]])
    else:
        spec = json.loads((output / 'task-spec.json').read_bytes())
        path = root / ('.project/skills.yaml' if case == 'skills-registry' else spec['skills']['selected'][0]['path'])
        path.write_bytes(path.read_bytes() + b'\n# drift\n')
    with pytest.raises(TaskVerifyError): _verify(project)
    assert not (output / 'task-verification.json').exists()


def test_unrelated_definitions_not_in_obligations_are_irrelevant_if_preexisting(project):
    root, objects, _ = project
    _edit(objects['other-req'][0], acceptance_criteria=['not selected'])
    _edit(objects['other-risk'][0], affects=[objects['other-req'][1]], mitigation='not relevant')
    _start(project)
    _, report = _verify(project)
    assert report['task_changed_paths'] == [] and report['risk_count'] == 1


@pytest.mark.parametrize('change,expected', [('same', []), ('dirty-more', ['docs/03_PRODUCT.md']),
    ('dirty-revert', ['docs/03_PRODUCT.md']), ('untracked-more', ['docs/preexisting.md']),
    ('untracked-delete', ['docs/preexisting.md']), ('new', ['docs/new.md']),
    ('tracked-delete', ['docs/03_PRODUCT.md']), ('rename', ['docs/03_PRODUCT.md', 'docs/renamed.md'])])
def test_real_delta_and_fingerprint_including_reversion(project, change, expected):
    root = project[0]
    tracked = root / 'docs/03_PRODUCT.md'
    original = tracked.read_bytes()
    tracked.write_bytes(b'dirty at start')
    (root / 'docs/preexisting.md').write_bytes(b'untracked at start')
    output = _start(project)
    _rescope(project, output, ['docs/**'], ['docs/**'])
    if change == 'dirty-more': tracked.write_bytes(b'further')
    elif change == 'dirty-revert': tracked.write_bytes(original)
    elif change == 'untracked-more': (root / 'docs/preexisting.md').write_bytes(b'further')
    elif change == 'untracked-delete': (root / 'docs/preexisting.md').unlink()
    elif change == 'new': (root / 'docs/new.md').write_bytes(b'new')
    elif change == 'tracked-delete': tracked.unlink()
    elif change == 'rename': tracked.rename(root / 'docs/renamed.md')
    _, report = _verify(project)
    assert report['task_changed_paths'] == expected
    states = {item['path']: item for item in report['task_state']}
    if change == 'dirty-revert':
        assert states['docs/03_PRODUCT.md']['sha256'] == sha256(original).hexdigest()
    if change == 'untracked-delete':
        assert states['docs/preexisting.md']['state'] == 'absent'


@pytest.mark.parametrize('scope,path,allowed', [
    (['docs/03_PRODUCT.md'], 'docs/03_PRODUCT.md', True), (['docs/**'], 'docs/sub/new.md', True),
    (['docs/**'], 'docs2/new.md', False), ([], 'docs/03_PRODUCT.md', False),
    (['docs/03_PRODUCT.md'], 'docs/other.md', False)])
def test_effective_not_canonical_scope_is_authority(project, scope, path, allowed):
    root = project[0]
    output = _start(project)
    _rescope(project, output, scope, ['docs/**'])
    destination = root / path
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(b'new allowed or forbidden bytes')
    if allowed:
        _, report = _verify(project)
        assert report['verification_result'] == 'PASS'
    else:
        with pytest.raises(TaskScopeError): _verify(project)
        report = load_task_verification(output / 'task-verification.json')
        assert report['changes_outside_scope'] == [path] and report['rule_evidence'] is None


def test_empty_scope_zero_delta_valid_generated_ignored(project):
    output = _start(project)
    _rescope(project, output, [], ['docs/**'])
    (output / 'extra-derived.txt').write_bytes(b'derived')
    assert _verify(project)[1]['verification_result'] == 'PASS'


@pytest.mark.parametrize('state', ['tracked-dirty', 'untracked'])
def test_unrelated_baseline_unchanged_allowed_but_later_edit_denied(project, state):
    root = project[0]
    path = root / ('README.md' if state == 'tracked-dirty' else 'owner-review.txt')
    path.write_bytes(b'pre-existing owner work')
    output = _start(project)
    assert _verify(project)[1]['task_changed_paths'] == []
    path.write_bytes(b'changed owner work')
    with pytest.raises(TaskScopeError): _verify(project)
    assert load_task_verification(output / 'task-verification.json')['changes_outside_scope'] == [path.name]


def _rules(root, *, severity='ERROR', required='docs/03_PRODUCT.md', policy='forbidden', scope=None):
    data = rule(parameters={'path': required}, checkpoints=['task_verify'], severity=severity, scope=scope)
    data['exception_policy'] = policy
    (root / '.project/policies/rules.yaml').write_text(dump_yaml(registry(**{'REPO-001': data})), encoding='utf-8')


def test_common_rule_invocation_exact_task_delta_and_common_evidence(project, monkeypatch):
    root = project[0]
    _rules(root)
    output = _start(project)
    (root / 'docs/03_PRODUCT.md').write_bytes(b'allowed implementation')
    calls = []
    original = module.validate_report
    def common(root, **options):
        report = original(root, **options)
        calls.append((options, report))
        return report
    monkeypatch.setattr(module, 'validate_report', common)
    _, report = _verify(project)
    assert calls[0][0] == {'rule_checkpoint': 'task_verify', 'rule_base_commit': project[2],
                            'rule_evaluation_paths': ('docs/03_PRODUCT.md',)}
    assert report['rule_evidence'] == rule_evidence_to_dict(calls[0][1].rule_evidence)
    assert report['rule_evidence']['results'][0]['raw_status'] == 'PASS'
    assert load_task_verification(output / 'task-verification.json') == report


@pytest.mark.parametrize('severity,blocked', [('BLOCKING', True), ('ERROR', True), ('WARNING', False), ('INFO', False)])
def test_common_rule_fail_uses_existing_severity_not_new_policy(project, severity, blocked):
    _rules(project[0], severity=severity, required='docs/missing.md')
    output = _start(project)
    if blocked:
        with pytest.raises(TaskValidationError): _verify(project)
        report = load_task_verification(output / 'task-verification.json')
    else: report = _verify(project)[1]
    assert report['rule_evidence']['results'][0]['raw_status'] == 'FAIL'
    assert report['verification_result'] == ('VALIDATION_FAIL' if blocked else 'PASS')


def test_checker_error_dominates_warning_severity(project, monkeypatch):
    import project_system.rule_checkers as checkers
    _rules(project[0], severity='WARNING')
    output = _start(project)
    def explode(*args, **kwargs): raise RuntimeError('bounded checker failure')
    monkeypatch.setattr(checkers, '_inspect_repository_path', explode)
    with pytest.raises(TaskValidationError): _verify(project)
    report = load_task_verification(output / 'task-verification.json')
    assert report['rule_evidence']['results'][0]['raw_status'] == 'ERROR'
    assert report['validation']['counts']['ERROR'] > 0


def test_rule_and_exception_drift_remain_common_evidence_not_task_definition(project):
    root = project[0]
    _rules(root)
    _start(project)
    _, first = _verify(project)
    path = root / '.project/policies/rules.yaml'
    import yaml
    data = yaml.safe_load(path.read_text())
    data['rules']['REPO-001']['description'] = 'Rule definition byte/semantic drift'
    path.write_text(dump_yaml(data), encoding='utf-8')
    # Policy edits are out of task write authority. A separately prepared lifecycle
    # records the policy change as pre-existing work, not implementation delta.
    task(root, project[1]['target'][1], budget='medium')
    _, second = verify_task(root, project[1]['target'][1], budget='medium')
    assert first['rule_evidence']['rules_registry_sha256'] != second['rule_evidence']['rules_registry_sha256']
    exception_path = root / '.project/policies/rule_exceptions.yaml'
    data = yaml.safe_load(exception_path.read_text())
    exception_id = 'EXC-20261007-deadbeef'
    data['exceptions'][exception_id] = {
        'rule_id': 'REPO-001', 'state': 'revoked', 'mode': 'permanent', 'reason': 'fixture',
        'scope': {'paths': ['docs/03_PRODUCT.md']}, 'decision_id': project[1]['target'][1],
        'approved_by': 'owner', 'approved_at': '2026-10-07T00:00:00Z',
        'revoked_by': 'owner', 'revoked_at': '2026-10-07T01:00:00Z', 'revocation_reason': 'fixture'}
    # Use no exception payload invention: canonical decision reference for existing common model.
    decision_path, decision = create_object(root, 'decision', 'Exception', 'product', 'owner')
    _edit(decision_path, status='active', approved_by='owner', approved_at='2026-10-07')
    data['exceptions'][exception_id]['decision_id'] = decision
    exception_path.write_text(dump_yaml(data), encoding='utf-8')
    task(root, project[1]['target'][1], budget='large')
    _, third = verify_task(root, project[1]['target'][1], budget='large')
    assert third['rule_evidence']['exception_registry_sha256'] != second['rule_evidence']['exception_registry_sha256']


def test_governed_waiver_is_preserved_via_common_pipeline(project):
    from test_rule_exceptions import exception
    root = project[0]
    decision_path, decision = create_object(root, 'decision', 'Waiver', 'product', 'owner')
    _edit(decision_path, status='active', approved_by='project-owner', approved_at='2026-09-27')
    _rules(root, required='docs/required.md', policy='decision_required')
    waiver = exception(decision_id=decision)
    (root / '.project/policies/rule_exceptions.yaml').write_text(dump_yaml({
        'schema_version': 1, 'profile': 'project-system-rule-exceptions-v1',
        'exceptions': {'EXC-20261007-deadbeef': waiver}}), encoding='utf-8')
    _start(project)
    _, report = _verify(project)
    result = report['rule_evidence']['results'][0]
    assert result['raw_status'] == 'FAIL' and result['effective_status'] == 'WAIVED'
    assert report['verification_result'] == 'PASS'


@pytest.mark.parametrize('field,value', [
    ('semantic_acceptance_verified', True), ('task_completion_claimed', True),
    ('deterministic_verification', False), ('schema_version', True), ('obligation_count', True),
    ('risk_count', -1), ('target_id', 'not-id'), ('extra', 1), ('rule_evidence', []),
    ('task_changed_paths', ['../escape']), ('verification_integrity', '0' * 64),
    ('working_tree_fingerprint', '0' * 64), ('task_state', []), ('verification_result', 'COMPLETE')])
def test_report_malformed_or_tampered_fail_closed(project, field, value):
    _start(project)
    (project[0] / 'docs/03_PRODUCT.md').write_bytes(b'changed')
    report = _verify(project)[1]
    report[field] = value
    with pytest.raises((TaskBaselineError, RuntimeError)):
        validate_task_verification(report)


@pytest.mark.parametrize('raw', [b'{', b'[]', b'{"a":1,"a":2}', b'{"a":NaN}', b'\xff'],
                         ids=['json', 'array', 'duplicate', 'nonfinite', 'utf8'])
def test_report_loader_rejects_malformed(tmp_path, raw):
    path = tmp_path / 'report.json'
    path.write_bytes(raw)
    with pytest.raises(TaskBaselineError): load_task_verification(path)


@pytest.mark.parametrize('missing', ['task-spec.json', 'task-obligations.json', 'task-baseline.json'])
def test_verify_never_regenerates_missing_artifacts(project, missing, monkeypatch):
    output = _start(project)
    (output / missing).unlink()
    import project_system.tasking as tasking_module
    def forbidden(*args, **kwargs): pytest.fail('verification must not call task creation')
    monkeypatch.setattr(tasking_module, 'task', forbidden)
    with pytest.raises(TaskVerifyError): _verify(project)
    assert not (output / missing).exists()


@pytest.mark.parametrize('options', [['--mode', 'implement'], ['--skill', 'knowledge-sync']])
def test_verify_rejects_creation_options(project, monkeypatch, options):
    _start(project)
    monkeypatch.chdir(project[0])
    with pytest.raises(SystemExit) as result: main(['task', 'verify', project[1]['target'][1], '--budget', 'small', *options])
    assert result.value.code == 2


@pytest.mark.parametrize('case,code', [('missing', 3), ('scope', 4), ('validation', 5)])
def test_expected_cli_errors_controlled_without_tracebacks(project, monkeypatch, capsys, case, code):
    root = project[0]
    if case == 'validation': _rules(root, required='docs/missing.md')
    if case != 'missing': _start(project)
    if case == 'scope': (root / 'outside.txt').write_bytes(b'outside')
    monkeypatch.chdir(root)
    with pytest.raises(SystemExit) as result: main(['task', 'verify', project[1]['target'][1], '--budget', 'small'])
    assert result.value.code == code
    assert 'Traceback' not in capsys.readouterr().err


def test_checker_side_effect_cannot_seal_stale_task_state(project, monkeypatch):
    root = project[0]
    output = _start(project)
    original = module.validate_report
    def side_effect(root, **options):
        result = original(root, **options)
        (root / 'docs/03_PRODUCT.md').write_bytes(b'changed during common validation')
        return result
    monkeypatch.setattr(module, 'validate_report', side_effect)
    with pytest.raises(TaskVerifyError, match='changed during validation'): _verify(project)
    assert not (output / 'task-verification.json').exists()


def test_generated_report_symlink_fail_closed(project, tmp_path):
    output = _start(project)
    outside = tmp_path / 'outside.json'
    outside.write_bytes(b'protected')
    try: (output / 'task-verification.json').symlink_to(outside)
    except OSError: pytest.skip('symlink permission unavailable')
    with pytest.raises(TaskVerifyError, match='symlink|reparse'): _verify(project)
    assert outside.read_bytes() == b'protected'


def test_report_bounded_loader_and_rehashed_contradictions(project, tmp_path):
    _start(project)
    report = _verify(project)[1]
    bad = deepcopy(report)
    bad['validation']['counts']['ERROR'] = 1
    bad['verification_integrity'] = canonical_sha256({key: value for key, value in bad.items()
                                                    if key != 'verification_integrity'})
    with pytest.raises(TaskBaselineError, match='counts'): validate_task_verification(bad)
    path = tmp_path / 'oversized.json'
    path.write_bytes(b' ' * (module.MAX_VERIFICATION_BYTES + 1))
    with pytest.raises(TaskBaselineError, match='size limit'): load_task_verification(path)


def test_nested_common_evidence_tamper_even_after_outer_rehash(project):
    _rules(project[0])
    _start(project)
    report = _verify(project)[1]
    report['rule_evidence']['results'][0]['raw_status'] = 'FAIL'
    report['verification_integrity'] = canonical_sha256({key: value for key, value in report.items()
                                                       if key != 'verification_integrity'})
    with pytest.raises(TaskBaselineError, match='fingerprint'): validate_task_verification(report)


def test_actual_task_checkpoint_executes_existing_code_quality_stack(project, monkeypatch):
    from test_stage11_closure import prepare, registry as quality_registry, transport, assert_stack
    root = project[0]
    prepare(root)
    rules = quality_registry(severity='ERROR')
    for data in rules['rules'].values(): data['enforcement']['checkpoints'] = ['task_verify']
    (root / '.project/policies/rules.yaml').write_text(dump_yaml(rules), encoding='utf-8')
    state = transport(monkeypatch)
    output = _start(project)
    _rescope(project, output, ['lib/**'], ['lib/**'])
    source = root / 'lib/main.dart'
    source.write_bytes(source.read_bytes() + b'\n// task implementation delta\n')
    _, report = _verify(project)
    assert_stack(report['rule_evidence'])
    assert report['rule_evidence']['checkpoint'] == 'task_verify'
    assert all(result['raw_status'] == 'PASS' for result in report['rule_evidence']['results'])
    assert all(state['calls'].values())
    assert report['semantic_acceptance_verified'] is report['task_completion_claimed'] is False


def test_common_validation_bad_configuration_fails_closed_not_attribute_error(project):
    import yaml
    root = project[0]
    path, _ = create_object(root, 'decision', 'Owner decision', 'product', 'owner')
    _edit(path, status='active', approved_by='owner', approved_at='2026-10-07')
    config = yaml.safe_load((root / 'project.yaml').read_text())
    config['validation'] = 'not an object'
    (root / 'project.yaml').write_text(dump_yaml(config), encoding='utf-8')
    output = _start(project)
    with pytest.raises(TaskValidationError, match='common validation pipeline'):
        _verify(project)
    assert not (output / 'task-verification.json').exists()
