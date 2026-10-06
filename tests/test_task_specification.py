"""Stage 12A: derived task contracts, not task completion or obligations."""

from pathlib import Path
from copy import deepcopy
from hashlib import sha256
import json
import shutil
import subprocess
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator

import project_system.task_specification as specification

from project_system.cli import main
from project_system.init_project import init_project
from project_system.objects import create_object
from project_system.context import build_context
from project_system.frontmatter import read_object, write_object
from project_system.object_loader import load_object_layer
from project_system.task_specification import (
    TaskSpecificationError, build_task_specification, load_task_specification,
    serialize_task_specification, snapshot_task_target, task_git_head,
    validate_task_specification,
)
from project_system.tasking import bootstrap, task
from project_system.utils import distribution_root, load_yaml
from test_sync_finalize import _commit_project, _git


def test_project_task_materializes_spec_without_changing_cli_output(tmp_path, monkeypatch, capsys):
    root = init_project('Demo', tmp_path / 'demo')
    _, target = create_object(root, 'feature', 'Target', 'product', 'owner')
    _commit_project(root)
    monkeypatch.chdir(root)
    main(['task', target, '--budget', 'small'])
    output = Path(capsys.readouterr().out.strip())
    assert output == root / '.generated/context' / f'TASK-{target}-small'
    assert (output / 'task-spec.json').is_file()
    assert (output / 'manifest.json').is_file() and (output / 'context.md').is_file()


@pytest.fixture(scope='module')
def prepared(tmp_path_factory):
    root = init_project('Demo', tmp_path_factory.mktemp('task-base') / 'demo')
    path, target = create_object(root, 'feature', 'Target', 'product', 'owner')
    path = path.rename(path.with_name(f'{target}-real-slug.md'))
    head = _commit_project(root)
    output, manifest = task(root, target, budget='small')
    return root, path.relative_to(root), target, head, output, manifest


@pytest.fixture
def project(prepared, tmp_path):
    source, relative, target, head, _, _ = prepared
    root = tmp_path / 'relocated-demo'
    shutil.copytree(source, root)
    return root, root / relative, target, head


@pytest.fixture
def document(prepared):
    return load_task_specification(prepared[4] / 'task-spec.json')


def _bytes(root, target, **kwargs):
    # These Stage 12A tests compare independently prepared contracts. Stage 12C
    # deliberately forbids resetting an existing lifecycle in production.
    output = root / '.generated/context' / f'TASK-{target}-{kwargs.get("budget", "medium")}'
    if output.exists():
        shutil.rmtree(output)
    output, manifest = task(root, target, **kwargs)
    return (output / 'task-spec.json').read_bytes(), manifest


def test_exact_contract_and_existing_manifest_evidence(prepared, document):
    root, relative, target, head, output, manifest = prepared
    assert set(document) == {
        'schema_version', 'profile', 'project_id', 'base_commit', 'target', 'mode',
        'verification_checkpoint', 'write_scope', 'skills',
    }
    assert document['schema_version'] == 1
    assert document['profile'] == 'project-system-task-spec-v1'
    assert document['verification_checkpoint'] == 'task_verify'
    assert document['project_id'] == load_yaml(root / 'project.yaml')['project']['id']
    assert document['base_commit'] == head == _git(root, 'rev-parse', 'HEAD')
    assert document['target'] == {
        'id': target, 'type': 'feature', 'path': relative.as_posix(),
        'sha256': sha256((root / relative).read_bytes()).hexdigest(),
    }
    assert document['mode'] == manifest['mode'] == 'implement'
    assert document['write_scope'] == {
        'canonical': sorted(set(manifest['task_write_scope']['canonical'])),
        'effective': sorted(set(manifest['effective_write_scope'])),
    }
    assert relative.as_posix() in document['write_scope']['canonical']
    assert document['skills'] == {
        'registry_sha256': manifest['skills_registry_sha256'],
        'selected': sorted(manifest['selected_skills'], key=lambda skill: skill['name']),
    }
    assert document['skills']['registry_sha256'] == sha256((root / '.project/skills.yaml').read_bytes()).hexdigest()
    for selected in document['skills']['selected']:
        assert selected['sha256'] == sha256((root / selected['path']).read_bytes()).hexdigest()
    assert (output / 'manifest.json').read_text(encoding='utf-8')
    schema = json.loads((distribution_root() / 'schemas/task-spec.schema.json').read_text(encoding='utf-8'))
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(document)


def test_repeated_generation_budget_independence_and_portability(project, prepared):
    root, _, target, _ = project
    small, manifest = _bytes(root, target, budget='small')
    again, _ = _bytes(root, target, budget='small')
    large, _ = _bytes(root, target, budget='large')
    assert small == again == large == (prepared[4] / 'task-spec.json').read_bytes()
    assert small.endswith(b'\n') and not small.endswith(b'\n\n') and b'\r' not in small
    assert serialize_task_specification(json.loads(small)).encode('utf-8') == small
    assert str(root).encode() not in small and root.as_posix().encode() not in small
    assert b'.generated' not in small and b'budget' not in small and b'task_spec_sha256' not in small
    assert manifest['budget'] == 'small'
    output, current = task(root, target, budget='small')
    task_context = (output / 'context.md').read_bytes()
    _, direct = build_context(
        root, target, 'small', 'implement', kind='task',
        allowed_write_set=current['allowed_write_set'],
        skill_names=[skill['name'] for skill in current['selected_skills']],
    )
    assert direct == current
    assert (output / 'context.md').read_bytes() == task_context


def test_dirty_target_exact_bytes_change_spec_without_changing_head(project):
    root, path, target, head = project
    before, _ = _bytes(root, target)
    path.write_bytes(path.read_bytes() + '\nУточнение байтов.\n'.encode('utf-8'))
    after, _ = _bytes(root, target)
    assert before != after
    current = json.loads(after)
    assert current['base_commit'] == head
    assert current['target']['sha256'] == sha256(path.read_bytes()).hexdigest()


def test_mode_change_and_strict_normalization(project):
    root, _, target, _ = project
    before, _ = _bytes(root, target)
    after, _ = _bytes(root, target, mode=' review ')
    assert before != after
    assert json.loads(after)['mode'] == 'review'
    custom, _ = _bytes(root, target, mode='owner-defined mode')
    assert json.loads(custom)['mode'] == 'owner-defined mode'


@pytest.mark.parametrize('mode', ['', '   ', None, 1, ['implement'], '\x00'])
def test_invalid_mode_fails_closed(project, mode):
    root, _, target, _ = project
    with pytest.raises(TaskSpecificationError):
        task(root, target, mode=mode)


def test_head_change_alone_changes_binding(project):
    root, _, target, head = project
    before, _ = _bytes(root, target)
    _git(root, 'commit', '--allow-empty', '-qm', 'fixture head only')
    after, _ = _bytes(root, target)
    assert before != after
    assert json.loads(after)['base_commit'] == _git(root, 'rev-parse', 'HEAD') != head
    assert json.loads(before)['target'] == json.loads(after)['target']


@pytest.mark.parametrize('part', ['content', 'registry'])
def test_skill_content_and_registry_identity_change_binding(project, part):
    root, _, target, _ = project
    before, _ = _bytes(root, target)
    path = root / ('.agents/skills/knowledge-sync/SKILL.md' if part == 'content' else '.project/skills.yaml')
    path.write_bytes(path.read_bytes() + b'\n# task binding regression\n')
    after, _ = _bytes(root, target)
    assert before != after
    key = 'registry_sha256' if part == 'registry' else 'selected'
    assert json.loads(before)['skills'][key] != json.loads(after)['skills'][key]


@pytest.mark.parametrize('part', ['canonical', 'effective'])
def test_write_scope_change_changes_contract(prepared, document, part):
    root, _, _, head, _, original = prepared
    manifest = deepcopy(original)
    scope = manifest['task_write_scope']['canonical'] if part == 'canonical' else manifest['effective_write_scope']
    if part == 'canonical':
        scope.append('docs/06_GLOSSARY.md')
    else:
        # Change the effective contract without exceeding its canonical ceiling.
        assert scope
        scope.pop()
    changed = build_task_specification(load_yaml(root / 'project.yaml'), head, document['target'], 'implement', manifest)
    assert changed['write_scope'][part] == sorted(set(scope))
    assert serialize_task_specification(changed) != serialize_task_specification(document)


def test_manifest_order_and_duplicates_are_normalized_without_mutation(prepared, document):
    root, _, _, head, _, original = prepared
    manifest = deepcopy(original)
    for scope in (manifest['task_write_scope']['canonical'], manifest['effective_write_scope']):
        scope[:] = list(reversed(scope)) + scope[:1]
    manifest['selected_skills'].reverse()
    prior = deepcopy(manifest)
    built = build_task_specification(load_yaml(root / 'project.yaml'), head, document['target'], 'implement', manifest)
    assert built == document and manifest == prior


def test_selected_skill_identity_change_changes_contract(prepared, document):
    root, _, _, head, _, original = prepared
    manifest = deepcopy(original)
    manifest['selected_skills'].append({
        'name': 'project-validation', 'path': '.agents/skills/project-validation/SKILL.md',
        'sha256': sha256((root / '.agents/skills/project-validation/SKILL.md').read_bytes()).hexdigest(),
    })
    changed = build_task_specification(load_yaml(root / 'project.yaml'), head, document['target'], 'implement', manifest)
    assert serialize_task_specification(changed) != serialize_task_specification(document)


def test_target_identity_path_and_type_are_not_filename_inventions(project):
    root, path, target, _ = project
    before, _ = _bytes(root, target)
    new_path = path.rename(path.with_name(f'{target}-changed-slug.md'))
    changed, _ = _bytes(root, target)
    assert before != changed
    assert json.loads(changed)['target']['id'] == target
    assert json.loads(changed)['target']['path'] == new_path.relative_to(root).as_posix()
    assert json.loads(before)['target']['sha256'] == json.loads(changed)['target']['sha256']
    _, second = create_object(root, 'question', 'Other type', 'product', 'owner')
    other, _ = _bytes(root, second)
    assert other != changed and json.loads(other)['target']['type'] == 'question'


@pytest.mark.parametrize('fault', ['missing', 'duplicate', 'filename', 'schema'])
def test_invalid_canonical_target_fails_closed(project, fault):
    root, path, target, _ = project
    if fault == 'missing':
        path.unlink()
    elif fault == 'duplicate':
        path.with_name(f'{target}-duplicate.md').write_bytes(path.read_bytes())
    elif fault == 'filename':
        path.rename(path.with_name('arbitrary-name.md'))
    else:
        data, body = read_object(path)
        data['type'] = 'question'
        write_object(path, data, body)
    with pytest.raises(TaskSpecificationError):
        task(root, target)


def test_symlink_target_is_not_authoritative(project):
    root, path, target, _ = project
    destination = root / 'retained-copy.md'
    destination.write_bytes(path.read_bytes())
    path.unlink()
    try:
        path.symlink_to(destination)
    except OSError:
        pytest.skip('symlink creation privilege unavailable')
    with pytest.raises(TaskSpecificationError):
        snapshot_task_target(root, load_object_layer(root), target)


def test_git_binding_uses_bounded_shell_false_probe(tmp_path, monkeypatch):
    calls = []
    def probe(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(stdout='a' * 40 + '\r\n', returncode=0)
    monkeypatch.setattr(specification, 'run_process', probe)
    assert task_git_head(tmp_path) == 'a' * 40
    args, options = calls[0]
    assert args == ['git', 'rev-parse', '--verify', 'HEAD^{commit}']
    assert options['shell'] is False
    assert options['timeout'] == 30
    assert options['max_capture_bytes'] == 65536
    assert options['encoding'] == 'utf-8'


@pytest.mark.parametrize('output,code', [
    ('', 0), ('a' * 40, 1), ('A' * 40, 0), ('a' * 39, 0),
    ('a' * 41, 0), (' ' + 'a' * 40, 0), ('a' * 40 + '\n\n', 0),
    ('a' * 40 + '\n' + 'b' * 40, 0), (None, 0), ('a' * 40, False),
])
def test_malformed_git_identity_fails_closed(tmp_path, monkeypatch, output, code):
    monkeypatch.setattr(specification, 'run_process', lambda *a, **kw: SimpleNamespace(stdout=output, returncode=code))
    with pytest.raises(TaskSpecificationError):
        task_git_head(tmp_path)


@pytest.mark.parametrize('error', [FileNotFoundError('git'), subprocess.TimeoutExpired('git', 30), OSError('transport')])
def test_missing_git_or_failed_probe_has_controlled_cli_error(project, monkeypatch, capsys, error):
    root, _, target, _ = project
    def fail(*args, **kwargs):
        raise error
    monkeypatch.setattr(specification, 'run_process', fail)
    monkeypatch.chdir(root)
    with pytest.raises(SystemExit) as exited:
        main(['task', target])
    assert exited.value.code == 2
    output = capsys.readouterr().err
    assert 'task failed:' in output and 'Git HEAD' in output and 'Traceback' not in output


def test_unborn_or_absent_git_repository_is_rejected(tmp_path):
    root = init_project('Demo', tmp_path / 'demo')
    _, target = create_object(root, 'feature', 'Target', 'product', 'owner')
    with pytest.raises(TaskSpecificationError):
        task(root, target)
    _git(root, 'init', '-q')
    with pytest.raises(TaskSpecificationError):
        task(root, target)


@pytest.mark.parametrize('config', [[], 'scalar', 42, {}, {'project': []}, {'project': {'id': ''}}, {'project': {'id': 'Demo'}}, {'project': {'id': 'demo\n'}}])
def test_project_identity_cannot_be_invented_or_normalized(config):
    with pytest.raises(TaskSpecificationError):
        specification.task_project_id(config)


@pytest.mark.parametrize('missing', [
    'schema_version', 'profile', 'project_id', 'base_commit', 'target', 'mode',
    'verification_checkpoint', 'write_scope', 'skills',
])
def test_missing_contract_field_rejected(document, missing):
    del document[missing]
    with pytest.raises(TaskSpecificationError):
        validate_task_specification(document)


@pytest.mark.parametrize('location,key,value', [
    ((), 'extra', True), ((), 'task_spec_sha256', 'a' * 64),
    ((), 'schema_version', 2), ((), 'schema_version', True),
    ((), 'profile', 'other'), ((), 'verification_checkpoint', 'sync_verify'),
    ((), 'base_commit', 'A' * 40), ((), 'base_commit', 'a' * 40 + '\n'),
    ((), 'base_commit', None), ((), 'mode', ''), ((), 'mode', ' implement '),
    (('target',), 'sha256', 'x' * 64), (('target',), 'type', 'task'),
    (('target',), 'extra', 1), (('skills',), 'registry_sha256', None),
    (('skills',), 'selected', {}), (('skills', 'selected', 0), 'name', []),
    (('skills', 'selected', 0), 'name', 'Other'),
    (('skills', 'selected', 0), 'path', '.agents/skills/other/SKILL.md'),
    (('skills', 'selected', 0), 'sha256', 'b' * 63),
    (('write_scope',), 'canonical', {}), (('write_scope',), 'effective', [None]),
])
def test_malformed_contract_is_controlled_validation_error(document, location, key, value):
    current = document
    for component in location:
        current = current[component]
    current[key] = value
    with pytest.raises(TaskSpecificationError):
        validate_task_specification(document)


@pytest.mark.parametrize('path', [
    '/knowledge/features/target.md', 'C:/target.md', '../target.md',
    'knowledge/../target.md', 'knowledge//target.md', 'knowledge/./target.md',
    'knowledge\\features\\target.md', '.git/config', '.generated/context/target.md',
    'knowledge/features/arbitrary-name.md', 'knowledge/features/FEAT-20260101-deadbeef.md',
])
def test_unsafe_or_mismatched_target_path_rejected(document, path):
    document['target']['path'] = path
    with pytest.raises(TaskSpecificationError):
        validate_task_specification(document)


@pytest.mark.parametrize('pattern', [
    '/docs/**', 'C:/docs/**', '../docs/**', 'docs/../**', '.git/**',
    '.GIT/**', '.generated/**', '**', '*/**', 'docs\\**', 'docs//**',
    'docs/./**', 'docs/\x00file', 'docs/\nfile',
])
@pytest.mark.parametrize('scope', ['canonical', 'effective'])
def test_unsafe_scope_rejected(document, pattern, scope):
    document['write_scope'][scope] = [pattern]
    with pytest.raises(TaskSpecificationError):
        validate_task_specification(document)


@pytest.mark.parametrize('scope', ['canonical', 'effective'])
@pytest.mark.parametrize('paths', [['docs/z.md', 'docs/a.md'], ['docs/a.md', 'docs/a.md']])
def test_unsorted_or_duplicate_persisted_scope_rejected(document, scope, paths):
    document['write_scope'][scope] = paths
    with pytest.raises(TaskSpecificationError):
        validate_task_specification(document)


@pytest.mark.parametrize('fault', ['order', 'duplicate'])
def test_unsorted_or_duplicate_selected_skills_rejected(document, fault):
    selected = document['skills']['selected']
    assert len(selected) >= 2
    if fault == 'order':
        selected.reverse()
    else:
        same_name = deepcopy(selected[0])
        same_name['sha256'] = 'c' * 64
        selected.insert(1, same_name)
    with pytest.raises(TaskSpecificationError):
        validate_task_specification(document)


@pytest.mark.parametrize('raw', [b'[]', b'"scalar"', b'{"schema_version":1,"schema_version":1}', b'{"x":NaN}', b'\xff', b'{', b' ' * (specification.MAX_SPEC_BYTES + 1)],
                         ids=['array', 'scalar', 'duplicate-key', 'nan', 'invalid-utf8', 'syntax', 'oversized'])
def test_bounded_loader_rejects_malformed_json(tmp_path, raw):
    path = tmp_path / 'task-spec.json'
    path.write_bytes(raw)
    with pytest.raises(TaskSpecificationError):
        load_task_specification(path)


def test_legacy_sync_bootstrap_and_context_do_not_gain_task_preconditions(tmp_path):
    root = init_project('Demo', tmp_path / 'demo')
    _, target = create_object(root, 'feature', 'Target', 'product', 'owner')
    sync_output, sync_manifest = task(root, target, sync=True, mode='sync')
    bootstrap_output, _ = bootstrap(root)
    context_output, _ = build_context(root, target)
    assert sync_manifest['target'] == target
    for output in (sync_output, bootstrap_output, context_output):
        assert not (output / 'task-spec.json').exists()


@pytest.mark.parametrize('canonical,effective,accepted', [
    (['knowledge/features/FEAT-20261006-deadbeef.md'], ['docs/**'], False),
    (['docs/product/**'], ['docs/**'], False),
    (['docs/product/**'], ['docs/product2/file.md'], False),
    (['docs/product'], ['docs/product/**'], False),
    (['docs/**'], ['docs/product/**'], True),
    (['knowledge/features/FEAT-20261006-deadbeef.md'],
     ['knowledge/features/FEAT-20261006-deadbeef.md'], True),
    (['docs/product/**'], ['docs/product/**'], True),
    (['docs/product/**'], ['docs/product'], True),
    (['docs/product/**'], [], True),
    ([], [], True),
    (['docs/product/**', 'knowledge/features/FEAT-20261006-deadbeef.md'],
     ['knowledge/features/FEAT-20261006-deadbeef.md'], True),
], ids=['disjoint', 'broader-tree', 'prefix-neighbor', 'exact-cannot-grant-tree',
        'narrower-tree', 'equal-exact', 'equal-tree', 'tree-base', 'empty-effective',
        'empty-both', 'one-containing-canonical'])
def test_task_scope_authority_containment(document, canonical, effective, accepted):
    document['write_scope'] = {'canonical': canonical, 'effective': effective}
    if accepted:
        assert validate_task_specification(document) is document
    else:
        with pytest.raises(TaskSpecificationError, match='effective.*canonical'):
            validate_task_specification(document)


@pytest.mark.parametrize('pattern', ['docs/*.md', 'docs/file?.md', 'docs/[ab].md', 'docs/**/nested/**'])
@pytest.mark.parametrize('scope', ['canonical', 'effective'])
def test_task_scope_rejects_unsupported_skill_globs(document, pattern, scope):
    document['write_scope'] = {'canonical': ['docs/**'], 'effective': []}
    document['write_scope'][scope] = [pattern]
    with pytest.raises(TaskSpecificationError, match='write_scope'):
        validate_task_specification(document)


def test_task_scope_builder_rejects_forged_manifest(prepared, document):
    root, relative, _, head, _, original = prepared
    manifest = deepcopy(original)
    manifest['task_write_scope']['canonical'] = [relative.as_posix()]
    manifest['effective_write_scope'] = ['docs/**']
    with pytest.raises(TaskSpecificationError, match='effective.*canonical'):
        build_task_specification(load_yaml(root / 'project.yaml'), head, document['target'], 'implement', manifest)


def test_task_scope_loader_rejects_persisted_authority_expansion(tmp_path, document):
    document['write_scope'] = {'canonical': [document['target']['path']], 'effective': ['docs/**']}
    path = tmp_path / 'forged-spec.json'
    # Deliberately bypass serializer/validator: model a forged persisted input.
    path.write_text(json.dumps(document), encoding='utf-8')
    with pytest.raises(TaskSpecificationError, match='effective.*canonical'):
        load_task_specification(path)


def test_task_scope_repair_preserves_candidate_serialization_and_bindings(project):
    root, path, target, head = project
    small, manifest = _bytes(root, target, budget='small')
    expected = {
        'schema_version': 1, 'profile': 'project-system-task-spec-v1', 'project_id': 'demo',
        'base_commit': head,
        'target': {'id': target, 'type': 'feature', 'path': path.relative_to(root).as_posix(),
                   'sha256': sha256(path.read_bytes()).hexdigest()},
        'mode': 'implement', 'verification_checkpoint': 'task_verify',
        'write_scope': {'canonical': sorted(set(manifest['task_write_scope']['canonical'])),
                        'effective': sorted(set(manifest['effective_write_scope']))},
        'skills': {'registry_sha256': manifest['skills_registry_sha256'],
                   'selected': sorted(manifest['selected_skills'], key=lambda item: item['name'])},
    }
    # Freeze the pre-repair wire contract, independently of the new validator.
    candidate_bytes = (json.dumps(expected, sort_keys=True, indent=2, ensure_ascii=False,
                                 allow_nan=False) + '\n').encode('utf-8')
    large, _ = _bytes(root, target, budget='large')
    assert small == large == candidate_bytes
