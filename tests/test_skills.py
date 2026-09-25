import json
import os
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

import pytest
import yaml

from project_system.context import build_context
from project_system.cli import main
from project_system.generation import generate
from project_system.init_project import init_project
from project_system.objects import create_object
from project_system.skills import (
    CORE_SKILLS,
    DESIGN_SKILL,
    SKILLS_ERA_PROJECT_CLI_VERSION,
    SkillError,
    SkillRecord,
    effective_write_scope,
    inspect_skill_layer,
    install_skills,
)
from project_system.sync_finalization import SyncFinalizeIntegrityError, finalize_sync
from project_system.sync_planning import (
    SyncPlanError,
    artifact_integrity_block,
    plan_sync,
)
from project_system.sync_verification import SyncIntegrityError, verify_sync
from project_system.tasking import bootstrap, task
from project_system.utils import distribution_root, load_yaml
from project_system.validation import validate


SKILL_FIELDS = {
    'selected_skills',
    'skills_registry_sha256',
    'task_write_scope',
    'effective_write_scope',
    'skill_write_authorizations',
}


def _write_yaml(path, value):
    path.write_text(
        yaml.safe_dump(value, sort_keys=False, allow_unicode=True),
        encoding='utf-8',
    )


def _registry(root):
    return load_yaml(root / '.project' / 'skills.yaml')


def _write_registry(root, value):
    _write_yaml(root / '.project' / 'skills.yaml', value)


def _fatal(root):
    return [item for item in validate(root) if item[0] in {'BLOCKING', 'ERROR'}]


def _git(root, *args):
    result = subprocess.run(
        ['git', *args], cwd=root, check=True, capture_output=True, text=True,
    )
    return result.stdout.strip()


def _commit(root):
    if not (root / '.git').exists():
        _git(root, 'init', '-q')
        _git(root, 'config', 'user.email', 'skills-tests@example.invalid')
        _git(root, 'config', 'user.name', 'Skills Tests')
    _git(root, 'add', '.')
    _git(root, 'commit', '-qm', 'fixture')
    return _git(root, 'rev-parse', 'HEAD')


def _legacy_project(root):
    shutil.rmtree(root / '.agents')
    (root / '.project' / 'skills.yaml').unlink()
    config = load_yaml(root / 'project.yaml')
    config['tooling'].pop('skills_schema_version')
    config['tooling']['project_cli'] = '0.11.0'
    _write_yaml(root / 'project.yaml', config)
    migration = distribution_root() / 'skills' / 'migrations' / 'v0.11'
    shutil.copy2(migration / 'AGENTS.md', root / 'AGENTS.md')
    shutil.copy2(migration / 'PROJECT_RULES.md', root / 'PROJECT_RULES.md')


def _pack(head, object_id, pack_id='SYNC-20260923-acde1234'):
    return {
        'schema_version': 1,
        'pack_id': pack_id,
        'project_id': 'demo',
        'source': {'type': 'approved_discussion', 'ref': 'skills-tests'},
        'created_at': '2026-09-23T12:00:00+03:00',
        'base_commit': head,
        'approval': {
            'approved_by': 'project-owner',
            'approved_at': '2026-09-23T12:01:00+03:00',
        },
        'change_class': 'C',
        'changes': [{
            'change_id': 'update-target',
            'kind': 'update_object',
            'summary': 'Apply an already approved clarification.',
            'target_id': object_id,
            'patch': {'body': 'Approved body.'},
        }],
        'expected_targets': [object_id],
        'notes': 'Skills evidence test.',
    }


def _planned_project(tmp_path, object_type='feature'):
    root = init_project('Demo', tmp_path / 'demo')
    path, object_id = create_object(root, object_type, 'Target', 'general', 'owner')
    path = path.rename(path.with_name(f'{object_id}-target.md'))
    head = _commit(root)
    pack = _pack(head, object_id)
    pack_path = root / 'inbox' / 'sync' / f'{pack["pack_id"]}.yaml'
    _write_yaml(pack_path, pack)
    output, manifest = plan_sync(root, pack_path)
    return root, path, pack_path, output, manifest


def _legacy_planned_project(tmp_path):
    root = init_project('Demo', tmp_path / 'legacy-demo')
    _legacy_project(root)
    path, object_id = create_object(root, 'feature', 'Target', 'general', 'owner')
    path = path.rename(path.with_name(f'{object_id}-target.md'))
    head = _commit(root)
    pack = _pack(head, object_id, 'SYNC-20260923-acde5678')
    pack_path = root / 'inbox' / 'sync' / f'{pack["pack_id"]}.yaml'
    _write_yaml(pack_path, pack)
    output, manifest = plan_sync(root, pack_path)
    return root, path, pack_path, output, manifest


def _rewrite_integrity(output):
    plan_path = output / 'plan.json'
    manifest_path = output / 'manifest.json'
    plan = json.loads(plan_path.read_text(encoding='utf-8'))
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    integrity = artifact_integrity_block(plan, manifest)
    plan['artifact_integrity'] = integrity
    manifest['artifact_integrity'] = integrity
    plan_path.write_text(json.dumps(plan, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + '\n', encoding='utf-8')


def _malform_skills_evidence(document, case):
    if case == 'selected_skills':
        document['selected_skills'] = {'name': 'knowledge-sync'}
    elif case == 'selected_skills.name':
        document['selected_skills'][0]['name'] = ['knowledge-sync']
    elif case == 'selected_skills.path':
        document['selected_skills'][0]['path'] = 42
    elif case == 'selected_skills.sha256':
        document['selected_skills'][0]['sha256'] = None
    elif case == 'skills_registry_sha256':
        document['skills_registry_sha256'] = {'digest': 'invalid'}
    elif case == 'skill_write_authorizations':
        path = next(iter(document['skill_write_authorizations']))
        document['skill_write_authorizations'][path] = [['knowledge-sync']]
    elif case == 'effective_write_scope':
        document['effective_write_scope'] = [{'path': 'knowledge/**'}]
    elif case == 'task_write_scope':
        document['task_write_scope']['canonical'] = {'path': 'knowledge/**'}
    else:  # pragma: no cover - test helper guard
        raise AssertionError(f'unknown malformed evidence case: {case}')


def test_init_materializes_valid_core_skills_and_generated_index(tmp_path):
    root = init_project('Demo', tmp_path / 'demo')
    config = load_yaml(root / 'project.yaml')
    layer = inspect_skill_layer(root, config)

    assert config['tooling']['skills_schema_version'] == 1
    assert layer.active is True
    assert set(layer.records) == set(CORE_SKILLS)
    assert DESIGN_SKILL not in layer.records
    assert not [item for item in layer.issues if item[0] in {'BLOCKING', 'ERROR'}]
    generate(root)
    assert (root / '.generated' / 'indexes' / 'SKILLS.md').is_file()


def test_legacy_project_without_marker_is_warning_only(tmp_path):
    root = init_project('Demo', tmp_path / 'demo')
    _legacy_project(root)

    layer = inspect_skill_layer(root)
    assert layer.active is False
    assert ('WARNING', 'project.yaml', 'legacy project has no Skills v1 layer; run project skills install') in layer.issues
    assert not _fatal(root)


def test_activation_marker_missing_registry_is_blocking(tmp_path):
    root = init_project('Demo', tmp_path / 'demo')
    (root / '.project' / 'skills.yaml').unlink()
    shutil.rmtree(root / '.agents' / 'skills')

    issues = inspect_skill_layer(root).issues
    assert any(severity == 'BLOCKING' and 'missing its registry' in message for severity, _, message in issues)


@pytest.mark.parametrize('marker', [True, False, '1', 1.0, 2, None])
def test_activation_marker_requires_exact_integer_one(tmp_path, marker):
    root = init_project('Demo', tmp_path / f'marker-{marker!r}')
    config = load_yaml(root / 'project.yaml')
    config['tooling']['skills_schema_version'] = marker
    _write_yaml(root / 'project.yaml', config)

    layer = inspect_skill_layer(root)
    assert any(
        severity == 'BLOCKING' and 'unsupported skills_schema_version' in message
        for severity, _, message in layer.issues
    )
    assert _fatal(root)


def test_skills_validate_cli_rejects_boolean_activation_marker(tmp_path, monkeypatch):
    root = init_project('Demo', tmp_path / 'boolean-marker')
    config = load_yaml(root / 'project.yaml')
    config['tooling']['skills_schema_version'] = True
    _write_yaml(root / 'project.yaml', config)
    monkeypatch.chdir(root)

    with pytest.raises(SystemExit) as exc:
        main(['skills', 'validate'])

    assert exc.value.code == 2


def test_missing_required_and_conditional_design_skill(tmp_path):
    root = init_project('Demo', tmp_path / 'demo')
    registry = _registry(root)
    registry['skills'].pop('release-check')
    _write_registry(root, registry)
    shutil.rmtree(root / '.agents' / 'skills' / 'release-check')
    assert any('missing required Skill: release-check' in item[2] for item in inspect_skill_layer(root).issues)

    root2 = init_project('Design', tmp_path / 'design')
    config = load_yaml(root2 / 'project.yaml')
    config['external_systems']['google_workspace'] = {
        'enabled': True,
        'project_overview': True,
        'design_knowledge': True,
        'design_changes': True,
    }
    _write_yaml(root2 / 'project.yaml', config)
    assert any('missing required Skill: design-handoff' in item[2] for item in inspect_skill_layer(root2).issues)


@pytest.mark.parametrize(
    ('mutation', 'message'),
    [
        ('unknown-capability', 'not one of'),
        ('generated-write', 'forbidden max_writes root'),
        ('history-write', 'forbidden max_writes root'),
        ('traversal-write', 'unsafe max_writes path'),
    ],
)
def test_registry_rejects_unknown_capability_and_unsafe_write_roots(tmp_path, mutation, message):
    root = init_project('Demo', tmp_path / mutation)
    registry = _registry(root)
    entry = registry['skills']['knowledge-sync']
    if mutation == 'unknown-capability':
        entry['capabilities'].append('shell.anything')
    elif mutation == 'generated-write':
        entry['max_writes'].append('.generated/**')
    elif mutation == 'history-write':
        entry['max_writes'].append('history/**')
    else:
        entry['max_writes'].append('../escape/**')
    _write_registry(root, registry)

    assert any(message in item[2] for item in inspect_skill_layer(root).issues)


@pytest.mark.parametrize(
    'pattern',
    ['.Project/**', '.PROJECT/**', 'Project.yaml', 'PROJECT.YAML', 'History/**'],
)
def test_registry_rejects_case_variants_of_protected_roots(tmp_path, pattern):
    root = init_project('Demo', tmp_path / pattern.replace('/', '-').replace('*', 'x'))
    registry = _registry(root)
    registry['skills']['knowledge-sync']['max_writes'].append(pattern)
    _write_registry(root, registry)

    assert any('forbidden max_writes root' in item[2] for item in inspect_skill_layer(root).issues)


@pytest.mark.parametrize('pattern', ['docs2/**', 'project.yaml.example', 'history2/**'])
def test_registry_allows_non_protected_prefix_neighbors(tmp_path, pattern):
    root = init_project('Demo', tmp_path / pattern.replace('/', '-').replace('*', 'x'))
    registry = _registry(root)
    registry['skills']['knowledge-sync']['max_writes'].append(pattern)
    _write_registry(root, registry)

    assert not [item for item in inspect_skill_layer(root).issues if item[0] in {'BLOCKING', 'ERROR'}]


def test_registry_rejects_duplicate_keys_and_aliases(tmp_path):
    root = init_project('Demo', tmp_path / 'demo')
    registry_path = root / '.project' / 'skills.yaml'
    original = registry_path.read_text(encoding='utf-8')
    registry_path.write_text(original.replace('schema_version: 1', 'schema_version: 1\nschema_version: 1', 1), encoding='utf-8')
    assert any('duplicate YAML key' in item[2] for item in inspect_skill_layer(root).issues)

    registry_path.write_text('schema_version: &v 1\nprofile: project-system-skills-v1\nskills: *v\n', encoding='utf-8')
    assert any('anchors and aliases' in item[2] for item in inspect_skill_layer(root).issues)


def test_invalid_frontmatter_name_and_unregistered_directory_fail(tmp_path):
    root = init_project('Demo', tmp_path / 'demo')
    skill = root / '.agents' / 'skills' / 'knowledge-sync' / 'SKILL.md'
    skill.write_text(skill.read_text(encoding='utf-8').replace('name: knowledge-sync', 'name: wrong-name', 1), encoding='utf-8')
    extra = root / '.agents' / 'skills' / 'extra-skill'
    extra.mkdir()
    (extra / 'SKILL.md').write_text('---\nname: extra-skill\ndescription: Extra.\n---\n\n# Extra\n', encoding='utf-8')

    messages = [item[2] for item in inspect_skill_layer(root).issues]
    assert any('frontmatter name' in message for message in messages)
    assert 'unregistered project Skill' in messages


def test_missing_local_reference_and_skill_resource_directory_fail(tmp_path):
    root = init_project('Demo', tmp_path / 'demo')
    skill_dir = root / '.agents' / 'skills' / 'knowledge-sync'
    skill = skill_dir / 'SKILL.md'
    skill.write_text(skill.read_text(encoding='utf-8') + '\n[Missing](references/missing.md)\n', encoding='utf-8')
    assert any('missing local SKILL.md reference' in item[2] for item in inspect_skill_layer(root).issues)

    skill.write_text(skill.read_text(encoding='utf-8').replace('\n[Missing](references/missing.md)\n', '\n'), encoding='utf-8')
    (skill_dir / 'scripts').mkdir()
    assert any('unsupported Skill resource directory: scripts' in item[2] for item in inspect_skill_layer(root).issues)


def test_skill_symlink_is_rejected_where_supported(tmp_path):
    root = init_project('Demo', tmp_path / 'demo')
    target = root / 'outside.md'
    target.write_text('outside', encoding='utf-8')
    link = root / '.agents' / 'skills' / 'knowledge-sync' / 'assets'
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f'symlink creation unavailable: {exc}')
    assert any('symlink or reparse point' in item[2] for item in inspect_skill_layer(root).issues)


def test_multiple_skill_scope_is_union_of_independent_intersections(tmp_path):
    base = dict(path=tmp_path / 'SKILL.md', description='test', body='test', content='test', sha256='0' * 64, capabilities=())
    records = {
        'one': SkillRecord(name='one', max_writes=('knowledge/**',), **base),
        'two': SkillRecord(name='two', max_writes=('docs/**',), **base),
        'irrelevant': SkillRecord(name='irrelevant', max_writes=('inbox/**',), **base),
    }
    effective, authorizations = effective_write_scope(
        records,
        ['knowledge/features/one.md', 'docs/03_PRODUCT.md', 'inbox/design/input.md'],
        ['knowledge/features/one.md', 'docs/03_PRODUCT.md'],
    )

    assert effective == ['docs/03_PRODUCT.md', 'knowledge/features/one.md']
    assert authorizations == {
        'docs/03_PRODUCT.md': ['two'],
        'knowledge/features/one.md': ['one'],
    }


def test_context_binds_only_selected_skill_and_scopes(tmp_path):
    root = init_project('Demo', tmp_path / 'demo')
    path, object_id = create_object(root, 'feature', 'Target', 'product', 'owner')
    relative = path.relative_to(root).as_posix()
    output, manifest = build_context(
        root,
        object_id,
        'small',
        'review',
        allowed_write_set=[relative],
        skill_names=['requirements-management'],
    )
    context = (output / 'context.md').read_text(encoding='utf-8')

    assert [item['name'] for item in manifest['selected_skills']] == ['requirements-management']
    assert manifest['skills_registry_sha256']
    assert manifest['task_write_scope']['canonical'] == [relative]
    assert manifest['task_write_scope']['derived'] == [f'{output.relative_to(root).as_posix()}/**']
    assert manifest['effective_write_scope'] == [relative]
    assert '# Requirements Management' in context
    assert '# Knowledge Sync' not in context


def test_bootstrap_keeps_knowledge_bootstrap_semantics_and_selects_skill(tmp_path):
    root = init_project('Demo', tmp_path / 'demo')
    output, manifest = bootstrap(root, 'small')

    assert manifest['target'] == 'bootstrap'
    assert manifest['mode'] == 'sync'
    assert manifest['allowed_write_set'] == ['docs/**', 'knowledge/**', 'inbox/**']
    assert [item['name'] for item in manifest['selected_skills']] == ['knowledge-sync']
    assert output.name == 'BOOTSTRAP-bootstrap-small'


def test_default_project_screen_task_does_not_require_conditional_design_skill(tmp_path):
    root = init_project('Demo', tmp_path / 'demo')
    _, object_id = create_object(root, 'screen', 'Home', 'design', 'owner')
    _, manifest = task(root, object_id, mode='review', budget='small')
    assert [item['name'] for item in manifest['selected_skills']] == ['knowledge-sync']


def test_install_adds_conditional_design_skill_after_configuration_change(tmp_path):
    root = init_project('Demo', tmp_path / 'demo')
    config = load_yaml(root / 'project.yaml')
    config['external_systems']['design_changes']['enabled'] = True
    _write_yaml(root / 'project.yaml', config)
    _commit(root)

    plan = install_skills(root)
    assert '.agents/skills/design-handoff/SKILL.md' in plan['writes']
    report = install_skills(root, apply=True)
    assert report['status'] == 'installed'
    assert DESIGN_SKILL in inspect_skill_layer(root).records


def test_sync_plan_binds_skill_evidence_and_authorizes_every_path(tmp_path):
    _, _, _, output, manifest = _planned_project(tmp_path)
    plan = json.loads((output / 'plan.json').read_text(encoding='utf-8'))

    assert {item['name'] for item in manifest['selected_skills']} == {
        'knowledge-sync',
        'requirements-management',
    }
    assert plan['selected_skills'] == manifest['selected_skills']
    assert plan['skills_registry_sha256'] == manifest['skills_registry_sha256']
    assert set(plan['allowed_write_set']) == set(plan['effective_write_scope'])
    assert set(plan['skill_write_authorizations']) == set(plan['allowed_write_set'])
    assert '# Knowledge Sync' in (output / 'context.md').read_text(encoding='utf-8')


def test_sync_plan_rejects_allowed_path_without_selected_skill_authorization(tmp_path):
    root = init_project('Demo', tmp_path / 'demo')
    _, object_id = create_object(root, 'entity', 'Target', 'general', 'owner')
    registry = _registry(root)
    registry['skills']['knowledge-sync']['max_writes'] = ['docs/**']
    _write_registry(root, registry)
    head = _commit(root)
    pack = _pack(head, object_id)
    pack_path = root / 'inbox' / 'sync' / f'{pack["pack_id"]}.yaml'
    _write_yaml(pack_path, pack)

    with pytest.raises(SyncPlanError, match='lack selected Skill authorization'):
        plan_sync(root, pack_path)


@pytest.mark.parametrize('drift', ['registry', 'skill'])
def test_sync_verify_rejects_registry_or_selected_skill_drift(tmp_path, drift):
    root, _, pack_path, _, _ = _planned_project(tmp_path)
    if drift == 'registry':
        registry_path = root / '.project' / 'skills.yaml'
        registry_path.write_text(registry_path.read_text(encoding='utf-8') + '\n', encoding='utf-8')
    else:
        skill = root / '.agents' / 'skills' / 'knowledge-sync' / 'SKILL.md'
        skill.write_text(skill.read_text(encoding='utf-8') + '\n', encoding='utf-8')

    with pytest.raises(SyncIntegrityError, match='Skills evidence verification failed'):
        verify_sync(root, pack_path)


def test_finalize_rechecks_same_skills_evidence(tmp_path):
    root, path, pack_path, _, _ = _planned_project(tmp_path)
    path.write_text(path.read_text(encoding='utf-8') + '\nApproved edit.\n', encoding='utf-8')
    verify_sync(root, pack_path)
    skill = root / '.agents' / 'skills' / 'knowledge-sync' / 'SKILL.md'
    skill.write_text(skill.read_text(encoding='utf-8') + '\n', encoding='utf-8')

    with pytest.raises(SyncFinalizeIntegrityError, match='Skills evidence verification failed'):
        finalize_sync(root, pack_path)


def test_true_legacy_sync_artifacts_without_skills_fields_remain_valid(tmp_path):
    root, path, pack_path, output, _ = _legacy_planned_project(tmp_path)
    plan = json.loads((output / 'plan.json').read_text(encoding='utf-8'))
    assert not (SKILL_FIELDS & set(plan))
    path.write_text(path.read_text(encoding='utf-8') + '\nLegacy-compatible edit.\n', encoding='utf-8')

    _, report = verify_sync(root, pack_path)
    assert report['verification_result'] == 'passed'
    assert not (SKILL_FIELDS & set(report))


def test_skills_aware_sync_artifacts_with_evidence_remain_valid(tmp_path):
    root, path, pack_path, _, _ = _planned_project(tmp_path)
    path.write_text(path.read_text(encoding='utf-8') + '\nSkills-aware edit.\n', encoding='utf-8')

    _, report = verify_sync(root, pack_path)
    assert report['verification_result'] == 'passed'
    assert SKILL_FIELDS <= set(report)


def test_rehashed_stripped_skills_evidence_cannot_downgrade_skills_base(tmp_path):
    root, path, pack_path, output, _ = _planned_project(tmp_path)
    plan_path = output / 'plan.json'
    manifest_path = output / 'manifest.json'
    plan = json.loads(plan_path.read_text(encoding='utf-8'))
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    for field in SKILL_FIELDS:
        plan.pop(field)
        manifest.pop(field)
    plan_path.write_text(json.dumps(plan, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    _rewrite_integrity(output)
    path.write_text(path.read_text(encoding='utf-8') + '\nTampered downgrade edit.\n', encoding='utf-8')

    with pytest.raises(SyncIntegrityError, match='Skills-era base project is missing Skills evidence'):
        verify_sync(root, pack_path)


def test_partial_skills_evidence_fails_closed(tmp_path):
    root, _, pack_path, output, _ = _planned_project(tmp_path)
    manifest_path = output / 'manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    manifest.pop('skills_registry_sha256')
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    _rewrite_integrity(output)

    with pytest.raises(SyncIntegrityError, match='Skills evidence is incomplete'):
        verify_sync(root, pack_path)


@pytest.mark.parametrize(
    'case',
    [
        'selected_skills',
        'selected_skills.name',
        'selected_skills.path',
        'selected_skills.sha256',
        'skills_registry_sha256',
        'skill_write_authorizations',
        'effective_write_scope',
        'task_write_scope',
    ],
)
def test_rehashed_malformed_skills_evidence_fails_closed(tmp_path, case):
    root, _, pack_path, output, _ = _planned_project(tmp_path)
    for filename in ('plan.json', 'manifest.json'):
        artifact_path = output / filename
        document = json.loads(artifact_path.read_text(encoding='utf-8'))
        _malform_skills_evidence(document, case)
        artifact_path.write_text(
            json.dumps(document, indent=2, sort_keys=True) + '\n', encoding='utf-8',
        )
    _rewrite_integrity(output)

    with pytest.raises(SyncIntegrityError, match='Skills evidence verification failed'):
        verify_sync(root, pack_path)


def test_migration_dry_run_and_apply_upgrade_only_known_stock_templates(tmp_path):
    root = init_project('Demo', tmp_path / 'demo')
    _legacy_project(root)
    _commit(root)

    plan = install_skills(root)
    assert plan['status'] == 'ready'
    assert plan['conflicts'] == []
    assert 'project.yaml' in plan['writes']
    assert 'AGENTS.md' in plan['writes']
    assert '.project/skills.yaml' in plan['writes']
    assert load_yaml(root / 'project.yaml')['tooling'].get('skills_schema_version') is None

    report = install_skills(root, apply=True)
    assert report['status'] == 'installed'
    assert not _fatal(root)
    tooling = load_yaml(root / 'project.yaml')['tooling']
    assert tooling['skills_schema_version'] == 1
    assert tooling['project_cli'] == SKILLS_ERA_PROJECT_CLI_VERSION


def test_migrated_project_cannot_silently_downgrade_by_deleting_skills_layer(tmp_path):
    root = init_project('Demo', tmp_path / 'migrated')
    _legacy_project(root)
    _commit(root)
    install_skills(root, apply=True)

    config = load_yaml(root / 'project.yaml')
    config['tooling'].pop('skills_schema_version')
    _write_yaml(root / 'project.yaml', config)
    (root / '.project' / 'skills.yaml').unlink()
    shutil.rmtree(root / '.agents' / 'skills')

    layer = inspect_skill_layer(root)
    assert layer.active is False
    assert any(
        severity == 'ERROR' and 'CLI 0.12+ project is missing' in message
        for severity, _, message in layer.issues
    )
    assert _fatal(root)


def test_migration_refuses_custom_constitution_and_dirty_worktree(tmp_path):
    root = init_project('Demo', tmp_path / 'custom')
    _legacy_project(root)
    (root / 'AGENTS.md').write_text('# Custom constitution\n', encoding='utf-8')
    _commit(root)
    plan = install_skills(root)
    assert plan['status'] == 'conflict'
    assert any('AGENTS.md: custom content' in item for item in plan['conflicts'])
    with pytest.raises(SkillError, match='manual merge'):
        install_skills(root, apply=True)

    root2 = init_project('Demo', tmp_path / 'dirty')
    _legacy_project(root2)
    _commit(root2)
    (root2 / 'README.md').write_text('dirty', encoding='utf-8')
    with pytest.raises(SkillError, match='clean Git worktree'):
        install_skills(root2, apply=True)


def test_install_blocks_dirty_untracked_worktree(tmp_path):
    root = init_project('Demo', tmp_path / 'dirty-untracked')
    _legacy_project(root)
    _commit(root)
    (root / 'untracked.txt').write_text('dirty\n', encoding='utf-8')

    with pytest.raises(SkillError, match='clean Git worktree'):
        install_skills(root, apply=True)


def test_install_allows_only_ignored_generated_state(tmp_path):
    root = init_project('Demo', tmp_path / 'ignored-generated')
    _legacy_project(root)
    _commit(root)
    (root / '.generated' / 'runtime.tmp').write_text('derived\n', encoding='utf-8')

    report = install_skills(root, apply=True)

    assert report['status'] == 'installed'


def test_install_blocks_non_git_project_without_mutation(tmp_path):
    root = init_project('Demo', tmp_path / 'non-git')
    _legacy_project(root)
    before = (root / 'project.yaml').read_bytes()

    with pytest.raises(SkillError, match='cannot prove Git worktree cleanliness'):
        install_skills(root, apply=True)

    assert (root / 'project.yaml').read_bytes() == before
    assert not (root / '.project' / 'skills.yaml').exists()


def test_install_blocks_git_status_error_without_mutation(tmp_path, monkeypatch):
    root = init_project('Demo', tmp_path / 'git-error')
    _legacy_project(root)
    _commit(root)
    before = (root / 'project.yaml').read_bytes()
    monkeypatch.setattr(
        'project_system.process_runner.run_process',
        lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout='', stderr='denied'),
    )

    with pytest.raises(SkillError, match='cannot prove Git worktree cleanliness'):
        install_skills(root, apply=True)

    assert (root / 'project.yaml').read_bytes() == before
    assert not (root / '.project' / 'skills.yaml').exists()


def test_install_blocks_inaccessible_git_without_mutation(tmp_path, monkeypatch):
    root = init_project('Demo', tmp_path / 'git-unavailable')
    _legacy_project(root)
    before = (root / 'project.yaml').read_bytes()

    def unavailable(*args, **kwargs):
        raise FileNotFoundError('git executable unavailable')

    monkeypatch.setattr('project_system.process_runner.run_process', unavailable)

    with pytest.raises(SkillError, match='cannot prove Git worktree cleanliness'):
        install_skills(root, apply=True)

    assert (root / 'project.yaml').read_bytes() == before
    assert not (root / '.project' / 'skills.yaml').exists()


def test_install_rolls_back_if_final_layer_validation_fails(tmp_path, monkeypatch):
    root = init_project('Demo', tmp_path / 'demo')
    _legacy_project(root)
    _commit(root)
    before = {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob('*')
        if path.is_file() and '.git' not in path.relative_to(root).parts
    }

    class FailedLayer:
        issues = [('ERROR', '.project/skills.yaml', 'injected final validation failure')]

    monkeypatch.setattr('project_system.skills.inspect_skill_layer', lambda *args, **kwargs: FailedLayer())
    with pytest.raises(SkillError, match='injected final validation failure'):
        install_skills(root, apply=True)

    after = {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob('*')
        if path.is_file() and '.git' not in path.relative_to(root).parts
    }
    assert after == before
    tooling = load_yaml(root / 'project.yaml')['tooling']
    assert tooling['project_cli'] == '0.11.0'
    assert 'skills_schema_version' not in tooling
