from pathlib import Path
import subprocess

import pytest
import yaml

from project_system.cli import main
from project_system.init_project import init_project
from project_system.objects import create_object
from project_system.sync_planning import SyncPlanError, plan_sync


def _git(root, *args):
    result = subprocess.run(
        ['git', *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _commit_project(root):
    _git(root, 'init', '-q')
    _git(root, 'config', 'user.email', 'tests@example.invalid')
    _git(root, 'config', 'user.name', 'Project System Tests')
    _git(root, 'add', '.')
    _git(root, 'commit', '-qm', 'base')
    return _git(root, 'rev-parse', 'HEAD')


def _pack(root, head, changes, expected_targets, pack_id='SYNC-20260905-deadbeef'):
    return {
        'schema_version': 1,
        'pack_id': pack_id,
        'project_id': 'demo',
        'source': {'type': 'approved_discussion', 'ref': 'discussion-1'},
        'created_at': '2026-09-05T10:00:00+03:00',
        'base_commit': head,
        'approval': {
            'approved_by': 'project-owner',
            'approved_at': '2026-09-05T10:05:00+03:00',
        },
        'change_class': 'C',
        'changes': changes,
        'expected_targets': expected_targets,
        'notes': 'Planning only.',
    }


def _write_pack(root, pack, name='SYNC-20260905-deadbeef.yaml'):
    path = root / 'inbox' / 'sync' / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(pack, sort_keys=False, allow_unicode=True),
        encoding='utf-8',
    )
    return path


def _update_change(object_id):
    return {
        'change_id': 'update-existing',
        'kind': 'update_object',
        'summary': 'Apply the approved clarification.',
        'target_id': object_id,
        'patch': {'body': 'Approved replacement body.'},
    }


def _snapshot_canonical(root):
    snapshot = {}
    for path in sorted(candidate for candidate in root.rglob('*') if candidate.is_file()):
        relative = path.relative_to(root)
        if relative.parts[0] in {'.git', '.generated'}:
            continue
        snapshot[relative.as_posix()] = path.read_bytes()
    return snapshot


def test_valid_pack_supports_all_change_kinds(tmp_path):
    root = init_project('Demo', tmp_path / 'demo')
    feature_path, feature_id = create_object(root, 'feature', 'Search', 'product', 'owner')
    requirement_path, requirement_id = create_object(root, 'requirement', 'Safe search', 'general', 'owner')
    head = _commit_project(root)
    new_id = 'FEAT-20260905-cafebabe'
    changes = [
        {
            'change_id': 'create-approved',
            'kind': 'create_object',
            'summary': 'Create the approved feature object.',
            'object': {
                'id': new_id,
                'type': 'feature',
                'title': 'Approved feature',
                'slug': 'approved-feature',
                'domain': 'product',
                'status': 'planned',
                'body': '# Approved feature\n',
            },
        },
        _update_change(feature_id),
        {
            'change_id': 'retire-requirement',
            'kind': 'retire_object',
            'summary': 'Deprecate the old requirement.',
            'target_id': requirement_id,
            'new_status': 'deprecated',
            'replacement_id': new_id,
        },
        {
            'change_id': 'product-doc',
            'kind': 'narrative_impact',
            'summary': 'Update the product narrative.',
            'narrative_paths': ['docs/03_PRODUCT.md'],
        },
        {
            'change_id': 'proposal-later',
            'kind': 'proposal',
            'summary': 'Keep a later option visible.',
            'proposal': 'Consider a second search mode later.',
            'related_ids': [feature_id],
        },
        {
            'change_id': 'unresolved-ranking',
            'kind': 'unresolved',
            'summary': 'Ranking remains unresolved.',
            'proposal': 'Do not activate a ranking choice.',
            'related_ids': [requirement_id],
        },
    ]
    pack = _pack(
        root,
        head,
        changes,
        [new_id, feature_id, requirement_id, 'docs/03_PRODUCT.md'],
    )
    pack_path = _write_pack(root, pack)

    output, manifest = plan_sync(root, pack_path)

    assert (output / 'plan.json').is_file()
    assert (output / 'manifest.json').is_file()
    assert (output / 'context.md').is_file()
    assert manifest['errors'] == []
    assert manifest['pack_content_sha256']
    assert manifest['pack_id'] == pack['pack_id']
    assert manifest['base_commit'] == head
    assert manifest['approval'] == pack['approval']
    assert manifest['resolved_targets']
    assert manifest['protected_paths']
    assert manifest['out_of_scope_paths']
    assert manifest['warnings']
    assert str(feature_path.relative_to(root)).replace('\\', '/') in manifest['allowed_write_set']
    assert str(requirement_path.relative_to(root)).replace('\\', '/') in manifest['allowed_write_set']
    assert f'knowledge/features/{new_id}-approved-feature.md' in manifest['allowed_write_set']
    assert 'docs/03_PRODUCT.md' in manifest['allowed_write_set']
    assert {item['kind'] for item in manifest['unresolved_proposal_items']} == {
        'proposal',
        'unresolved',
    }


def test_malformed_schema_is_rejected_without_output(tmp_path):
    root = init_project('Demo', tmp_path / 'demo')
    head = _commit_project(root)
    pack = _pack(root, head, [], [])
    pack['schema_version'] = 2
    pack_path = _write_pack(root, pack)

    with pytest.raises(SyncPlanError, match='schema validation failed'):
        plan_sync(root, pack_path)

    assert not (root / '.generated' / 'sync').exists()


def test_wrong_project_is_rejected(tmp_path):
    root = init_project('Demo', tmp_path / 'demo')
    _, object_id = create_object(root, 'feature', 'Search', 'product', 'owner')
    head = _commit_project(root)
    pack = _pack(root, head, [_update_change(object_id)], [object_id])
    pack['project_id'] = 'another-project'

    with pytest.raises(SyncPlanError, match='wrong project'):
        plan_sync(root, _write_pack(root, pack))


def test_stale_base_commit_is_rejected(tmp_path):
    root = init_project('Demo', tmp_path / 'demo')
    _, object_id = create_object(root, 'feature', 'Search', 'product', 'owner')
    head = _commit_project(root)
    pack = _pack(root, head, [_update_change(object_id)], [object_id])
    pack['base_commit'] = '0' * 40

    with pytest.raises(SyncPlanError, match='stale base_commit'):
        plan_sync(root, _write_pack(root, pack))


def test_missing_target_id_is_rejected(tmp_path):
    root = init_project('Demo', tmp_path / 'demo')
    head = _commit_project(root)
    missing_id = 'FEAT-20260905-deadbeef'
    pack = _pack(root, head, [_update_change(missing_id)], [missing_id])

    with pytest.raises(SyncPlanError, match='targets missing object ID'):
        plan_sync(root, _write_pack(root, pack))


def test_missing_object_reference_in_patch_is_rejected(tmp_path):
    root = init_project('Demo', tmp_path / 'demo')
    _, object_id = create_object(root, 'feature', 'Search', 'product', 'owner')
    head = _commit_project(root)
    missing_id = 'REQ-20260905-deadbeef'
    change = _update_change(object_id)
    change['patch'] = {'frontmatter': {'depends_on': [missing_id]}}
    pack = _pack(root, head, [change], [object_id])

    with pytest.raises(SyncPlanError, match='references missing object ID'):
        plan_sync(root, _write_pack(root, pack))


def test_duplicate_change_id_is_rejected(tmp_path):
    root = init_project('Demo', tmp_path / 'demo')
    head = _commit_project(root)
    proposal = {
        'change_id': 'duplicate',
        'kind': 'proposal',
        'summary': 'Proposal.',
        'proposal': 'Keep this non-canonical.',
    }
    pack = _pack(root, head, [proposal, dict(proposal)], [])

    with pytest.raises(SyncPlanError, match='duplicate change_id'):
        plan_sync(root, _write_pack(root, pack))


def test_duplicate_pack_id_in_sync_inbox_is_rejected(tmp_path):
    root = init_project('Demo', tmp_path / 'demo')
    head = _commit_project(root)
    proposal = {
        'change_id': 'proposal',
        'kind': 'proposal',
        'summary': 'Proposal.',
        'proposal': 'Keep this non-canonical.',
    }
    pack = _pack(root, head, [proposal], [])
    primary = root / 'approved-pack.yaml'
    primary.write_text(yaml.safe_dump(pack, sort_keys=False), encoding='utf-8')
    _write_pack(root, pack, 'duplicate.yaml')

    with pytest.raises(SyncPlanError, match='duplicate pack_id'):
        plan_sync(root, primary)


def test_path_traversal_is_rejected(tmp_path):
    root = init_project('Demo', tmp_path / 'demo')
    head = _commit_project(root)
    change = {
        'change_id': 'escape',
        'kind': 'narrative_impact',
        'summary': 'Unsafe target.',
        'narrative_paths': ['docs/../README.md'],
    }
    pack = _pack(root, head, [change], ['docs/../README.md'])

    with pytest.raises(SyncPlanError, match='unsafe target path'):
        plan_sync(root, _write_pack(root, pack))


def test_proposal_does_not_authorize_canonical_write(tmp_path):
    root = init_project('Demo', tmp_path / 'demo')
    _, object_id = create_object(root, 'feature', 'Search', 'product', 'owner')
    head = _commit_project(root)
    change = {
        'change_id': 'proposal-only',
        'kind': 'proposal',
        'summary': 'Do not activate this proposal.',
        'proposal': 'Maybe add a different search mode.',
        'related_ids': [object_id],
    }
    pack = _pack(root, head, [change], [])

    _, manifest = plan_sync(root, _write_pack(root, pack))

    assert manifest['allowed_write_set'] == []
    assert manifest['resolved_targets'] == []
    assert manifest['unresolved_proposal_items'][0]['change_id'] == 'proposal-only'


def test_plan_is_deterministic_and_idempotent(tmp_path):
    root = init_project('Demo', tmp_path / 'demo')
    _, object_id = create_object(root, 'feature', 'Search', 'product', 'owner')
    head = _commit_project(root)
    pack_path = _write_pack(root, _pack(root, head, [_update_change(object_id)], [object_id]))

    first_output, _ = plan_sync(root, pack_path)
    first = {path.name: path.read_bytes() for path in first_output.iterdir()}
    second_output, _ = plan_sync(root, pack_path)
    second = {path.name: path.read_bytes() for path in second_output.iterdir()}

    assert first == second


def test_changed_content_cannot_reuse_planned_pack_id(tmp_path):
    root = init_project('Demo', tmp_path / 'demo')
    _, object_id = create_object(root, 'feature', 'Search', 'product', 'owner')
    head = _commit_project(root)
    pack = _pack(root, head, [_update_change(object_id)], [object_id])
    pack_path = _write_pack(root, pack)
    plan_sync(root, pack_path)
    pack['notes'] = 'Changed after planning.'
    _write_pack(root, pack)

    with pytest.raises(SyncPlanError, match='already planned with different content'):
        plan_sync(root, pack_path)


def test_planning_does_not_write_canonical_files(tmp_path):
    root = init_project('Demo', tmp_path / 'demo')
    _, object_id = create_object(root, 'feature', 'Search', 'product', 'owner')
    head = _commit_project(root)
    pack_path = _write_pack(root, _pack(root, head, [_update_change(object_id)], [object_id]))
    before = _snapshot_canonical(root)

    output, _ = plan_sync(root, pack_path)

    assert _snapshot_canonical(root) == before
    assert all(path.parent == output for path in output.iterdir())
    assert {path.name for path in output.iterdir()} == {'plan.json', 'manifest.json', 'context.md'}


def test_slugged_atomic_filename_resolves_by_internal_id(tmp_path):
    root = init_project('Demo', tmp_path / 'demo')
    path, object_id = create_object(root, 'feature', 'Search', 'product', 'owner')
    slugged = path.rename(path.with_name(f'{object_id}-search.md'))
    head = _commit_project(root)
    pack_path = _write_pack(root, _pack(root, head, [_update_change(object_id)], [object_id]))

    _, manifest = plan_sync(root, pack_path)

    assert manifest['resolved_targets'][0]['object_id'] == object_id
    assert manifest['resolved_targets'][0]['path'] == slugged.relative_to(root).as_posix()
    assert slugged.relative_to(root).as_posix() in manifest['allowed_write_set']


def test_cli_sync_plan_and_legacy_sync_are_unambiguous(tmp_path, monkeypatch, capsys):
    root = init_project('Demo', tmp_path / 'demo')
    _, object_id = create_object(root, 'feature', 'Search', 'product', 'owner')
    head = _commit_project(root)
    pack_path = _write_pack(root, _pack(root, head, [_update_change(object_id)], [object_id]))
    monkeypatch.chdir(root)

    main(['sync', 'plan', str(pack_path)])
    plan_output = capsys.readouterr().out
    main(['sync', object_id, '--budget', 'small'])
    legacy_output = capsys.readouterr().out

    assert '.generated/sync/' in plan_output.replace('\\', '/')
    assert 'SYNC-' + object_id in legacy_output
