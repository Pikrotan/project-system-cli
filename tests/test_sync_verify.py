import json
from pathlib import Path
import subprocess

import pytest
import yaml

from project_system.cli import main
from project_system.frontmatter import read_object, write_object
from project_system.init_project import init_project
from project_system.objects import create_object
from project_system.sync_planning import (
    SyncPlanError,
    artifact_integrity_block,
    plan_sync,
)
from project_system.sync_verification import (
    SyncIntegrityError,
    SyncScopeError,
    SyncValidationError,
    verify_sync,
)


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


def _pack(head, changes, expected_targets, pack_id='SYNC-20260905-feedface'):
    return {
        'schema_version': 1,
        'pack_id': pack_id,
        'project_id': 'demo',
        'source': {'type': 'approved_discussion', 'ref': 'phase-2-tests'},
        'created_at': '2026-09-05T12:00:00+03:00',
        'base_commit': head,
        'approval': {
            'approved_by': 'project-owner',
            'approved_at': '2026-09-05T12:01:00+03:00',
        },
        'change_class': 'C',
        'changes': changes,
        'expected_targets': expected_targets,
        'notes': 'Deterministic verification test.',
    }


def _write_pack(root, pack):
    path = root / 'inbox' / 'sync' / f'{pack["pack_id"]}.yaml'
    path.write_text(
        yaml.safe_dump(pack, sort_keys=False, allow_unicode=True),
        encoding='utf-8',
    )
    return path


def _update_change(object_id):
    return {
        'change_id': 'update-existing',
        'kind': 'update_object',
        'summary': 'Apply the approved body supplied by the executor.',
        'target_id': object_id,
        'patch': {'body': 'Externally supplied body.'},
    }


def _setup_update_plan(tmp_path, *, extra_changes=None, extra_targets=None):
    root = init_project('Demo', tmp_path / 'demo')
    path, object_id = create_object(root, 'feature', 'Search', 'general', 'owner')
    path = path.rename(path.with_name(f'{object_id}-search.md'))
    head = _commit_project(root)
    changes = [_update_change(object_id), *(extra_changes or [])]
    targets = [object_id, *(extra_targets or [])]
    pack = _pack(head, changes, targets)
    pack_path = _write_pack(root, pack)
    output, manifest = plan_sync(root, pack_path)
    return root, path, object_id, head, pack, pack_path, output, manifest


def _append_body(path, text='\nExternally applied deterministic test edit.\n'):
    path.write_text(path.read_text(encoding='utf-8') + text, encoding='utf-8')


def _load_report(output):
    return json.loads((output / 'verification.json').read_text(encoding='utf-8'))


def test_valid_allowed_slugged_edit_and_no_commit(tmp_path):
    root, path, object_id, head, _, pack_path, output, _ = _setup_update_plan(tmp_path)
    _append_body(path)

    verified_output, report = verify_sync(root, pack_path)

    relative = path.relative_to(root).as_posix()
    assert verified_output == output
    assert report['verification_result'] == 'passed'
    assert report['actual_changed_canonical_paths'] == [relative]
    assert report['changes_outside_scope'] == []
    assert report['atomic_object_lifecycle'][0]['id'] == object_id
    assert report['atomic_object_lifecycle'][0]['action'] == 'updated'
    assert report['semantic_meaning_verified'] is False
    assert report['human_semantic_review_required'] is True
    assert _git(root, 'rev-parse', 'HEAD') == head
    assert {'verification.json', 'verification.md', 'diff-summary.md'} <= {
        item.name for item in output.iterdir()
    }


def test_allowed_new_object(tmp_path):
    root = init_project('Demo', tmp_path / 'demo')
    head = _commit_project(root)
    object_id = 'FEAT-20260905-cafebabe'
    relative = f'knowledge/features/{object_id}-approved-feature.md'
    change = {
        'change_id': 'create-feature',
        'kind': 'create_object',
        'summary': 'Create the approved feature object.',
        'object': {
            'id': object_id,
            'type': 'feature',
            'title': 'Approved feature',
            'slug': 'approved-feature',
            'domain': 'general',
            'status': 'planned',
            'body': '# Approved feature\n',
        },
    }
    pack = _pack(head, [change], [object_id])
    pack_path = _write_pack(root, pack)
    plan_sync(root, pack_path)
    write_object(
        root / relative,
        {
            'schema_version': 1,
            'id': object_id,
            'type': 'feature',
            'title': 'Approved feature',
            'domain': 'general',
            'status': 'planned',
            'owner': 'project-owner',
            'created_at': '2026-09-05',
            'source': {'type': 'owner_decision', 'ref': pack['pack_id']},
            'depends_on': [],
        },
        '# Approved feature\n',
    )

    _, report = verify_sync(root, pack['pack_id'])

    assert report['verification_result'] == 'passed'
    assert report['actual_changed_canonical_paths'] == [relative]
    assert report['atomic_object_lifecycle'][0]['action'] == 'created'
    assert report['atomic_object_lifecycle'][0]['id'] == object_id


def test_allowed_retirement_status_change(tmp_path):
    root = init_project('Demo', tmp_path / 'demo')
    path, object_id = create_object(root, 'feature', 'Search', 'general', 'owner')
    path = path.rename(path.with_name(f'{object_id}-search.md'))
    head = _commit_project(root)
    retire = {
        'change_id': 'retire-feature',
        'kind': 'retire_object',
        'summary': 'Retire the approved feature.',
        'target_id': object_id,
        'new_status': 'deprecated',
    }
    pack = _pack(head, [retire], [object_id], 'SYNC-20260905-retire01')
    retirement_pack = _write_pack(root, pack)
    plan_sync(root, retirement_pack)
    data, body = read_object(path)
    data['status'] = 'deprecated'
    write_object(path, data, body)

    _, report = verify_sync(root, retirement_pack)

    item = report['atomic_object_lifecycle'][0]
    assert item['id'] == object_id
    assert item['action'] == 'retired'
    assert item['status_before'] == 'idea'
    assert item['status_after'] == 'deprecated'


def test_edit_outside_allowed_write_set_fails(tmp_path):
    root, path, _, _, _, pack_path, output, _ = _setup_update_plan(tmp_path)
    _append_body(path)
    outside = root / 'docs' / '01_VISION.md'
    _append_body(outside)

    with pytest.raises(SyncScopeError, match='scope violation'):
        verify_sync(root, pack_path)

    report = _load_report(output)
    assert report['verification_result'] == 'failed_scope'
    assert 'docs/01_VISION.md' in report['changes_outside_scope']


def test_unexpected_untracked_file_fails(tmp_path):
    root, path, _, _, _, pack_path, output, _ = _setup_update_plan(tmp_path)
    _append_body(path)
    (root / 'unexpected.txt').write_text('unexpected\n', encoding='utf-8')

    with pytest.raises(SyncScopeError):
        verify_sync(root, pack_path)

    report = _load_report(output)
    assert 'unexpected.txt' in report['git_changes']['untracked']
    assert 'unexpected.txt' in report['changes_outside_scope']


def test_unexpected_gitignored_file_is_not_silently_ignored(tmp_path):
    root, path, _, _, _, pack_path, output, _ = _setup_update_plan(tmp_path)
    _append_body(path)
    (root / '.env').write_text('SMOKE_ONLY=1\n', encoding='utf-8')

    with pytest.raises(SyncScopeError):
        verify_sync(root, pack_path)

    report = _load_report(output)
    assert '.env' in report['git_changes']['ignored_untracked']
    assert '.env' in report['changes_outside_scope']


def test_preexisting_gitignored_baseline_is_allowed_but_drift_fails(tmp_path):
    root = init_project('Demo', tmp_path / 'demo')
    path, object_id = create_object(root, 'feature', 'Search', 'general', 'owner')
    path = path.rename(path.with_name(f'{object_id}-search.md'))
    ignored = root / '.env'
    ignored.write_text('BASELINE=1\n', encoding='utf-8')
    head = _commit_project(root)
    pack = _pack(head, [_update_change(object_id)], [object_id])
    pack_path = _write_pack(root, pack)
    _, manifest = plan_sync(root, pack_path)
    assert manifest['ignored_untracked_baseline'][0]['path'] == '.env'
    _append_body(path)

    _, passed = verify_sync(root, pack_path)
    assert passed['verification_result'] == 'passed'
    assert passed['ignored_untracked_drift'] == []

    ignored.write_text('BASELINE=2\n', encoding='utf-8')
    with pytest.raises(SyncScopeError):
        verify_sync(root, pack_path)


def test_rename_from_allowed_path_to_forbidden_path_fails(tmp_path):
    root, path, object_id, _, _, pack_path, output, _ = _setup_update_plan(tmp_path)
    forbidden = path.with_name(f'{object_id}-different-slug.md')
    _git(root, 'mv', path.relative_to(root).as_posix(), forbidden.relative_to(root).as_posix())

    with pytest.raises(SyncScopeError):
        verify_sync(root, pack_path)

    report = _load_report(output)
    assert forbidden.relative_to(root).as_posix() in report['changes_outside_scope']
    assert report['git_changes']['renamed'][0]['old_path'] == path.relative_to(root).as_posix()
    assert report['git_changes']['renamed'][0]['new_path'] == forbidden.relative_to(root).as_posix()


def test_stale_head_fails_integrity_preflight(tmp_path):
    root, path, _, _, _, pack_path, _, _ = _setup_update_plan(tmp_path)
    _append_body(path)
    _git(root, 'commit', '--allow-empty', '-qm', 'advance HEAD')

    with pytest.raises(SyncIntegrityError, match='stale HEAD'):
        verify_sync(root, pack_path)


def test_tampered_pack_fails_integrity_preflight(tmp_path):
    root, _, _, _, _, pack_path, _, _ = _setup_update_plan(tmp_path)
    pack_path.write_text(pack_path.read_text(encoding='utf-8') + '\n', encoding='utf-8')

    with pytest.raises(SyncIntegrityError, match='pack SHA-256 mismatch'):
        verify_sync(root, pack_path)


@pytest.mark.parametrize('artifact_name', ['plan.json', 'manifest.json'])
def test_tampered_plan_or_manifest_fails_integrity_preflight(tmp_path, artifact_name):
    root, _, _, _, _, pack_path, output, _ = _setup_update_plan(tmp_path)
    artifact = output / artifact_name
    data = json.loads(artifact.read_text(encoding='utf-8'))
    data['pack_content_sha256'] = '0' * 64
    artifact.write_text(json.dumps(data, indent=2) + '\n', encoding='utf-8')

    with pytest.raises(SyncIntegrityError, match='fingerprint mismatch'):
        verify_sync(root, pack_path)


def test_recomputed_integrity_cannot_authorize_path_traversal(tmp_path):
    root, _, _, _, _, pack_path, output, _ = _setup_update_plan(tmp_path)
    plan_path = output / 'plan.json'
    manifest_path = output / 'manifest.json'
    plan = json.loads(plan_path.read_text(encoding='utf-8'))
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    for document in (plan, manifest):
        document['resolved_targets'][0]['path'] = 'docs/../PROJECT_RULES.md'
        document['allowed_write_set'] = ['docs/../PROJECT_RULES.md']
        document.pop('artifact_integrity')
    integrity = artifact_integrity_block(plan, manifest)
    plan['artifact_integrity'] = integrity
    manifest['artifact_integrity'] = integrity
    plan_path.write_text(json.dumps(plan, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + '\n', encoding='utf-8')

    with pytest.raises(SyncIntegrityError, match='unsafe allowed write path'):
        verify_sync(root, pack_path)


def test_validation_failure_after_executor_edit(tmp_path):
    root, path, _, _, _, pack_path, output, _ = _setup_update_plan(tmp_path)
    text = path.read_text(encoding='utf-8').replace('status: idea', 'status: invalid-status')
    path.write_text(text, encoding='utf-8')

    with pytest.raises(SyncValidationError, match='initial validation failed'):
        verify_sync(root, pack_path)

    report = _load_report(output)
    assert report['verification_result'] == 'failed_validation'
    assert report['validation']['before_generation']['counts']['ERROR'] > 0
    assert report['validation']['generation_ran'] is False


def test_generation_and_second_validation_are_reported(tmp_path):
    root, path, _, _, _, pack_path, _, _ = _setup_update_plan(tmp_path)
    _append_body(path)

    _, report = verify_sync(root, pack_path)

    assert report['validation']['generation_ran'] is True
    assert report['validation']['before_generation']['counts']['ERROR'] == 0
    assert report['validation']['after_generation']['counts']['ERROR'] == 0
    assert (root / '.generated' / 'indexes' / 'PROJECT_MAP.md').is_file()


def test_staged_and_unstaged_changes_are_both_reported(tmp_path):
    narrative = {
        'change_id': 'product-doc',
        'kind': 'narrative_impact',
        'summary': 'Update the approved product narrative.',
        'narrative_paths': ['docs/03_PRODUCT.md'],
    }
    root, path, _, _, _, pack_path, _, _ = _setup_update_plan(
        tmp_path,
        extra_changes=[narrative],
        extra_targets=['docs/03_PRODUCT.md'],
    )
    _append_body(path)
    _git(root, 'add', path.relative_to(root).as_posix())
    narrative_path = root / 'docs' / '03_PRODUCT.md'
    _append_body(narrative_path)

    _, report = verify_sync(root, pack_path)

    staged_paths = {item.get('path') for item in report['git_changes']['staged']}
    unstaged_paths = {item.get('path') for item in report['git_changes']['unstaged']}
    assert path.relative_to(root).as_posix() in staged_paths
    assert 'docs/03_PRODUCT.md' in unstaged_paths


def test_staged_change_cannot_be_hidden_by_restoring_worktree(tmp_path):
    root, path, _, _, _, pack_path, output, _ = _setup_update_plan(tmp_path)
    _append_body(path)
    outside = root / 'docs' / '01_VISION.md'
    _append_body(outside)
    _git(root, 'add', 'docs/01_VISION.md')
    _git(root, 'restore', '--worktree', '--source=HEAD', '--', 'docs/01_VISION.md')

    with pytest.raises(SyncScopeError):
        verify_sync(root, pack_path)

    report = _load_report(output)
    assert 'docs/01_VISION.md' in report['changes_outside_scope']
    assert any(item.get('path') == 'docs/01_VISION.md' for item in report['git_changes']['staged'])


def test_partially_staged_allowed_path_is_rejected(tmp_path):
    root, path, _, _, _, pack_path, output, _ = _setup_update_plan(tmp_path)
    _append_body(path, '\nStaged version.\n')
    _git(root, 'add', path.relative_to(root).as_posix())
    _append_body(path, '\nDifferent unstaged version.\n')

    with pytest.raises(SyncScopeError):
        verify_sync(root, pack_path)

    report = _load_report(output)
    assert report['partially_staged_paths'] == [path.relative_to(root).as_posix()]


def test_verification_reports_are_deterministic(tmp_path):
    root, path, _, _, pack, _, output, _ = _setup_update_plan(tmp_path)
    _append_body(path)

    verify_sync(root, pack['pack_id'])
    first = {
        name: (output / name).read_bytes()
        for name in ('verification.json', 'verification.md', 'diff-summary.md')
    }
    verify_sync(root, pack['pack_id'])
    second = {
        name: (output / name).read_bytes()
        for name in ('verification.json', 'verification.md', 'diff-summary.md')
    }

    assert first == second


def test_unresolved_is_reminder_not_semantic_approval(tmp_path):
    unresolved = {
        'change_id': 'unresolved-ranking',
        'kind': 'unresolved',
        'summary': 'Ranking remains unresolved.',
        'proposal': 'Do not activate a ranking decision.',
    }
    root, path, _, _, _, pack_path, _, _ = _setup_update_plan(
        tmp_path,
        extra_changes=[unresolved],
    )
    _append_body(path)

    _, report = verify_sync(root, pack_path)

    assert report['verification_result'] == 'passed'
    assert report['unresolved_proposal_items'][0]['change_id'] == 'unresolved-ranking'
    assert report['semantic_meaning_verified'] is False
    assert report['human_semantic_review_required'] is True


def test_deleted_protected_file_fails_scope(tmp_path):
    root, path, _, _, _, pack_path, output, _ = _setup_update_plan(tmp_path)
    _append_body(path)
    protected = root / '.github' / 'CODEOWNERS'
    protected.unlink()

    with pytest.raises(SyncScopeError):
        verify_sync(root, pack_path)

    report = _load_report(output)
    assert '.github/CODEOWNERS' in report['git_changes']['deleted']
    assert '.github/CODEOWNERS' in report['changes_outside_scope']


def test_generation_cannot_change_non_generated_files(tmp_path, monkeypatch):
    root, path, _, _, _, pack_path, output, _ = _setup_update_plan(tmp_path)
    _append_body(path)
    from project_system import sync_verification

    real_generate = sync_verification.generate

    def unsafe_generate(project_root):
        result = real_generate(project_root)
        _append_body(Path(project_root) / 'docs' / '02_SCOPE.md')
        return result

    monkeypatch.setattr(sync_verification, 'generate', unsafe_generate)

    with pytest.raises(SyncScopeError, match='generation changed'):
        verify_sync(root, pack_path)

    report = _load_report(output)
    assert report['verification_result'] == 'failed_scope'
    assert 'generation changed files outside .generated/**' in report['errors']


def test_cli_verify_contract_and_scope_exit_code(tmp_path, monkeypatch, capsys):
    root, path, _, _, pack, _, _, _ = _setup_update_plan(tmp_path)
    _append_body(path)
    monkeypatch.chdir(root)

    main(['sync', 'verify', pack['pack_id']])
    assert '.generated/sync/' in capsys.readouterr().out.replace('\\', '/')
    (root / 'unexpected.txt').write_text('outside scope\n', encoding='utf-8')

    with pytest.raises(SystemExit) as raised:
        main(['sync', 'verify', pack['pack_id']])

    assert raised.value.code == 4
    assert 'sync verify failed [scope]' in capsys.readouterr().err


def test_planning_rejects_dirty_baseline(tmp_path):
    root = init_project('Demo', tmp_path / 'demo')
    _, object_id = create_object(root, 'feature', 'Search', 'general', 'owner')
    head = _commit_project(root)
    pack_path = _write_pack(root, _pack(head, [_update_change(object_id)], [object_id]))
    (root / 'unexpected.txt').write_text('dirty baseline\n', encoding='utf-8')

    with pytest.raises(SyncPlanError, match='planning requires a clean working tree'):
        plan_sync(root, pack_path)
