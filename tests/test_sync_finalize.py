import json
from pathlib import Path
import shutil
import subprocess

import pytest
import yaml

from project_system.cli import main
from project_system.frontmatter import read_object, write_object
from project_system.init_project import init_project
from project_system.objects import create_object
from project_system.sync_finalization import (
    SyncCommitError,
    SyncFinalizeIntegrityError,
    SyncFinalizeScopeError,
    SyncPushError,
    finalize_sync,
)
from project_system.sync_planning import plan_sync
from project_system.sync_verification import SyncScopeError, verify_sync


def _git(root, *args, check=True):
    result = subprocess.run(
        ['git', *args],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if check and result.returncode:
        raise AssertionError(result.stderr or result.stdout)
    return result.stdout.strip()


def _commit_project(root):
    _git(root, 'init', '-q')
    _git(root, 'config', 'user.email', 'tests@example.invalid')
    _git(root, 'config', 'user.name', 'Project System Tests')
    _git(root, 'add', '.')
    _git(root, 'commit', '-qm', 'base')
    return _git(root, 'rev-parse', 'HEAD')


def _pack(head, changes, expected_targets, pack_id='SYNC-20260905-facefeed'):
    return {
        'schema_version': 1,
        'pack_id': pack_id,
        'project_id': 'demo',
        'source': {'type': 'approved_discussion', 'ref': 'phase-3-tests'},
        'created_at': '2026-09-05T12:00:00+03:00',
        'base_commit': head,
        'approval': {
            'approved_by': 'project-owner',
            'approved_at': '2026-09-05T12:01:00+03:00',
        },
        'change_class': 'C',
        'changes': changes,
        'expected_targets': expected_targets,
        'notes': 'Deterministic finalization test.',
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


def _setup_plan(tmp_path, *, with_remote=False):
    root = init_project('Demo', tmp_path / 'demo')
    path, object_id = create_object(root, 'feature', 'Search', 'general', 'owner')
    path = path.rename(path.with_name(f'{object_id}-search.md'))
    head = _commit_project(root)
    remote = None
    if with_remote:
        remote = tmp_path / 'remote.git'
        subprocess.run(['git', 'init', '--bare', '-q', str(remote)], check=True)
        _git(root, 'remote', 'add', 'origin', str(remote))
        _git(root, 'push', '-u', 'origin', 'HEAD')
    pack = _pack(head, [_update_change(object_id)], [object_id])
    pack_path = _write_pack(root, pack)
    output, _ = plan_sync(root, pack_path)
    return root, path, object_id, head, pack, pack_path, output, remote


def _setup_verified(tmp_path, *, with_remote=False):
    values = _setup_plan(tmp_path, with_remote=with_remote)
    root, path, _, _, _, pack_path, _, _ = values
    path.write_text(
        path.read_text(encoding='utf-8') + '\nExternally approved edit.\n',
        encoding='utf-8',
    )
    verify_sync(root, pack_path)
    return values


def _load_finalization(output):
    return json.loads((output / 'finalization.json').read_text(encoding='utf-8'))


def test_successful_dry_run_creates_no_commit(tmp_path):
    root, path, _, head, _, pack_path, output, _ = _setup_verified(tmp_path)

    actual_output, report = finalize_sync(root, pack_path)

    assert actual_output == output
    assert report['state'] == 'prepared'
    assert report['commit_requested'] is False
    assert report['commit_result'] == 'not_requested'
    assert report['push_requested'] is False
    assert report['semantic_meaning_verified_by_cli'] is False
    assert report['human_semantic_approval_required_before_commit'] is True
    assert _git(root, 'rev-parse', 'HEAD') == head
    assert _git(root, 'diff', '--cached', '--name-only') == ''
    assert {'finalization.json', 'finalization.md'} <= {item.name for item in output.iterdir()}
    assert path.relative_to(root).as_posix() in report['verified_canonical_paths']


def test_explicit_commit_stages_only_verified_paths_and_never_pushes(tmp_path):
    root, path, _, head, _, pack_path, output, remote = _setup_verified(
        tmp_path,
        with_remote=True,
    )
    upstream_before = _git(remote, 'rev-parse', 'HEAD')

    _, report = finalize_sync(root, pack_path, commit=True)

    assert report['state'] == 'committed'
    assert report['commit_result'] == 'committed'
    assert report['push_result'] == 'not_requested'
    assert report['commit_message'] == f'sync: apply {report["pack_id"]}'
    assert report['commit_sha'] == _git(root, 'rev-parse', 'HEAD')
    assert report['commit_sha'] != head
    expected = path.relative_to(root).as_posix()
    assert report['committed_paths'] == [expected]
    assert _git(root, 'show', '--pretty=', '--name-only', 'HEAD') == expected
    assert _git(root, 'ls-files', '.generated') == '.generated/.gitkeep'
    assert _git(remote, 'rev-parse', 'HEAD') == upstream_before
    persisted = _load_finalization(output)
    assert persisted['commit_sha'] == report['commit_sha']


def test_custom_message_and_repeat_commit_are_idempotent(tmp_path):
    root, _, _, _, _, pack_path, _, _ = _setup_verified(tmp_path)
    _, first = finalize_sync(root, pack_path, commit=True, message='approved sync state')

    _, second = finalize_sync(root, pack_path, commit=True)

    assert second['commit_result'] == 'already_committed'
    assert second['commit_sha'] == first['commit_sha']
    assert second['commit_message'] == 'approved sync state'
    assert _git(root, 'rev-list', '--count', first['base_commit'] + '..HEAD') == '1'


def test_successful_push_and_repeat_push_are_idempotent(tmp_path, monkeypatch):
    root, _, _, _, _, pack_path, _, remote = _setup_verified(tmp_path, with_remote=True)
    from project_system import sync_finalization

    calls = []
    real_run_git = sync_finalization._run_git

    def recording_git(root_arg, args, **kwargs):
        calls.append(list(args))
        return real_run_git(root_arg, args, **kwargs)

    monkeypatch.setattr(sync_finalization, '_run_git', recording_git)
    _, committed = finalize_sync(root, pack_path, commit=True, push=True)
    _, repeated = finalize_sync(root, pack_path, push=True)

    assert committed['push_result'] == 'pushed'
    assert repeated['push_result'] == 'already_synchronized'
    assert _git(remote, 'rev-parse', 'HEAD') == committed['commit_sha']
    assert not any('--force' in item or '--force-with-lease' in item for call in calls for item in call)
    assert not any(call and call[0] in {'reset', 'checkout', 'clean'} for call in calls)


def test_push_never_implicitly_commits(tmp_path):
    root, _, _, head, _, pack_path, output, _ = _setup_verified(tmp_path)

    with pytest.raises(SyncPushError):
        finalize_sync(root, pack_path, push=True)

    assert _git(root, 'rev-parse', 'HEAD') == head
    report = _load_finalization(output)
    assert report['commit_sha'] is None
    assert report['push_result'] == 'failed'


def test_missing_and_failed_verification_are_rejected(tmp_path):
    root, path, _, _, _, pack_path, _, _ = _setup_plan(tmp_path)
    with pytest.raises(SyncFinalizeIntegrityError, match='verification'):
        finalize_sync(root, pack_path)

    path.write_text(path.read_text(encoding='utf-8') + '\nAllowed.\n', encoding='utf-8')
    (root / 'unexpected.txt').write_text('outside', encoding='utf-8')
    with pytest.raises(SyncScopeError):
        verify_sync(root, pack_path)
    with pytest.raises(SyncFinalizeIntegrityError, match='successful sync verify'):
        finalize_sync(root, pack_path)


def test_stale_verification_after_canonical_edit_is_rejected(tmp_path):
    root, path, _, head, _, pack_path, _, _ = _setup_verified(tmp_path)
    path.write_text(path.read_text(encoding='utf-8') + '\nChanged later.\n', encoding='utf-8')

    with pytest.raises(SyncFinalizeIntegrityError, match='stale'):
        finalize_sync(root, pack_path, commit=True)

    assert _git(root, 'rev-parse', 'HEAD') == head


def test_tampered_verification_artifact_is_rejected(tmp_path):
    root, _, _, _, _, pack_path, output, _ = _setup_verified(tmp_path)
    report = json.loads((output / 'verification.json').read_text(encoding='utf-8'))
    report['warnings'].append('tampered')
    (output / 'verification.json').write_text(json.dumps(report), encoding='utf-8')

    with pytest.raises(SyncFinalizeIntegrityError, match='tampered'):
        finalize_sync(root, pack_path)


def test_changed_head_after_verify_is_rejected(tmp_path):
    root, _, _, _, _, pack_path, _, _ = _setup_verified(tmp_path)
    _git(root, 'commit', '--allow-empty', '-qm', 'unrelated history')

    with pytest.raises(SyncFinalizeIntegrityError, match='HEAD changed'):
        finalize_sync(root, pack_path)


@pytest.mark.parametrize('kind', ['staged', 'unstaged', 'untracked'])
def test_unrelated_changes_after_verify_are_rejected(tmp_path, kind):
    root, _, _, _, _, pack_path, _, _ = _setup_verified(tmp_path)
    if kind == 'untracked':
        (root / 'unexpected.txt').write_text('outside', encoding='utf-8')
    else:
        path = root / 'README.md'
        path.write_text(path.read_text(encoding='utf-8') + '\noutside\n', encoding='utf-8')
        if kind == 'staged':
            _git(root, 'add', '--', 'README.md')

    with pytest.raises(SyncFinalizeScopeError):
        finalize_sync(root, pack_path, commit=True)


def test_allowed_create_update_delete_are_staged_and_committed_exactly(tmp_path):
    root = init_project('Demo', tmp_path / 'demo')
    update_path, update_id = create_object(root, 'feature', 'Update', 'general', 'owner')
    delete_path, delete_id = create_object(root, 'feature', 'Delete', 'general', 'owner')
    head = _commit_project(root)
    create_id = 'FEAT-20260905-cafebabe'
    create_relative = f'knowledge/features/{create_id}-created.md'
    changes = [
        _update_change(update_id),
        {
            'change_id': 'retire-delete',
            'kind': 'retire_object',
            'summary': 'Remove approved legacy object.',
            'target_id': delete_id,
            'new_status': 'removed',
        },
        {
            'change_id': 'create-approved',
            'kind': 'create_object',
            'summary': 'Create approved object.',
            'object': {
                'id': create_id,
                'type': 'feature',
                'title': 'Created',
                'slug': 'created',
                'domain': 'general',
                'status': 'planned',
                'body': '# Created\n',
            },
        },
    ]
    pack = _pack(head, changes, [update_id, delete_id, create_id])
    pack_path = _write_pack(root, pack)
    plan_sync(root, pack_path)
    update_path.write_text(update_path.read_text(encoding='utf-8') + '\nUpdated.\n', encoding='utf-8')
    delete_path.unlink()
    write_object(
        root / create_relative,
        {
            'schema_version': 1,
            'id': create_id,
            'type': 'feature',
            'title': 'Created',
            'domain': 'general',
            'status': 'planned',
            'owner': 'owner',
            'created_at': '2026-09-05',
            'source': {'type': 'owner_decision', 'ref': pack['pack_id']},
            'depends_on': [],
        },
        '# Created\n',
    )
    verify_sync(root, pack_path)

    _, report = finalize_sync(root, pack_path, commit=True)

    expected = sorted([
        update_path.relative_to(root).as_posix(),
        delete_path.relative_to(root).as_posix(),
        create_relative,
    ])
    assert report['committed_paths'] == expected
    assert _git(root, 'diff', '--cached', '--name-only') == ''
    assert _git(root, 'status', '--short', '--untracked-files=all').splitlines() == [
        '?? inbox/sync/SYNC-20260905-facefeed.yaml'
    ]


def test_allowed_rename_is_staged_as_exact_old_and_new_paths(tmp_path):
    root = init_project('Demo', tmp_path / 'demo')
    old_path, old_id = create_object(root, 'feature', 'Rename', 'general', 'owner')
    head = _commit_project(root)
    new_id = 'FEAT-20260905-deadbeef'
    new_path = old_path.with_name(f'{new_id}-renamed.md')
    pack = _pack(
        head,
        [
            {
                'change_id': 'retire-old',
                'kind': 'retire_object',
                'summary': 'Replace old identity.',
                'target_id': old_id,
                'new_status': 'removed',
            },
            {
                'change_id': 'create-new',
                'kind': 'create_object',
                'summary': 'Create replacement identity.',
                'object': {
                    'id': new_id,
                    'type': 'feature',
                    'title': 'Rename',
                    'slug': 'renamed',
                    'domain': 'general',
                    'status': 'planned',
                    'body': '# Summary\n',
                },
            },
        ],
        [old_id, new_id],
    )
    pack_path = _write_pack(root, pack)
    plan_sync(root, pack_path)
    shutil.move(old_path, new_path)
    data, body = read_object(new_path)
    data['id'] = new_id
    write_object(new_path, data, body)
    verify_sync(root, pack_path)

    _, report = finalize_sync(root, pack_path, commit=True)

    assert report['committed_paths'] == sorted([
        old_path.relative_to(root).as_posix(),
        new_path.relative_to(root).as_posix(),
    ])
    assert _git(root, 'show', '--format=', '--name-status', '-M', 'HEAD')


def test_commit_failure_restores_prior_index_without_touching_worktree(tmp_path, monkeypatch):
    root, path, _, head, _, pack_path, output, _ = _setup_verified(tmp_path)
    content_before = path.read_bytes()
    from project_system import sync_finalization

    real_run_git = sync_finalization._run_git

    def failing_commit(root_arg, args, **kwargs):
        if args and args[0] == 'commit':
            return subprocess.CompletedProcess(['git', *args], 1, '', 'hook rejected commit')
        return real_run_git(root_arg, args, **kwargs)

    monkeypatch.setattr(sync_finalization, '_run_git', failing_commit)
    with pytest.raises(SyncCommitError, match='hook rejected'):
        finalize_sync(root, pack_path, commit=True)

    assert _git(root, 'rev-parse', 'HEAD') == head
    assert _git(root, 'diff', '--cached', '--name-only') == ''
    assert path.read_bytes() == content_before
    assert _load_finalization(output)['commit_result'] == 'failed'


def test_push_failure_keeps_local_commit_and_is_reported(tmp_path, monkeypatch):
    root, _, _, _, _, pack_path, output, _ = _setup_verified(tmp_path, with_remote=True)
    _, committed = finalize_sync(root, pack_path, commit=True)
    commit_sha = committed['commit_sha']
    from project_system import sync_finalization

    real_run_git = sync_finalization._run_git

    def failing_push(root_arg, args, **kwargs):
        if args == ['push']:
            return subprocess.CompletedProcess(['git', 'push'], 1, '', 'remote rejected')
        return real_run_git(root_arg, args, **kwargs)

    monkeypatch.setattr(sync_finalization, '_run_git', failing_push)
    with pytest.raises(SyncPushError, match='commit succeeded'):
        finalize_sync(root, pack_path, push=True)

    assert _git(root, 'rev-parse', 'HEAD') == commit_sha
    report = _load_finalization(output)
    assert report['commit_sha'] == commit_sha
    assert report['state'] == 'committed'
    assert report['push_result'] == 'failed'


def test_detached_head_and_merge_state_fail_closed(tmp_path):
    root, _, _, head, _, pack_path, _, _ = _setup_verified(tmp_path)
    _git(root, 'checkout', '--detach', '-q', head)
    with pytest.raises(SyncFinalizeScopeError, match='detached'):
        finalize_sync(root, pack_path)

    _git(root, 'checkout', '-q', '-')
    git_dir = Path(_git(root, 'rev-parse', '--git-dir'))
    (root / git_dir / 'MERGE_HEAD').write_text(head + '\n', encoding='ascii')
    with pytest.raises(SyncFinalizeScopeError, match='operation in progress'):
        finalize_sync(root, pack_path)


def test_unresolved_conflict_preflight_fails_closed(tmp_path, monkeypatch):
    root, _, _, _, _, pack_path, output, _ = _setup_verified(tmp_path)
    from project_system import sync_finalization

    real_run_git = sync_finalization._run_git

    def conflict_git(root_arg, args, **kwargs):
        if args == ['diff', '--name-only', '--diff-filter=U', '-z']:
            return subprocess.CompletedProcess(
                ['git', *args],
                0,
                b'knowledge/features/conflicted.md\0',
                b'',
            )
        return real_run_git(root_arg, args, **kwargs)

    monkeypatch.setattr(sync_finalization, '_run_git', conflict_git)
    with pytest.raises(SyncFinalizeScopeError, match='unresolved conflicts'):
        finalize_sync(root, pack_path)
    assert _load_finalization(output)['state'] == 'failed'


def test_ignored_file_drift_after_verify_is_rejected(tmp_path):
    root = init_project('Demo', tmp_path / 'demo')
    path, object_id = create_object(root, 'feature', 'Search', 'general', 'owner')
    ignored = root / '.env'
    ignored.write_text('baseline', encoding='utf-8')
    head = _commit_project(root)
    pack = _pack(head, [_update_change(object_id)], [object_id])
    pack_path = _write_pack(root, pack)
    plan_sync(root, pack_path)
    path.write_text(path.read_text(encoding='utf-8') + '\nAllowed.\n', encoding='utf-8')
    verify_sync(root, pack_path)
    ignored.write_text('drift', encoding='utf-8')

    with pytest.raises(SyncFinalizeScopeError, match='differs outside'):
        finalize_sync(root, pack_path)


def test_cli_contract_and_exit_codes(tmp_path, monkeypatch, capsys):
    root, path, _, _, _, pack_path, _, _ = _setup_plan(tmp_path)
    monkeypatch.chdir(root)
    with pytest.raises(SystemExit) as exc:
        main(['sync', 'finalize', str(pack_path)])
    assert exc.value.code == 3
    assert 'sync finalize failed [integrity]' in capsys.readouterr().err

    path.write_text(path.read_text(encoding='utf-8') + '\nAllowed.\n', encoding='utf-8')
    verify_sync(root, pack_path)
    main(['sync', 'finalize', str(pack_path)])
    assert '.generated' in capsys.readouterr().out


def test_message_requires_explicit_commit(tmp_path):
    root, _, _, _, _, pack_path, _, _ = _setup_verified(tmp_path)
    with pytest.raises(SyncCommitError, match='requires --commit'):
        finalize_sync(root, pack_path, message='not authorized')
