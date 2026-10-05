import json
from pathlib import Path
import shutil
import subprocess

import pytest
import yaml

from project_system import sync_verification, validation as validation_module
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


def _setup_plan(tmp_path, *, with_remote=False, configure=None):
    root = init_project('Demo', tmp_path / 'demo')
    path, object_id = create_object(root, 'feature', 'Search', 'general', 'owner')
    path = path.rename(path.with_name(f'{object_id}-search.md'))
    if configure is not None:
        configure(root, path, object_id)
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


def _setup_verified(tmp_path, *, with_remote=False, configure=None):
    values = _setup_plan(
        tmp_path, with_remote=with_remote, configure=configure,
    )
    root, path, _, _, _, pack_path, _, _ = values
    path.write_text(
        path.read_text(encoding='utf-8') + '\nExternally approved edit.\n',
        encoding='utf-8',
    )
    verify_sync(root, pack_path)
    return values


def _load_finalization(output):
    return json.loads((output / 'finalization.json').read_text(encoding='utf-8'))


def _write_rule_layer(root, rules, exceptions=None):
    (root / '.project/policies/rules.yaml').write_text(
        yaml.safe_dump({
            'schema_version': 1,
            'profile': 'project-system-rules-v1',
            'rules': rules,
        }, sort_keys=False),
        encoding='utf-8',
    )
    (root / '.project/policies/rule_exceptions.yaml').write_text(
        yaml.safe_dump({
            'schema_version': 1,
            'profile': 'project-system-rule-exceptions-v1',
            'exceptions': exceptions or {},
        }, sort_keys=False),
        encoding='utf-8',
    )


def _temporary_waiver_configuration(root, path, object_id):
    _, decision_id = create_object(
        root, 'decision', 'Temporary SYNC waiver', 'governance', 'project-owner',
    )
    _write_rule_layer(
        root,
        {
            'REPO-001': {
                'title': 'Temporary waiver rule',
                'status': 'active',
                'category': 'repository',
                'description': 'Require a deliberately missing path during SYNC.',
                'verification': {
                    'method': 'deterministic',
                    'checker': 'repository.required_path',
                    'parameters': {'path': 'missing-sync-required'},
                },
                'enforcement': {
                    'severity': 'BLOCKING',
                    'checkpoints': ['sync_verify'],
                },
                'exception_policy': 'decision_required',
            }
        },
        {
            'EXC-20260927-abcdef12': {
                'rule_id': 'REPO-001',
                'state': 'active',
                'mode': 'temporary',
                'reason': 'Temporary Stage 6 test waiver.',
                'scope': {'paths': ['missing-sync-required']},
                'decision_id': decision_id,
                'approved_by': 'project-owner',
                'approved_at': '2026-09-27T10:00:00Z',
                'expires_at': '2026-09-27T12:00:00Z',
            }
        },
    )


def _set_rule_time(monkeypatch, hour, minute=0):
    monkeypatch.setattr(
        validation_module,
        '_utc_now',
        lambda: validation_module.datetime(
            2026, 9, 27, hour, minute, tzinfo=validation_module.timezone.utc
        ),
    )


def test_successful_dry_run_creates_no_commit(tmp_path):
    root, path, _, head, _, pack_path, output, _ = _setup_verified(tmp_path)

    actual_output, report = finalize_sync(root, pack_path)

    assert actual_output == output
    assert report['state'] == 'prepared'
    assert report['commit_requested'] is False
    assert report['commit_result'] == 'not_requested'
    assert report['push_requested'] is False
    assert report['rule_evidence_fingerprint'] is None
    assert 'Rule Evidence fingerprint: `none`' in (
        output / 'finalization.md'
    ).read_text(encoding='utf-8')
    assert report['semantic_meaning_verified_by_cli'] is False
    assert report['human_semantic_approval_required_before_commit'] is True
    assert report['selected_skills']
    assert report['skills_registry_sha256']
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


def test_matching_rule_evidence_is_rechecked_and_bound_to_finalization(
    tmp_path,
    monkeypatch,
):
    _set_rule_time(monkeypatch, 11)
    root, _, _, _, _, pack_path, output, _ = _setup_verified(
        tmp_path,
        configure=_temporary_waiver_configuration,
    )
    verification = json.loads(
        (output / 'verification.json').read_text(encoding='utf-8')
    )
    captured = []
    actual_evaluate = validation_module.evaluate_rules

    def capture_context(registry, evaluation_context):
        captured.append(evaluation_context)
        return actual_evaluate(registry, evaluation_context)

    monkeypatch.setattr(validation_module, 'evaluate_rules', capture_context)

    _, report = finalize_sync(root, pack_path)

    expected = verification['rule_evidence_binding']['evidence']['evidence_fingerprint']
    assert report['state'] == 'prepared'
    assert report['rule_evidence_fingerprint'] == expected
    assert _load_finalization(output)['rule_evidence_fingerprint'] == expected
    assert captured[-1].checkpoint == 'sync_verify'
    assert captured[-1].evaluation_paths == tuple(
        verification['actual_changed_canonical_paths']
    )


@pytest.mark.parametrize('inventory_drift', [False, True])
def test_dart_test_recheck_ignores_transcript_volatility_but_binds_inventory(
    tmp_path, monkeypatch, inventory_drift,
):
    from project_system import dart_test_adapter

    def configure(root, path, object_id):
        for name in ('first', 'second'):
            test_path = root / 'test' / f'{name}_test.dart'
            test_path.parent.mkdir(exist_ok=True)
            test_path.write_text('void main() {}\n', encoding='utf-8')
        _write_rule_layer(root, {
            'TEST-001': {
                'title': 'Run tests', 'status': 'active', 'category': 'testing',
                'description': 'Verify the full Dart test suite.',
                'verification': {'method': 'deterministic', 'checker': 'code.verification',
                                 'parameters': {'adapter': 'dart.test'}},
                'enforcement': {'severity': 'BLOCKING',
                                'checkpoints': ['project_validate', 'sync_verify']},
                'exception_policy': 'forbidden',
            },
        })

    calls = []
    drift = False

    def run(arguments, **kwargs):
        from subprocess import CompletedProcess

        calls.append(tuple(arguments))
        alternate = len(calls) % 2 == 0
        events = [{'type': 'start', 'protocolVersion': '0.1.1',
                   'runnerVersion': '1.31.0', 'pid': 1234 + len(calls)},
                  {'type': 'allSuites', 'count': 2}]
        suite_order = [('first', 0, 1), ('second', 2, 3)]
        if alternate:
            suite_order.reverse()
        completions = []
        for name, suite_id, test_id in suite_order:
            events.extend([
                {'type': 'suite', 'suite': {'id': suite_id, 'platform': 'vm',
                                          'path': f'test/{name}_test.dart'}},
                {'type': 'testStart', 'test': {
                    'id': test_id, 'name': name + (' changed' if drift else ''),
                    'suiteID': suite_id, 'groupIDs': [],
                    'line': None, 'column': None, 'url': None,
                    'metadata': {'skip': False, 'skipReason': None},
                }},
            ])
            completions.append({'type': 'testDone', 'testID': test_id,
                                'result': 'success', 'hidden': False, 'skipped': False})
        events.extend(reversed(completions) if alternate else completions)
        events.append({'type': 'done', 'success': True})
        for index, event in enumerate(events):
            event['time'] = index * len(calls)
        return CompletedProcess(arguments, 0, '\n'.join(map(json.dumps, events)), '')

    monkeypatch.setattr(dart_test_adapter, 'run_process', run)
    root, _, _, head, _, pack_path, output, _ = _setup_verified(
        tmp_path, configure=configure,
    )
    verification = json.loads((output / 'verification.json').read_text(encoding='utf-8'))
    item = verification['rule_evidence_binding']['evidence']['results'][0]
    assert item['raw_status'] == 'PASS'
    assert item['details']['evaluation_mode'] == 'project_wide_invalidation'
    drift = inventory_drift
    before = len(calls)
    if inventory_drift:
        with pytest.raises(SyncFinalizeIntegrityError, match='Rule Evidence|sync verify again'):
            finalize_sync(root, pack_path)
    else:
        _, report = finalize_sync(root, pack_path)
        assert report['state'] == 'prepared'
        assert report['rule_evidence_fingerprint'] == (
            verification['rule_evidence_binding']['evidence']['evidence_fingerprint']
        )
    assert len(calls) > before
    assert _git(root, 'rev-parse', 'HEAD') == head
    assert _git(root, 'diff', '--cached', '--name-only') == ''


def test_temporary_waiver_expiry_after_verify_fails_before_staging(
    tmp_path,
    monkeypatch,
):
    _set_rule_time(monkeypatch, 11)
    root, path, _, head, _, pack_path, _, _ = _setup_verified(
        tmp_path,
        configure=_temporary_waiver_configuration,
    )
    content_before = path.read_bytes()
    index_before = _git(root, 'diff', '--cached', '--name-only')
    _set_rule_time(monkeypatch, 13)

    with pytest.raises(SyncFinalizeIntegrityError, match='sync verify again'):
        finalize_sync(root, pack_path, commit=True)

    assert _git(root, 'rev-parse', 'HEAD') == head
    assert _git(root, 'diff', '--cached', '--name-only') == index_before == ''
    assert path.read_bytes() == content_before


def test_tampered_nested_rule_evidence_resealed_only_outside_is_rejected(
    tmp_path,
    monkeypatch,
):
    _set_rule_time(monkeypatch, 11)
    root, _, _, _, _, pack_path, output, _ = _setup_verified(
        tmp_path,
        configure=_temporary_waiver_configuration,
    )
    verification_path = output / 'verification.json'
    report = json.loads(verification_path.read_text(encoding='utf-8'))
    report['rule_evidence_binding']['evidence']['results'][0]['details']['path'] = 'tampered'
    sync_verification._seal_verification_report(report)
    verification_path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + '\n', encoding='utf-8'
    )

    with pytest.raises(SyncFinalizeIntegrityError, match='Rule Evidence'):
        finalize_sync(root, pack_path)


def test_tampered_rule_binding_state_fingerprint_is_rejected(tmp_path):
    root, _, _, _, _, pack_path, output, _ = _setup_verified(tmp_path)
    verification_path = output / 'verification.json'
    report = json.loads(verification_path.read_text(encoding='utf-8'))
    report['rule_evidence_binding']['verified_working_tree_fingerprint'] = '0' * 64
    sync_verification._seal_verification_report(report)
    verification_path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + '\n', encoding='utf-8'
    )

    with pytest.raises(SyncFinalizeIntegrityError, match='working-tree fingerprint'):
        finalize_sync(root, pack_path)


def test_missing_stage6_rule_binding_requires_fresh_verification(tmp_path):
    root, _, _, _, _, pack_path, output, _ = _setup_verified(tmp_path)
    verification_path = output / 'verification.json'
    report = json.loads(verification_path.read_text(encoding='utf-8'))
    report.pop('rule_evidence_binding')
    sync_verification._seal_verification_report(report)
    verification_path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + '\n', encoding='utf-8'
    )

    with pytest.raises(SyncFinalizeIntegrityError, match='sync verify again'):
        finalize_sync(root, pack_path)


def test_committed_retry_does_not_reopen_expired_temporary_waiver(
    tmp_path,
    monkeypatch,
):
    _set_rule_time(monkeypatch, 11)
    root, _, _, _, _, pack_path, _, _ = _setup_verified(
        tmp_path,
        configure=_temporary_waiver_configuration,
    )
    _, committed = finalize_sync(root, pack_path, commit=True)
    _set_rule_time(monkeypatch, 13)

    _, repeated = finalize_sync(root, pack_path, commit=True)

    assert repeated['commit_result'] == 'already_committed'
    assert repeated['commit_sha'] == committed['commit_sha']


def test_previous_committed_finalization_must_match_rule_evidence_fingerprint(
    tmp_path,
    monkeypatch,
):
    from project_system import sync_finalization

    _set_rule_time(monkeypatch, 11)
    root, _, _, _, _, pack_path, output, _ = _setup_verified(
        tmp_path,
        configure=_temporary_waiver_configuration,
    )
    finalize_sync(root, pack_path, commit=True)
    finalization_path = output / 'finalization.json'
    report = json.loads(finalization_path.read_text(encoding='utf-8'))
    report['rule_evidence_fingerprint'] = '0' * 64
    sync_finalization._seal_finalization_report(report)
    finalization_path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + '\n', encoding='utf-8'
    )

    with pytest.raises(
        SyncFinalizeIntegrityError,
        match='rule_evidence_fingerprint',
    ):
        finalize_sync(root, pack_path, commit=True)

def test_push_retry_after_commit_does_not_reopen_expired_temporary_waiver(
    tmp_path,
    monkeypatch,
):
    _set_rule_time(monkeypatch, 11)
    root, _, _, _, _, pack_path, _, remote = _setup_verified(
        tmp_path,
        with_remote=True,
        configure=_temporary_waiver_configuration,
    )

    _, committed = finalize_sync(root, pack_path, commit=True)
    _set_rule_time(monkeypatch, 13)

    _, pushed = finalize_sync(root, pack_path, push=True)

    assert pushed['commit_result'] == 'already_committed'
    assert pushed['push_result'] == 'pushed'
    assert pushed['commit_sha'] == committed['commit_sha']
    assert _git(remote, 'rev-parse', 'HEAD') == committed['commit_sha']


def _setup_osv_verified(tmp_path, monkeypatch, *, severity='BLOCKING', waiver=False):
    """Real SYNC lifecycle and OSV parser; only external process transport is fake."""
    from project_system import osv_scan_adapter, sync_finalization

    state = {
        'payload': {'results': [{
            'source': {'type': 'lockfile', 'path': 'pubspec.lock'},
            'packages': [
                {'package': {'ecosystem': 'Pub', 'name': name, 'version': '1.2.3'}}
                for name in ('first', 'second')
            ],
        }]},
        'exit_code': 0, 'stderr': '', 'pretty': False, 'error': None,
        'tool_version': '2.3.3', 'scans': [], 'rechecks': [],
    }
    tool = tmp_path / 'osv-scanner.exe'
    tool.write_bytes(b'external fixture executable\n')
    real_which = shutil.which
    monkeypatch.setattr(
        osv_scan_adapter.shutil, 'which',
        lambda name: str(tool) if name == 'osv-scanner' else real_which(name),
    )

    def run(arguments, **kwargs):
        if arguments[1:] == ['--version']:
            return subprocess.CompletedProcess(
                arguments, 0, f"osv-scanner version: {state['tool_version']}\n", '',
            )
        assert arguments[1:3] == ['scan', 'source']
        assert kwargs['shell'] is False
        config = Path(arguments[arguments.index('--config') + 1])
        assert config.is_file() and config.read_bytes() == b''
        assert not config.resolve().is_relative_to((tmp_path / 'demo').resolve())
        assert Path(kwargs['cwd']) == config.parent
        state['scans'].append(tuple(arguments))
        if state['error'] is not None:
            raise state['error']
        return subprocess.CompletedProcess(
            arguments, state['exit_code'],
            json.dumps(state['payload'], sort_keys=state['pretty'],
                       indent=2 if state['pretty'] else None),
            state['stderr'],
        )

    monkeypatch.setattr(osv_scan_adapter, 'run_process', run)
    real_validate = sync_finalization.validate_report

    def observe_recheck(root, **kwargs):
        # Observation only: do not substitute Rule results, Evidence or adapters.
        current = real_validate(root, **kwargs)
        state['rechecks'].append((kwargs, current))
        return current

    monkeypatch.setattr(sync_finalization, 'validate_report', observe_recheck)

    def configure(root, path, object_id):
        (root / 'pubspec.lock').write_text('resolved lockfile fixture\n', encoding='utf-8')
        exceptions = {}
        if waiver:
            _, decision_id = create_object(
                root, 'decision', 'Governed dependency exception', 'security', 'project-owner',
            )
            exceptions['EXC-20260927-abcdef12'] = {
                'rule_id': 'SEC-001', 'state': 'active', 'mode': 'permanent',
                'reason': 'Isolated test of governed dependency waiver.',
                'scope': {'paths': ['pubspec.lock']}, 'decision_id': decision_id,
                'approved_by': 'project-owner', 'approved_at': '2026-09-27T10:00:00Z',
            }
        _write_rule_layer(root, {
            'SEC-001': {
                'title': 'Resolved dependency verification', 'status': 'active',
                'category': 'security', 'description': 'Verify dependency vulnerability state.',
                'verification': {'method': 'deterministic', 'checker': 'code.verification',
                                 'parameters': {'adapter': 'osv.scan'}},
                'enforcement': {'severity': severity,
                                'checkpoints': ['project_validate', 'sync_verify']},
                'exception_policy': 'decision_required' if waiver else 'forbidden',
            },
        }, exceptions)

    values = _setup_plan(tmp_path, configure=configure)
    root, path, _, head, _, pack_path, output, _ = values
    scans_before_verify = len(state['scans'])
    path.write_text(path.read_text(encoding='utf-8') + '\nExternally approved edit.\n',
                    encoding='utf-8')
    verify_sync(root, pack_path)
    verified = json.loads((output / 'verification.json').read_text(encoding='utf-8'))
    assert verified['verification_result'] == 'passed'
    assert len(state['scans']) > scans_before_verify
    binding = verified['rule_evidence_binding']
    assert binding['checkpoint'] == 'sync_verify'
    assert binding['verified_working_tree_fingerprint'] == verified['verification_fingerprint']
    item, = binding['evidence']['results']
    assert item['rule_id'] == 'SEC-001'
    assert (item['raw_status'], item['effective_status']) == ('PASS', 'PASS')
    assert item['details']['adapter_id'] == 'osv.scan'
    assert item['details']['adapter_version'] == '1'
    assert item['details']['evaluation_mode'] == 'project_wide_invalidation'
    assert binding['evidence']['schema_version'] == 1
    return root, head, pack_path, output, verified, state


def _osv_vulnerability(state):
    state['exit_code'] = 1
    state['payload']['results'][0]['packages'][0].update(
        vulnerabilities=[{'id': 'OSV-1', 'aliases': ['CVE-2026-1234'],
                          'summary': 'raw advisory prose'}],
        groups=[{'ids': ['OSV-1'], 'aliases': ['OSV-1', 'CVE-2026-1234']}],
    )


def _osv_repository_identity(root, pack_path):
    """Both unchanged file bytes/Git state and the real verified-state digest."""
    integrity = sync_verification._resolve_integrity_inputs(root, pack_path)
    plan = integrity['plan']
    changes = sync_verification.collect_git_changes(root, plan['base_commit'])
    scope = sync_verification._scope_analysis(
        root, changes, plan['allowed_write_set'], integrity['pack_path'],
        plan['ignored_untracked_baseline'],
    )
    # Finalization reports are disposable .generated output, not verified inputs.
    comparable_changes = dict(changes)
    comparable_changes['ignored_untracked'] = [
        path for path in changes['ignored_untracked']
        if not sync_verification._is_generated(path)
    ]
    return (
        sync_verification._snapshot_non_generated(root), comparable_changes,
        sync_verification.verified_working_tree_state(root, plan, scope),
        _git(root, 'rev-parse', 'HEAD'),
        _git(root, 'diff', '--cached', '--binary'),
        _git(root, 'diff', '--binary'),
        _git(root, 'status', '--short', '--untracked-files=all'),
    )


def _osv_fresh_binding(verified, state):
    options, current = state['rechecks'][-1]
    assert options['rule_checkpoint'] == 'sync_verify'
    assert options['rule_base_commit'] == verified['base_commit']
    assert options['rule_evaluation_paths'] == tuple(verified['actual_changed_canonical_paths'])
    return current, sync_verification.build_rule_evidence_binding(
        current, verified['verification_fingerprint'],
    )


def _assert_osv_blocked(output, head, root):
    report = _load_finalization(output)
    assert report['state'] == 'failed'
    assert report['commit_sha'] is None
    assert report['commit_requested'] is False
    assert report['commit_result'] == 'not_requested'
    assert report['push_requested'] is False
    assert report['push_result'] == 'not_requested'
    assert _git(root, 'rev-parse', 'HEAD') == head
    assert _git(root, 'diff', '--cached', '--name-only') == ''


def test_osv_finalize_reexecutes_semantically_equivalent_pass(tmp_path, monkeypatch):
    root, head, pack_path, output, verified, state = _setup_osv_verified(tmp_path, monkeypatch)
    identity = _osv_repository_identity(root, pack_path)
    assert identity[2] == (verified['verified_working_tree_state'], verified['verification_fingerprint'])
    scans_before = len(state['scans'])
    raw_before = json.dumps(state['payload'])
    state['pretty'] = True
    state['stderr'] = 'volatile transport diagnostic; not semantic evidence'
    state['payload']['results'][0]['packages'].reverse()
    # Equivalent empty advisory/group representations are already accepted by the parser.
    for package in state['payload']['results'][0]['packages']:
        package.update(vulnerabilities=[], groups=[])
    assert json.dumps(state['payload'], sort_keys=True, indent=2) != raw_before

    _, report = finalize_sync(root, pack_path)

    assert len(state['scans']) > scans_before
    current, rebuilt = _osv_fresh_binding(verified, state)
    old_details = verified['rule_evidence_binding']['evidence']['results'][0]['details']
    new_details = rebuilt['evidence']['results'][0]['details']
    assert new_details['semantic_sha256'] == old_details['semantic_sha256']
    assert new_details['result_sha256'] == old_details['result_sha256']
    assert rebuilt == verified['rule_evidence_binding']
    assert not any(level in {'BLOCKING', 'ERROR'} for level, _, _ in current.issues)
    assert report['state'] == 'prepared'
    assert report['rule_evidence_fingerprint'] == rebuilt['evidence']['evidence_fingerprint']
    assert _load_finalization(output)['state'] == 'prepared'
    assert report['commit_result'] == report['push_result'] == 'not_requested'
    assert _osv_repository_identity(root, pack_path) == identity
    assert _git(root, 'rev-parse', 'HEAD') == head


@pytest.mark.parametrize('severity,waiver', [
    ('BLOCKING', False), ('WARNING', False), ('INFO', False), ('BLOCKING', True),
])
def test_osv_finalize_rejects_new_vulnerability_with_unchanged_git_state(
    tmp_path, monkeypatch, severity, waiver,
):
    root, head, pack_path, output, verified, state = _setup_osv_verified(
        tmp_path, monkeypatch, severity=severity, waiver=waiver,
    )
    identity = _osv_repository_identity(root, pack_path)
    assert identity[2] == (verified['verified_working_tree_state'], verified['verification_fingerprint'])
    scans_before = len(state['scans'])
    _osv_vulnerability(state)
    reason = (
        'current sync_verify Rules do not pass'
        if severity == 'BLOCKING' and not waiver else 'Rule Evidence changed'
    )

    with pytest.raises(SyncFinalizeIntegrityError, match=reason + '.*sync verify again'):
        finalize_sync(root, pack_path)

    assert len(state['scans']) > scans_before
    current, rebuilt = _osv_fresh_binding(verified, state)
    item, = rebuilt['evidence']['results']
    assert item['raw_status'] == 'FAIL'
    assert item['effective_status'] == ('WAIVED' if waiver else 'FAIL')
    assert item['details']['verification_status'] == 'FAIL'
    assert item['details']['exit_code'] == 1
    assert item['details']['findings'][0]['path'] == 'pubspec.lock'
    assert item['exception_id'] == ('EXC-20260927-abcdef12' if waiver else None)
    assert rebuilt != verified['rule_evidence_binding']
    blocking = any(level in {'BLOCKING', 'ERROR'} for level, _, _ in current.issues)
    assert blocking == (severity == 'BLOCKING' and not waiver)
    assert _osv_repository_identity(root, pack_path) == identity
    _assert_osv_blocked(output, head, root)


@pytest.mark.parametrize('failure', ['exception', 'network_exit'])
def test_osv_finalize_network_error_is_not_waivable_or_leaked(tmp_path, monkeypatch, failure):
    # Even a pre-existing governed waiver and non-blocking severity cannot waive ERROR.
    root, head, pack_path, output, verified, state = _setup_osv_verified(
        tmp_path, monkeypatch, severity='WARNING', waiver=True,
    )
    identity = _osv_repository_identity(root, pack_path)
    scans_before = len(state['scans'])
    secret = 'SECRET-NETWORK-TOKEN https://private.example/?token=hidden'
    if failure == 'exception':
        state['error'] = OSError(secret)
    else:
        state.update(exit_code=129, payload=secret, stderr=secret)

    with pytest.raises(SyncFinalizeIntegrityError, match='current sync_verify Rules do not pass') as caught:
        finalize_sync(root, pack_path)

    assert len(state['scans']) > scans_before
    current, rebuilt = _osv_fresh_binding(verified, state)
    item, = rebuilt['evidence']['results']
    assert item['raw_status'] == item['effective_status'] == 'ERROR'
    assert item['exception_id'] is None
    assert rebuilt['evidence']['applied_exception_ids'] == []
    assert any(level == 'ERROR' and location == 'SEC-001' for level, location, _ in current.issues)
    assert rebuilt != verified['rule_evidence_binding']
    exposed = str(caught.value) + json.dumps(rebuilt) + repr(current.issues)
    exposed += ''.join((output / name).read_text(encoding='utf-8')
                       for name in ('finalization.json', 'finalization.md'))
    assert secret not in exposed
    assert 'SECRET-NETWORK-TOKEN' not in exposed
    assert 'private.example' not in exposed
    assert _osv_repository_identity(root, pack_path) == identity
    _assert_osv_blocked(output, head, root)


def test_osv_finalize_binds_accepted_tool_version_even_with_same_semantics(tmp_path, monkeypatch):
    root, head, pack_path, output, verified, state = _setup_osv_verified(tmp_path, monkeypatch)
    identity = _osv_repository_identity(root, pack_path)
    scans_before = len(state['scans'])
    state['tool_version'] = '2.3.4'

    with pytest.raises(SyncFinalizeIntegrityError, match='Rule Evidence changed.*sync verify again'):
        finalize_sync(root, pack_path)

    assert len(state['scans']) > scans_before
    _, rebuilt = _osv_fresh_binding(verified, state)
    old_details = verified['rule_evidence_binding']['evidence']['results'][0]['details']
    item, = rebuilt['evidence']['results']
    assert item['raw_status'] == 'PASS'
    assert item['details']['tool_version'] == '2.3.4'
    assert item['details']['semantic_sha256'] == old_details['semantic_sha256']
    assert item['details']['result_sha256'] != old_details['result_sha256']
    assert rebuilt != verified['rule_evidence_binding']
    assert _osv_repository_identity(root, pack_path) == identity
    _assert_osv_blocked(output, head, root)


@pytest.mark.parametrize('format_drift', [False, True])
def test_dart_format_fresh_sync_recheck_binds_semantics_not_transcript(
    tmp_path, monkeypatch, format_drift,
):
    from project_system import dart_analyze_adapter, dart_format_adapter

    def configure(root, path, object_id):
        source = root / 'lib' / 'main.dart'
        source.parent.mkdir()
        source.write_text('void main() {}\n', encoding='utf-8')
        _write_rule_layer(root, {'QUALITY-001': {
            'title': 'Formatting', 'status': 'active', 'category': 'testing',
            'description': 'Verify project-owned Dart formatting.',
            'verification': {'method': 'deterministic', 'checker': 'code.verification',
                             'parameters': {'adapter': 'dart.format'}},
            'enforcement': {'severity': 'WARNING',
                            'checkpoints': ['project_validate', 'sync_verify']},
            'exception_policy': 'forbidden',
        }})

    dirty, volatile = False, False
    format_calls = []

    def run(argv, **kwargs):
        if argv == ['dart', '--version']:
            return subprocess.CompletedProcess(argv, 0, 'Dart SDK version: 3.13.1', '')
        assert argv[:4] == ['dart', 'format', '--output=none', '--set-exit-if-changed']
        assert kwargs['shell'] is False
        format_calls.append(tuple(argv))
        return subprocess.CompletedProcess(argv, int(dirty),
                                           'different transcript' if volatile else '',
                                           'stderr noise' if volatile else '')

    monkeypatch.setattr(dart_format_adapter, 'run_process', run)
    monkeypatch.setattr(dart_analyze_adapter, 'run_process', run)
    root, _, _, head, _, pack_path, output, _ = _setup_verified(tmp_path, configure=configure)
    verified = json.loads((output / 'verification.json').read_text(encoding='utf-8'))
    before = _osv_repository_identity(root, pack_path)
    scans_before = len(format_calls)
    dirty, volatile = format_drift, True
    if format_drift:
        with pytest.raises(SyncFinalizeIntegrityError, match='Rule Evidence changed'):
            finalize_sync(root, pack_path)
        _assert_osv_blocked(output, head, root)
    else:
        _, report = finalize_sync(root, pack_path)
        assert report['state'] == 'prepared'
        assert report['rule_evidence_fingerprint'] == verified['rule_evidence_binding']['evidence']['evidence_fingerprint']
        assert report['commit_result'] == report['push_result'] == 'not_requested'
    assert len(format_calls) > scans_before
    assert _osv_repository_identity(root, pack_path) == before


@pytest.mark.parametrize('info_drift', [False, True])
def test_dart_analyze_strict_fresh_sync_recheck_binds_semantics_not_transcript(
    tmp_path, monkeypatch, info_drift,
):
    from project_system import dart_analyze_adapter, sync_finalization
    from project_system.verification_adapters import VERIFICATION_ADAPTER_REGISTRY

    assert 'dart.analyze.strict' in VERIFICATION_ADAPTER_REGISTRY

    def configure(root, path, object_id):
        source = root / 'lib' / 'main.dart'
        source.parent.mkdir()
        source.write_text('void main() {}\n', encoding='utf-8')
        _write_rule_layer(root, {'QUALITY-001': {
            'title': 'Strict analysis', 'status': 'active', 'category': 'testing',
            'description': 'Verify all Dart analyzer diagnostics.',
            'verification': {'method': 'deterministic', 'checker': 'code.verification',
                             'parameters': {'adapter': 'dart.analyze.strict'}},
            'enforcement': {'severity': 'WARNING',
                            'checkpoints': ['project_validate', 'sync_verify']},
            'exception_policy': 'forbidden',
        }})

    state = {'info': False, 'volatile': False, 'scans': [], 'rechecks': []}

    def run(argv, **kwargs):
        if argv == ['dart', '--version']:
            return subprocess.CompletedProcess(argv, 0, 'Dart SDK version: 3.13.1', '')
        assert argv == ['dart', 'analyze', '--format=machine', '--no-plugins', '--fatal-infos', '.']
        assert kwargs['shell'] is False
        stdout = '\n\n' if state['volatile'] else ''
        if state['info']:
            machine_path = (kwargs['cwd'] / 'lib' / 'main.dart').as_posix()
            stdout += f'INFO|HINT|LINT|{machine_path}|1|1|1|Consider a better name.\n'
        stderr = 'volatile analyzer stderr' if state['volatile'] else ''
        state['scans'].append((tuple(argv), kwargs['cwd'], stdout, stderr))
        return subprocess.CompletedProcess(argv, int(state['info']), stdout, stderr)

    monkeypatch.setattr(dart_analyze_adapter, 'run_process', run)
    root, _, _, head, _, pack_path, output, _ = _setup_verified(tmp_path, configure=configure)
    verified = json.loads((output / 'verification.json').read_text(encoding='utf-8'))
    before = _osv_repository_identity(root, pack_path)
    assert before[2] == (verified['verified_working_tree_state'], verified['verification_fingerprint'])
    scans_before = len(state['scans'])
    assert scans_before > 0
    assert all(not stdout and not stderr for _, _, stdout, stderr in state['scans'])

    real_validate = sync_finalization.validate_report

    def observe(root, **options):
        report = real_validate(root, **options)
        state['rechecks'].append((options, report))
        return report

    monkeypatch.setattr(sync_finalization, 'validate_report', observe)
    state.update(info=info_drift, volatile=True)
    if info_drift:
        with pytest.raises(SyncFinalizeIntegrityError, match='Rule Evidence changed.*sync verify again'):
            finalize_sync(root, pack_path)
        _assert_osv_blocked(output, head, root)
    else:
        _, report = finalize_sync(root, pack_path)
        assert report['state'] == _load_finalization(output)['state'] == 'prepared'
        assert report['commit_result'] == report['push_result'] == 'not_requested'

    assert len(state['scans']) > scans_before  # Real fresh analyzer execution, not cached evidence.
    assert all(cwd == root.resolve() and stdout and stderr
               for _, cwd, stdout, stderr in state['scans'][scans_before:])
    current, rebuilt = _osv_fresh_binding(verified, state)
    old_item, = verified['rule_evidence_binding']['evidence']['results']
    new_item, = rebuilt['evidence']['results']
    assert old_item['raw_status'] == 'PASS'
    assert old_item['details']['adapter_id'] == 'dart.analyze.strict'
    assert new_item['details']['exit_code'] == int(info_drift)
    assert not any(level in {'BLOCKING', 'ERROR'} for level, _, _ in current.issues)
    if info_drift:
        assert new_item['raw_status'] == new_item['effective_status'] == 'FAIL'
        assert new_item['details']['findings'][0]['severity'] == 'INFO'
        assert new_item['details']['findings'][0]['path'] == 'lib/main.dart'
        assert new_item['details']['semantic_sha256'] != old_item['details']['semantic_sha256']
        assert new_item['details']['result_sha256'] != old_item['details']['result_sha256']
        assert rebuilt != verified['rule_evidence_binding']
    else:
        assert new_item['details']['semantic_sha256'] == old_item['details']['semantic_sha256']
        assert new_item['details']['result_sha256'] == old_item['details']['result_sha256']
        assert rebuilt == verified['rule_evidence_binding']
        assert report['rule_evidence_fingerprint'] == rebuilt['evidence']['evidence_fingerprint']
    # File bytes, source, HEAD, index, diffs and actual verified fingerprint are unchanged.
    assert _osv_repository_identity(root, pack_path) == before


def _stage10c_quality_rules(format_severity='WARNING', strict_severity='BLOCKING', *, reverse=False):
    rules = {}
    for rule_id, adapter, severity in (
        ('QUALITY-001', 'dart.format', format_severity),
        ('QUALITY-002', 'dart.analyze.strict', strict_severity),
    ):
        rules[rule_id] = {
            'title': adapter, 'status': 'active', 'category': 'testing',
            'description': 'Verify the independently governed Dart quality capability.',
            'verification': {'method': 'deterministic', 'checker': 'code.verification',
                             'parameters': {'adapter': adapter}},
            'enforcement': {'severity': severity,
                            'checkpoints': ['project_validate', 'sync_verify']},
            'exception_policy': 'forbidden',
        }
    return dict(reversed(tuple(rules.items()))) if reverse else rules


def _stage10c_source(root):
    source = root / 'lib' / 'main.dart'
    source.parent.mkdir()
    source.write_text('void main() {}\n', encoding='utf-8')


def _stage10c_transport(monkeypatch):
    from project_system import dart_analyze_adapter, dart_format_adapter

    state = {'format_dirty': False, 'strict_info': False, 'volatile': False,
             'error_adapter': None, 'calls': {'dart.format': [], 'dart.analyze.strict': []},
             'rechecks': []}

    def run(argv, **kwargs):
        if argv == ['dart', '--version']:
            return subprocess.CompletedProcess(argv, 0, 'Dart SDK version: 3.13.1', '')
        root = kwargs['cwd']
        assert kwargs['shell'] is False
        if argv[:4] == ['dart', 'format', '--output=none', '--set-exit-if-changed']:
            assert len(argv) == 5 and Path(argv[4]) == root / 'lib' / 'main.dart'
            adapter_id = 'dart.format'
            stdout = 'volatile formatter transcript' if state['volatile'] else ''
            exit_code = int(state['format_dirty'])
        else:
            assert argv == ['dart', 'analyze', '--format=machine', '--no-plugins', '--fatal-infos', '.']
            adapter_id = 'dart.analyze.strict'
            stdout = '\n\n' if state['volatile'] else ''
            if state['strict_info']:
                machine_path = (root / 'lib' / 'main.dart').as_posix()
                stdout += f'INFO|HINT|LINT|{machine_path}|1|1|1|Consider a better name.\n'
            exit_code = int(state['strict_info'])
        stderr = f'volatile {adapter_id} stderr' if state['volatile'] else ''
        state['calls'][adapter_id].append((tuple(argv), root, stdout, stderr))
        if state['error_adapter'] == adapter_id:
            raise OSError('SECRET-TRANSPORT-TOKEN C:\\private\\tool https://private.example/?token=hidden')
        return subprocess.CompletedProcess(argv, exit_code, stdout, stderr)

    monkeypatch.setattr(dart_format_adapter, 'run_process', run)
    monkeypatch.setattr(dart_analyze_adapter, 'run_process', run)
    return state


def _stage10c_evidence(root, rules, *, checkpoint='sync_verify', results=None):
    from project_system.rule_engine import RuleEvaluationContext, evaluate_rules
    from project_system.rule_evidence import build_rule_evidence, rule_evidence_to_dict

    registry = {'schema_version': 1, 'profile': 'project-system-rules-v1', 'rules': rules}
    context = RuleEvaluationContext(root, checkpoint, {}, True, evaluation_paths=('README.md',))
    results = evaluate_rules(registry, context) if results is None else results
    return rule_evidence_to_dict(build_rule_evidence(
        project_id='demo', git_head='1' * 40, base_commit='1' * 40, cli_version='test',
        rules_registry=registry, context=context, results=results,
        exception_registry={'schema_version': 1, 'profile': 'project-system-rule-exceptions-v1', 'exceptions': {}},
    ))


def _stage10c_assert_combined_evidence(payload):
    assert payload['schema_version'] == 1
    assert [item['rule_id'] for item in payload['results']] == ['QUALITY-001', 'QUALITY-002']
    for item, adapter_id in zip(payload['results'], ('dart.format', 'dart.analyze.strict')):
        assert item['checker'] == 'code.verification'
        assert item['details']['adapter_id'] == adapter_id
        assert item['details']['adapter_version'] == '1'
        assert 'stdout_sha256' not in item['details'] and 'stderr_sha256' not in item['details']
        if item['raw_status'] != 'ERROR':
            assert len(item['details']['semantic_sha256']) == 64
            assert len(item['details']['result_sha256']) == 64


@pytest.mark.parametrize('checkpoint', ['project_validate', 'sync_verify'])
def test_stage10c_combined_pass_and_registry_order_independence(tmp_path, monkeypatch, checkpoint):
    _stage10c_source(tmp_path)
    state = _stage10c_transport(monkeypatch)
    normal = _stage10c_quality_rules()
    reversed_rules = _stage10c_quality_rules(reverse=True)
    assert tuple(normal) == ('QUALITY-001', 'QUALITY-002')
    assert tuple(reversed_rules) == ('QUALITY-002', 'QUALITY-001')
    first = _stage10c_evidence(tmp_path, normal, checkpoint=checkpoint)
    second = _stage10c_evidence(tmp_path, reversed_rules, checkpoint=checkpoint)
    _stage10c_assert_combined_evidence(first)
    assert first == second  # Includes canonical registry hash, result order and Evidence fingerprint.
    assert all(item['raw_status'] == item['effective_status'] == 'PASS' for item in first['results'])
    assert all(len(calls) == 2 for calls in state['calls'].values())
    assert first['results'][0]['details']['result_sha256'] != first['results'][1]['details']['result_sha256']


@pytest.mark.parametrize('recipient,field', [
    (0, 'result_sha256'), (1, 'result_sha256'), (0, 'details'), (1, 'details'),
])
def test_stage10c_cross_rule_result_substitution_is_rejected(tmp_path, monkeypatch, recipient, field):
    from dataclasses import replace
    from project_system.rule_engine import RuleEvaluationContext, evaluate_rules
    from project_system.rule_evidence import RuleEvidenceError

    _stage10c_source(tmp_path)
    state = _stage10c_transport(monkeypatch)
    rules = _stage10c_quality_rules()
    results = list(evaluate_rules(
        {'schema_version': 1, 'profile': 'project-system-rules-v1', 'rules': rules},
        RuleEvaluationContext(tmp_path, 'sync_verify', {}, True),
    ))
    assert all(len(calls) == 1 for calls in state['calls'].values())
    _stage10c_assert_combined_evidence(_stage10c_evidence(tmp_path, rules, results=results))
    donor = results[1 - recipient].details
    details = dict(donor) if field == 'details' else {**results[recipient].details, field: donor[field]}
    results[recipient] = replace(results[recipient], details=details)
    with pytest.raises(RuleEvidenceError):
        _stage10c_evidence(tmp_path, rules, results=results)


@pytest.mark.parametrize('format_dirty,strict_info,format_severity,strict_severity,blocking_ids', [
    (False, False, 'WARNING', 'BLOCKING', ()),
    (True, True, 'WARNING', 'BLOCKING', ('QUALITY-002',)),
    (True, True, 'BLOCKING', 'WARNING', ('QUALITY-001',)),
    (True, False, 'WARNING', 'BLOCKING', ()),
    (True, False, 'BLOCKING', 'WARNING', ('QUALITY-001',)),
    (False, True, 'BLOCKING', 'WARNING', ()),
    (False, True, 'WARNING', 'BLOCKING', ('QUALITY-002',)),
])
def test_stage10c_project_validate_no_short_circuit_and_per_rule_enforcement(
    tmp_path, monkeypatch, format_dirty, strict_info, format_severity, strict_severity, blocking_ids,
):
    from project_system.rule_evidence import rule_evidence_to_dict

    state = _stage10c_transport(monkeypatch)

    def configure(root, path, object_id):
        _stage10c_source(root)
        _write_rule_layer(root, _stage10c_quality_rules(format_severity, strict_severity))

    root, _, _, _, _, _, _, _ = _setup_plan(tmp_path, configure=configure)
    before = {adapter: len(calls) for adapter, calls in state['calls'].items()}
    state.update(format_dirty=format_dirty, strict_info=strict_info)
    report = validation_module.validate_report(root)  # Real public project_validate path.
    payload = rule_evidence_to_dict(report.rule_evidence)
    _stage10c_assert_combined_evidence(payload)
    assert payload['checkpoint'] == 'project_validate'
    for item, dirty, severity in zip(payload['results'], (format_dirty, strict_info),
                                     (format_severity, strict_severity)):
        assert item['raw_status'] == item['effective_status'] == ('FAIL' if dirty else 'PASS')
        assert item['severity'] == severity
        assert item['details']['verification_status'] == ('FAIL' if dirty else 'PASS')
        if dirty:
            finding, = item['details']['findings']
            assert finding['path'] == 'lib/main.dart'
            assert finding['code'] == ('dart.format.required' if item['rule_id'] == 'QUALITY-001' else 'LINT')
            assert finding['severity'] == ('ERROR' if item['rule_id'] == 'QUALITY-001' else 'INFO')
    assert all(len(calls) == before[adapter] + 1 for adapter, calls in state['calls'].items())
    blocking = [location for level, location, _ in report.issues if level in {'BLOCKING', 'ERROR'}]
    assert tuple(blocking) == blocking_ids
    rule_issues = {location: level for level, location, _ in report.issues if location.startswith('QUALITY-')}
    assert rule_issues == {rule_id: severity for rule_id, dirty, severity in (
        ('QUALITY-001', format_dirty, format_severity), ('QUALITY-002', strict_info, strict_severity),
    ) if dirty}


def _stage10c_verified_project(tmp_path, monkeypatch):
    from project_system import sync_finalization

    state = _stage10c_transport(monkeypatch)

    def configure(root, path, object_id):
        _stage10c_source(root)
        _write_rule_layer(root, _stage10c_quality_rules('WARNING', 'INFO'))

    root, _, _, head, _, pack_path, output, _ = _setup_verified(tmp_path, configure=configure)
    verified = json.loads((output / 'verification.json').read_text(encoding='utf-8'))
    _stage10c_assert_combined_evidence(verified['rule_evidence_binding']['evidence'])
    assert verified['verification_result'] == 'passed'
    assert verified['errors'] == []
    assert verified['validation']['generation_ran'] is True
    assert verified['rule_evidence_binding']['evidence']['checkpoint'] == 'sync_verify'
    assert all(item['raw_status'] == 'PASS' for item in verified['rule_evidence_binding']['evidence']['results'])
    assert all(calls for calls in state['calls'].values())
    real_validate = sync_finalization.validate_report

    def observe(root, **options):
        current = real_validate(root, **options)
        state['rechecks'].append((options, current))
        return current

    monkeypatch.setattr(sync_finalization, 'validate_report', observe)
    return root, head, pack_path, output, verified, state


@pytest.mark.parametrize('drift', ['none', 'format', 'strict', 'both'])
def test_stage10c_combined_fresh_sync_volatility_and_isolated_semantic_drift(tmp_path, monkeypatch, drift):
    root, head, pack_path, output, verified, state = _stage10c_verified_project(tmp_path, monkeypatch)
    identity = _osv_repository_identity(root, pack_path)
    assert identity[2] == (verified['verified_working_tree_state'], verified['verification_fingerprint'])
    before = {adapter: len(calls) for adapter, calls in state['calls'].items()}
    state.update(volatile=True, format_dirty=drift in {'format', 'both'}, strict_info=drift in {'strict', 'both'})
    if drift == 'none':
        _, report = finalize_sync(root, pack_path)
        assert report['state'] == _load_finalization(output)['state'] == 'prepared'
        assert report['commit_requested'] is report['push_requested'] is False
        assert report['commit_result'] == report['push_result'] == 'not_requested'
        assert report['commit_sha'] is None
    else:
        with pytest.raises(SyncFinalizeIntegrityError, match='Rule Evidence changed.*sync verify again'):
            finalize_sync(root, pack_path)
        _assert_osv_blocked(output, head, root)
    for adapter, calls in state['calls'].items():
        assert len(calls) > before[adapter]
        assert all(cwd == root.resolve() and stdout and stderr
                   for _, cwd, stdout, stderr in calls[before[adapter]:])
    current, rebuilt = _osv_fresh_binding(verified, state)
    _stage10c_assert_combined_evidence(rebuilt['evidence'])
    assert not any(level in {'BLOCKING', 'ERROR'} for level, _, _ in current.issues)
    for old, new, changed in zip(verified['rule_evidence_binding']['evidence']['results'],
                                rebuilt['evidence']['results'], (state['format_dirty'], state['strict_info'])):
        assert new['raw_status'] == ('FAIL' if changed else 'PASS')
        assert (new['details']['semantic_sha256'] != old['details']['semantic_sha256']) == changed
        assert (new['details']['result_sha256'] != old['details']['result_sha256']) == changed
        if not changed:
            assert new == old
    if drift == 'none':
        assert rebuilt == verified['rule_evidence_binding']
        assert report['rule_evidence_fingerprint'] == rebuilt['evidence']['evidence_fingerprint']
    else:
        assert rebuilt != verified['rule_evidence_binding']
    assert _osv_repository_identity(root, pack_path) == identity


@pytest.mark.parametrize('error_adapter', ['dart.format', 'dart.analyze.strict'])
@pytest.mark.parametrize('other_fails', [False, True])
def test_stage10c_combined_infrastructure_error_isolated_fail_closed_and_sanitized(
    tmp_path, monkeypatch, capsys, error_adapter, other_fails,
):
    root, head, pack_path, output, verified, state = _stage10c_verified_project(tmp_path, monkeypatch)
    identity = _osv_repository_identity(root, pack_path)
    before = {adapter: len(calls) for adapter, calls in state['calls'].items()}
    state.update(error_adapter=error_adapter,
                 format_dirty=other_fails and error_adapter != 'dart.format',
                 strict_info=other_fails and error_adapter != 'dart.analyze.strict')
    with pytest.raises(SyncFinalizeIntegrityError, match='current sync_verify Rules do not pass') as caught:
        finalize_sync(root, pack_path)
    assert all(len(calls) > before[adapter] for adapter, calls in state['calls'].items())
    current, rebuilt = _osv_fresh_binding(verified, state)
    _stage10c_assert_combined_evidence(rebuilt['evidence'])
    for item in rebuilt['evidence']['results']:
        is_error = item['details']['adapter_id'] == error_adapter
        assert item['raw_status'] == item['effective_status'] == (
            'ERROR' if is_error else 'FAIL' if other_fails else 'PASS'
        )
        assert item['exception_id'] is None
        if is_error:
            assert item['details'] == {'adapter_id': error_adapter, 'adapter_version': '1'}
    assert rebuilt['evidence']['applied_exception_ids'] == []
    error_rule = 'QUALITY-001' if error_adapter == 'dart.format' else 'QUALITY-002'
    assert any(level == 'ERROR' and location == error_rule for level, location, _ in current.issues)
    assert rebuilt != verified['rule_evidence_binding']
    captured = capsys.readouterr()
    exposed = str(caught.value) + json.dumps(rebuilt) + repr(current.issues) + captured.out + captured.err
    exposed += ''.join((output / name).read_text(encoding='utf-8')
                       for name in ('finalization.json', 'finalization.md'))
    for secret in ('SECRET-TRANSPORT-TOKEN', 'C:\\private\\tool', 'private.example', 'token=hidden'):
        assert secret not in exposed
    assert _osv_repository_identity(root, pack_path) == identity
    _assert_osv_blocked(output, head, root)
