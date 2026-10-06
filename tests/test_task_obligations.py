"""Stage 12B derived requirements/risk bindings, never completion evidence."""

from pathlib import Path
from copy import deepcopy
from hashlib import sha256
import json
import shutil

import pytest
from jsonschema import Draft202012Validator

import project_system.tasking as tasking
import project_system.task_obligations as obligations_module
from project_system.task_obligations import (
    MAX_OBLIGATIONS_BYTES, PROFILE, TaskObligationsError, build_task_obligations,
    load_task_obligations, serialize_task_obligations, validate_task_obligations,
)
from project_system.task_specification import build_task_specification, serialize_task_specification
from project_system.tasking import task, bootstrap
from project_system.context import build_context
from project_system.frontmatter import read_object, write_object
from project_system.object_loader import load_object_layer
from project_system.utils import distribution_root, load_yaml

from project_system.cli import main
from project_system.init_project import init_project
from project_system.objects import create_object
from test_sync_finalize import _commit_project


def test_project_task_materializes_obligations(tmp_path, monkeypatch, capsys):
    root = init_project('Demo', tmp_path / 'demo')
    _, target = create_object(root, 'feature', 'Target', 'product', 'owner')
    _commit_project(root)
    monkeypatch.chdir(root)
    main(['task', target, '--budget', 'small'])
    output = Path(capsys.readouterr().out.strip())
    assert (output / 'task-obligations.json').is_file()


def _edit(path, **fields):
    data, body = read_object(path)
    data.update(fields)
    write_object(path, data, body)


@pytest.fixture(scope='module')
def prepared(tmp_path_factory):
    root = init_project('Demo', tmp_path_factory.mktemp('obligations-base') / 'demo')
    objects = {}
    for name, kind in [('feature', 'feature'), ('active', 'requirement'), ('other', 'requirement'),
                       ('draft', 'requirement'), ('entity', 'entity'), ('risk', 'risk'),
                       ('req-risk', 'risk'), ('unrelated-risk', 'risk')]:
        path, identity = create_object(root, kind, name, 'product', 'owner')
        path = path.rename(path.with_name(identity + '-real-slug.md'))
        objects[name] = (path.relative_to(root), identity)
    def path(name):
        return root / objects[name][0]
    def identity(name):
        return objects[name][1]
    _edit(path('active'), status='active', priority='high',
          acceptance_criteria=['  Exact text\nwith newline  ', 'duplicate', 'duplicate'],
          depends_on=[identity('other')])
    _edit(path('draft'), status='draft', acceptance_criteria=['not executable'])
    _edit(path('feature'), requirements=[identity('draft'), identity('active')], depends_on=[identity('other')])
    _edit(path('risk'), affects=[identity('feature')], severity='critical', mitigation='  Exact mitigation  ')
    _edit(path('req-risk'), affects=[identity('active'), identity('draft'), identity('active')], status='accepted')
    _edit(path('unrelated-risk'), affects=[identity('other')], depends_on=[identity('feature')])
    head = _commit_project(root)
    output, _ = task(root, identity('feature'), budget='small')
    return root, objects, head, output


@pytest.fixture
def project(prepared, tmp_path):
    source, objects, head, _ = prepared
    root = tmp_path / 'relocated'
    shutil.copytree(source, root)
    return root, {key: (root / path, identity) for key, (path, identity) in objects.items()}, head


@pytest.fixture
def document(prepared):
    return load_task_obligations(prepared[3] / 'task-obligations.json')


def _generate(project, target='feature', **kwargs):
    root, objects, _ = project
    # Independent builder fixtures, not an implicit production lifecycle reset.
    output = root / '.generated/context' / f'TASK-{objects[target][1]}-{kwargs.get("budget", "small")}'
    if output.exists():
        shutil.rmtree(output)
    output, manifest = task(root, objects[target][1], budget=kwargs.get('budget', 'small'))
    return output, load_task_obligations(output / 'task-obligations.json'), manifest


def test_exact_schema_and_persisted_spec_binding(prepared, document):
    root, objects, _, output = prepared
    assert set(document) == {'schema_version', 'profile', 'task_spec_sha256', 'requirements', 'risks', 'obligations'}
    assert document['profile'] == PROFILE and document['schema_version'] == 1
    assert document['task_spec_sha256'] == sha256((output / 'task-spec.json').read_bytes()).hexdigest()
    schema = json.loads((distribution_root() / 'schemas/task-obligations.schema.json').read_text('utf-8'))
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(document)
    for key, names in [('requirements', ['active', 'draft']), ('risks', ['risk', 'req-risk'])]:
        assert [item['id'] for item in document[key]] == sorted(objects[name][1] for name in names)
        for source in document[key]:
            assert source['sha256'] == sha256((root / source['path']).read_bytes()).hexdigest()
            assert source['path'].endswith('-real-slug.md')
    raw = (output / 'task-obligations.json').read_bytes()
    assert raw == serialize_task_obligations(document).encode('utf-8')
    assert raw.endswith(b'\n') and not raw.endswith(b'\n\n') and b'\r' not in raw


def test_feature_explicit_requirements_only_no_recursive_inheritance(prepared, document):
    objects = prepared[1]
    assert {source['id'] for source in document['requirements']} == {objects['active'][1], objects['draft'][1]}
    assert objects['other'][1] not in {source['id'] for source in document['requirements']}


def test_requirement_target_selects_itself(project):
    _, document, _ = _generate(project, 'active')
    assert [source['id'] for source in document['requirements']] == [project[1]['active'][1]]


def test_other_target_does_not_infer_requirements_from_prose_or_dependencies(project):
    path, _ = project[1]['entity']
    _edit(path, depends_on=[project[1]['active'][1]], requirements=[project[1]['active'][1]])
    _, document, _ = _generate(project, 'entity')
    assert document['requirements'] == document['obligations'] == []


@pytest.mark.parametrize('case', ['missing', 'wrong-type', 'duplicate', 'non-list', 'non-string'])
def test_feature_invalid_requirement_references_fail_closed(project, case):
    objects = project[1]
    refs = {'missing': ['REQ-20261006-deadbeef'], 'wrong-type': [objects['entity'][1]],
            'duplicate': [objects['active'][1]] * 2, 'non-list': 'not a list', 'non-string': [3]}[case]
    _edit(objects['feature'][0], requirements=refs)
    # The original Task Specification safety boundary may reject malformed schema first.
    from project_system.task_specification import TaskSpecificationError
    with pytest.raises((TaskObligationsError, TaskSpecificationError)):
        _generate(project)


def test_active_exact_indexed_criteria_nonactive_sources_and_duplicate_text(prepared, document):
    active = prepared[1]['active'][1]
    assert document['obligations'] == [
        {'id': f'{active}#acceptance-{index:04d}', 'kind': 'acceptance_criterion',
         'source_id': active, 'criterion_index': index, 'text': text}
        for index, text in enumerate(['  Exact text\nwith newline  ', 'duplicate', 'duplicate'], 1)
    ]
    assert len({item['id'] for item in document['obligations']}) == 3
    draft = next(source for source in document['requirements'] if source['status'] == 'draft')
    assert draft['acceptance_criteria'] == ['not executable']


@pytest.mark.parametrize('status', ['draft', 'proposed', 'deprecated', 'rejected'])
def test_all_nonactive_lifecycles_bind_without_obligations(project, status):
    _edit(project[1]['active'][0], status=status, deprecation_reason='fixture reason')
    _, document, _ = _generate(project, 'active')
    assert document['requirements'][0]['status'] == status
    assert document['requirements'][0]['acceptance_criteria']
    assert document['obligations'] == []


def test_missing_criteria_priority_severity_mitigation_not_invented(project):
    path = project[1]['active'][0]
    data, body = read_object(path)
    del data['acceptance_criteria'], data['priority']
    write_object(path, data, body)
    risk_path = project[1]['req-risk'][0]
    data, body = read_object(risk_path)
    data.pop('severity', None)
    data.pop('mitigation', None)
    write_object(risk_path, data, body)
    _, document, _ = _generate(project)
    source = next(item for item in document['requirements'] if item['id'] == project[1]['active'][1])
    assert source['acceptance_criteria'] == [] and source['priority'] is None
    risk = next(item for item in document['risks'] if item['id'] == project[1]['req-risk'][1])
    assert risk['severity'] is None and risk['mitigation'] is None


def test_risks_exact_affects_selection_and_bound_metadata(prepared, document):
    objects = prepared[1]
    by_id = {item['id']: item for item in document['risks']}
    assert set(by_id) == {objects['risk'][1], objects['req-risk'][1]}
    assert by_id[objects['risk'][1]]['severity'] == 'critical'
    assert by_id[objects['risk'][1]]['mitigation'] == '  Exact mitigation  '
    assert by_id[objects['req-risk'][1]]['status'] == 'accepted'
    assert by_id[objects['req-risk'][1]]['affects'] == sorted({objects['active'][1], objects['draft'][1]})
    assert all(item['kind'] == 'acceptance_criterion' for item in document['obligations'])


def test_risk_target_includes_itself_without_affects_match(project):
    _, document, _ = _generate(project, 'unrelated-risk')
    assert [item['id'] for item in document['risks']] == [project[1]['unrelated-risk'][1]]
    assert document['requirements'] == document['obligations'] == []


@pytest.mark.parametrize('status', ['open', 'mitigated', 'accepted', 'closed'])
def test_risk_lifecycle_does_not_enforce_outcome(project, status):
    _edit(project[1]['risk'][0], status=status)
    _, document, _ = _generate(project)
    assert next(item for item in document['risks'] if item['id'] == project[1]['risk'][1])['status'] == status
    assert len(document['obligations']) == 3


@pytest.mark.parametrize('name,fields', [
    ('active', {'status': 'proposed'}), ('active', {'priority': 'critical'}),
    ('active', {'acceptance_criteria': ['changed']}), ('risk', {'status': 'closed'}),
    ('risk', {'severity': 'low'}), ('risk', {'mitigation': 'changed'}),
    ('risk', {'affects': []}),
])
def test_selected_metadata_changes_artifact(project, name, fields):
    output, _, _ = _generate(project)
    before = (output / 'task-obligations.json').read_bytes()
    _edit(project[1][name][0], **fields)
    output, _, _ = _generate(project)
    assert (output / 'task-obligations.json').read_bytes() != before


@pytest.mark.parametrize('name', ['active', 'risk'])
def test_exact_source_body_bytes_change_binding_without_semantic_edits(project, name):
    output, before, _ = _generate(project)
    path = project[1][name][0]
    path.write_bytes(path.read_bytes() + b'\nBody-only byte edit\n')
    _, after, _ = _generate(project)
    assert before != after
    assert before['obligations'] == after['obligations']


def test_unrelated_source_edits_do_not_change_artifact(project):
    output, _, _ = _generate(project)
    before = (output / 'task-obligations.json').read_bytes()
    _edit(project[1]['other'][0], acceptance_criteria=['unrelated'])
    _edit(project[1]['unrelated-risk'][0], severity='critical')
    output, _, _ = _generate(project)
    assert (output / 'task-obligations.json').read_bytes() == before


def test_exact_persisted_spec_byte_change_is_bound(project):
    root, objects, _ = project
    output, before, _ = _generate(project)
    path = output / 'task-spec.json'
    path.write_bytes(path.read_bytes() + b' \n')  # valid, logically equivalent JSON
    after = build_task_obligations(root, path, objects['feature'][1])
    assert before['task_spec_sha256'] != after['task_spec_sha256']
    assert after['task_spec_sha256'] == sha256(path.read_bytes()).hexdigest()
    assert serialize_task_obligations(before) != serialize_task_obligations(after)


def test_budget_and_relocation_byte_equivalence(project, prepared):
    output, _, _ = _generate(project, budget='small')
    before = (output / 'task-obligations.json').read_bytes()
    output, _, _ = _generate(project, budget='large')
    assert before == (output / 'task-obligations.json').read_bytes()
    assert before == (prepared[3] / 'task-obligations.json').read_bytes()


def test_stage12a_spec_wire_bytes_unchanged(project):
    root, objects, head = project
    output, _, manifest = _generate(project)
    source = objects['feature'][0]
    target = {'id': objects['feature'][1], 'type': 'feature',
              'path': source.relative_to(root).as_posix(), 'sha256': sha256(source.read_bytes()).hexdigest()}
    expected = build_task_specification(load_yaml(root / 'project.yaml'), head, target, 'implement', manifest)
    assert (output / 'task-spec.json').read_bytes() == serialize_task_specification(expected).encode('utf-8')


@pytest.mark.parametrize('case', ['selected-id', 'hash', 'type', 'path', 'source-edit'])
def test_spec_target_contradictions_fail_closed(project, case):
    root, objects, _ = project
    output, _, _ = _generate(project)
    path = output / 'task-spec.json'
    spec = json.loads(path.read_bytes())
    selected = objects['feature'][1]
    if case == 'selected-id':
        selected = objects['active'][1]
    elif case == 'source-edit':
        objects['feature'][0].write_bytes(objects['feature'][0].read_bytes() + b'\nchanged\n')
    else:
        key = {'hash': 'sha256', 'type': 'type', 'path': 'path'}[case]
        spec['target'][key] = {'hash': 'a' * 64, 'type': 'requirement',
                               'path': 'knowledge/features/' + selected + '-another-slug.md'}[case]
        path.write_text(json.dumps(spec), encoding='utf-8')
    with pytest.raises(TaskObligationsError):
        build_task_obligations(root, path, selected)


def test_legacy_context_bootstrap_sync_gain_no_artifact_or_git_precondition(tmp_path):
    root = init_project('Demo', tmp_path / 'demo')
    _, identity = create_object(root, 'feature', 'Target', 'product', 'owner')
    for output, _ in [bootstrap(root), build_context(root, identity), task(root, identity, sync=True)]:
        assert not (output / 'task-obligations.json').exists()
        assert not (output / 'task-spec.json').exists()


@pytest.mark.parametrize('case', [
    'id', 'missing', 'duplicate', 'orphan', 'text', 'index', 'float-index', 'order',
    'inactive-source', 'inventory-order', 'inventory-duplicate', 'risk-order', 'risk-duplicate',
    'affects-order', 'affects-duplicate', 'source-path', 'source-id', 'source-hash',
    'status', 'priority', 'severity', 'unknown', 'nested-unknown', 'profile', 'version', 'spec-hash',
])
def test_forged_artifacts_fail_closed(document, case):
    item = document['obligations'][0]
    if case == 'id': item['id'] += 'forged'
    elif case == 'missing': document['obligations'].pop()
    elif case == 'duplicate': document['obligations'].append(deepcopy(item))
    elif case == 'orphan': item['source_id'] = 'REQ-20261006-deadbeef'
    elif case == 'text': item['text'] = item['text'].strip()
    elif case == 'index': item['criterion_index'] = 2
    elif case == 'float-index': item['criterion_index'] = 1.0
    elif case == 'order': document['obligations'].reverse()
    elif case == 'inactive-source':
        next(source for source in document['requirements'] if source['status'] == 'active')['status'] = 'draft'
    elif case == 'inventory-order': document['requirements'].reverse()
    elif case == 'inventory-duplicate': document['requirements'].append(deepcopy(document['requirements'][0]))
    elif case == 'risk-order': document['risks'].reverse()
    elif case == 'risk-duplicate': document['risks'].append(deepcopy(document['risks'][0]))
    elif case == 'affects-order':
        next(source for source in document['risks'] if len(source['affects']) == 2)['affects'].reverse()
    elif case == 'affects-duplicate': document['risks'][0]['affects'] *= 2
    elif case == 'source-path': document['requirements'][0]['path'] = '../escape.md'
    elif case == 'source-id': document['requirements'][0]['id'] = 'RISK-20261006-deadbeef'
    elif case == 'source-hash': document['requirements'][0]['sha256'] = 'F' * 64
    elif case == 'status': document['requirements'][0]['status'] = 'complete'
    elif case == 'priority': document['requirements'][0]['priority'] = 'urgent'
    elif case == 'severity': document['risks'][0]['severity'] = 'urgent'
    elif case == 'unknown': document['approved'] = True
    elif case == 'nested-unknown': item['status'] = 'PASS'
    elif case == 'profile': document['profile'] += '-future'
    elif case == 'version': document['schema_version'] = 1.0
    elif case == 'spec-hash': document['task_spec_sha256'] = 'a' * 64 + '\n'
    with pytest.raises(TaskObligationsError):
        validate_task_obligations(document)


@pytest.mark.parametrize('raw', [b'[]', b'"scalar"', b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":Infinity}',
                               b'\xff', b'{', b' ' * (MAX_OBLIGATIONS_BYTES + 1)],
                         ids=['array', 'scalar', 'duplicate-key', 'nan', 'infinity', 'utf8', 'syntax', 'oversized'])
def test_bounded_loader_rejects_bad_json(tmp_path, raw):
    path = tmp_path / 'obligations.json'
    path.write_bytes(raw)
    with pytest.raises(TaskObligationsError):
        load_task_obligations(path)


@pytest.mark.parametrize('inventory,key,value', [
    ('requirements', 'id', []), ('requirements', 'path', {}), ('requirements', 'sha256', 4),
    ('requirements', 'acceptance_criteria', [None]), ('risks', 'affects', [None]),
    ('risks', 'mitigation', {}), ('obligations', 'source_id', []), ('obligations', 'criterion_index', True),
])
def test_structurally_malformed_values_are_controlled(document, inventory, key, value):
    document[inventory][0][key] = value
    with pytest.raises(TaskObligationsError):
        validate_task_obligations(document)


def test_cli_controls_obligations_failure(project, monkeypatch, capsys):
    monkeypatch.chdir(project[0])
    def broken(*args):
        raise TaskObligationsError('controlled binding failure')
    monkeypatch.setattr(tasking, 'build_task_obligations', broken)
    with pytest.raises(SystemExit) as exc:
        main(['task', project[1]['feature'][1]])
    assert exc.value.code == 2
    assert 'task failed: controlled binding failure' in capsys.readouterr().err


def test_relevant_source_schema_and_duplicate_identity_rejected(project):
    path, identity = project[1]['active']
    other = path.with_name(identity + '-duplicate.md')
    other.write_bytes(path.read_bytes())
    with pytest.raises(TaskObligationsError):
        _generate(project)


@pytest.mark.parametrize('name,fields', [('active', {'priority': 'invalid'}), ('risk', {'severity': 'invalid'}),
                                       ('risk', {'affects': 'not a list'})])
def test_invalid_selected_source_metadata_fails_closed(project, name, fields):
    _edit(project[1][name][0], **fields)
    with pytest.raises(TaskObligationsError):
        _generate(project)


@pytest.mark.parametrize('inventory', ['requirements', 'risks'])
@pytest.mark.parametrize('path', ['../escape.md', '/absolute.md', 'C:/escape.md',
                                'knowledge/requirements/../escape.md', '.generated/escape.md'])
def test_source_paths_do_not_grant_out_of_scope_identity(document, inventory, path):
    document[inventory][0]['path'] = path
    with pytest.raises(TaskObligationsError):
        validate_task_obligations(document)


def test_source_symlink_rejected(project, tmp_path):
    root, objects, _ = project
    source = objects['active'][0]
    outside = tmp_path / 'outside.md'
    outside.write_bytes(source.read_bytes())
    source.unlink()
    try:
        source.symlink_to(outside)
    except OSError:
        pytest.skip('symlink privilege unavailable')
    with pytest.raises(TaskObligationsError):
        _generate(project)


def test_no_canonical_writes_during_task_generation(project):
    root = project[0]
    paths = [root / 'project.yaml', *root.joinpath('knowledge').rglob('*.md'), *root.joinpath('docs').rglob('*.md')]
    before = {path: path.read_bytes() for path in paths}
    _generate(project)
    assert before == {path: path.read_bytes() for path in paths}


def test_bad_risk_parse_is_not_silently_excluded(project):
    project[1]['risk'][0].write_bytes(b'---\ninvalid: [\n---\n')
    with pytest.raises(TaskObligationsError, match='risk inventory'):
        _generate(project)


def test_criteria_locator_limit_fails_closed(project):
    _edit(project[1]['active'][0], acceptance_criteria=['x'] * 10000)
    with pytest.raises(TaskObligationsError):
        _generate(project)


def test_persisted_spec_drift_during_trusted_load_rejected(project, monkeypatch):
    import project_system.task_obligations as module
    output, _, _ = _generate(project)
    original = module.load_task_specification
    def drift(path):
        value = original(path)
        path.write_bytes(path.read_bytes() + b'\n')
        return value
    monkeypatch.setattr(module, 'load_task_specification', drift)
    with pytest.raises(TaskObligationsError, match='changed during binding'):
        build_task_obligations(project[0], output / 'task-spec.json', project[1]['feature'][1])


def test_metadata_drift_during_source_snapshot_rejected(project, monkeypatch):
    import project_system.task_obligations as module
    output, _, _ = _generate(project)
    original = module.snapshot_task_target
    def drift(root, layer, identity):
        binding = original(root, layer, identity)
        if identity == project[1]['active'][1]:
            _edit(project[1]['active'][0], acceptance_criteria=['changed after snapshot'])
        return binding
    monkeypatch.setattr(module, 'snapshot_task_target', drift)
    with pytest.raises(TaskObligationsError, match='changed during task binding'):
        build_task_obligations(project[0], output / 'task-spec.json', project[1]['feature'][1])


def _limit_document(count, status='active'):
    identity = 'REQ-20261006-deadbeef'
    return {
        'schema_version': 1, 'profile': PROFILE, 'task_spec_sha256': 'a' * 64,
        'requirements': [{
            'id': identity, 'path': f'knowledge/requirements/{identity}.md',
            'sha256': 'b' * 64, 'status': status, 'priority': None,
            'acceptance_criteria': [''] * count,
        }], 'risks': [], 'obligations': [],
    }


def _forbid_criterion_expansion(monkeypatch):
    calls = []

    def forbidden(*args, **kwargs):
        calls.append(True)
        raise AssertionError('criterion expansion started before the active-source limit check')

    # Schema validation may inspect strings, but the generation boundary must
    # reject overflow before even enumerating criteria to construct dictionaries.
    monkeypatch.setattr(obligations_module, 'enumerate', forbidden, raising=False)
    return calls


@pytest.mark.parametrize('preceding_valid_source', [False, True], ids=['single-source', 'later-overflow'])
def test_early_bound_rejects_active_overflow_before_any_expansion(monkeypatch, preceding_valid_source):
    sources = _limit_document(10000)['requirements']
    if preceding_valid_source:
        sources.insert(0, _limit_document(1)['requirements'][0])
    calls = _forbid_criterion_expansion(monkeypatch)
    with pytest.raises(TaskObligationsError, match='active requirement.*9999'):
        obligations_module._obligations(sources)
    assert calls == []


@pytest.mark.parametrize('missing_inventory', [False, True], ids=['empty-inventory', 'missing-inventory'])
def test_early_bound_forged_validator_artifact_never_expands(monkeypatch, missing_inventory):
    document = _limit_document(10000)
    if missing_inventory:
        del document['obligations']
    calls = _forbid_criterion_expansion(monkeypatch)
    with pytest.raises(TaskObligationsError):
        validate_task_obligations(document)
    assert calls == []


def test_early_bound_persisted_loader_never_expands(tmp_path, monkeypatch):
    path = tmp_path / 'forged-obligations.json'
    raw = (json.dumps(_limit_document(10000), sort_keys=True, indent=2) + '\n').encode('utf-8')
    assert len(raw) < MAX_OBLIGATIONS_BYTES
    path.write_bytes(raw)
    calls = _forbid_criterion_expansion(monkeypatch)
    with pytest.raises(TaskObligationsError, match='active requirement.*9999'):
        load_task_obligations(path)
    assert calls == []


def test_early_bound_builder_never_expands(project, monkeypatch):
    _edit(project[1]['active'][0], acceptance_criteria=[''] * 10000)
    calls = _forbid_criterion_expansion(monkeypatch)
    with pytest.raises(TaskObligationsError, match='active requirement.*9999'):
        _generate(project)
    assert calls == []


def test_locator_boundary_9999_active_roundtrip_is_valid(tmp_path):
    document = _limit_document(9999)
    document['obligations'] = obligations_module._obligations(document['requirements'])
    assert len(document['obligations']) == 9999
    assert document['obligations'][-1]['id'] == 'REQ-20261006-deadbeef#acceptance-9999'
    assert document['obligations'][-1]['criterion_index'] == 9999
    assert len({item['id'] for item in document['obligations']}) == 9999
    assert all(item['text'] == '' for item in document['obligations'])
    assert validate_task_obligations(document) is document
    raw = serialize_task_obligations(document).encode('utf-8')
    assert len(raw) < MAX_OBLIGATIONS_BYTES
    path = tmp_path / 'boundary-obligations.json'
    path.write_bytes(raw)
    assert load_task_obligations(path) == document


@pytest.mark.parametrize('status', ['draft', 'proposed', 'deprecated', 'rejected'])
def test_locator_boundary_10000_nonactive_builder_and_loader_retain_sources(project, status):
    _edit(project[1]['active'][0], status=status, deprecation_reason='fixture reason',
          acceptance_criteria=[''] * 10000)
    output, document, _ = _generate(project, 'active', budget='large')
    assert len(document['requirements'][0]['acceptance_criteria']) == 10000
    assert document['requirements'][0]['acceptance_criteria'] == [''] * 10000
    assert document['requirements'][0]['status'] == status
    assert document['obligations'] == []
    assert (output / 'task-obligations.json').stat().st_size < MAX_OBLIGATIONS_BYTES
    assert load_task_obligations(output / 'task-obligations.json') == document


def test_locator_repair_preserves_normal_obligations_and_task_spec_bytes(project, monkeypatch):
    fixed = obligations_module._obligations

    def pre_repair(requirements):
        # Freeze the exact pre-repair expansion for ordinary bounded sources.
        return [
            {'id': f'{source["id"]}#acceptance-{index:04d}',
             'kind': 'acceptance_criterion', 'source_id': source['id'],
             'criterion_index': index, 'text': text}
            for source in requirements if source['status'] == 'active'
            for index, text in enumerate(source['acceptance_criteria'], 1)
        ]

    monkeypatch.setattr(obligations_module, '_obligations', pre_repair)
    output, before, _ = _generate(project)
    old_obligations = (output / 'task-obligations.json').read_bytes()
    old_spec = (output / 'task-spec.json').read_bytes()
    monkeypatch.setattr(obligations_module, '_obligations', fixed)
    output, after, _ = _generate(project)
    assert (output / 'task-obligations.json').read_bytes() == old_obligations
    assert (output / 'task-spec.json').read_bytes() == old_spec
    assert before == after
    assert after['obligations'][1]['text'] == after['obligations'][2]['text'] == 'duplicate'
    assert after['obligations'][1]['id'] != after['obligations'][2]['id']
