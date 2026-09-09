from copy import deepcopy
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
import shutil
import subprocess

import pytest
import yaml

from project_system.init_project import init_project
from project_system.objects import create_object
from project_system import sync_bindings, sync_finalization, sync_pull
from project_system.sync_bindings import (
    SyncBindingError, durable_roots, git_admin_dir, load_sync_bindings,
)
from project_system.sync_finalization import (
    SyncFinalizeIntegrityError,
    SyncFinalizeScopeError,
    SyncPushError,
    finalize_sync,
)
from project_system.sync_intake import intake_sync
from project_system.sync_migration import migrate_bindings
from project_system.sync_pull import SyncPullError, pull_sync
from project_system.sync_verification import verify_sync


REPO = 'terminal-owner/terminal-project'


def git(root, *args, check=True):
    result = subprocess.run(
        ['git', *args], cwd=root, capture_output=True, text=True, check=False,
    )
    if check and result.returncode:
        raise AssertionError(result.stderr or result.stdout)
    return result.stdout.strip()


def setup_project(tmp_path, monkeypatch, *, no_change=False, with_remote=False):
    root = init_project('Terminal project', tmp_path / 'project')
    obj, object_id = create_object(root, 'feature', 'Terminal fixture', 'general', 'owner')
    config_path = root / 'project.yaml'
    config = yaml.safe_load(config_path.read_text(encoding='utf-8'))
    config['external_systems']['github'].update(enabled=True, sync_pull={
        'allowed_authors': ['owner'], 'expected_repository': REPO,
    })
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding='utf-8')
    git(root, 'init', '-q')
    git(root, 'config', 'user.name', 'Terminal Tests')
    git(root, 'config', 'user.email', 'terminal@example.invalid')
    git(root, 'remote', 'add', 'origin', f'git@github.com:{REPO}.git')
    git(root, 'add', '.')
    git(root, 'commit', '-qm', 'baseline')
    remote = None
    if with_remote:
        remote = tmp_path / 'remote.git'
        subprocess.run(['git', 'init', '--bare', '-q', str(remote)], check=True)
        git(root, 'remote', 'set-url', '--push', 'origin', str(remote))
        git(root, 'push', '-u', 'origin', 'HEAD')

    if no_change:
        changes = [{
            'change_id': 'unresolved-item', 'kind': 'unresolved',
            'summary': 'Human review is required.',
            'proposal': 'Keep this proposal non-canonical.',
        }]
        expected = []
    else:
        changes = [{
            'change_id': 'update-object', 'kind': 'update_object',
            'target_id': object_id, 'summary': 'Approved fixture update.',
            'patch': {'body': 'Approved fixture body.'},
        }]
        expected = [object_id]
    request = {
        'schema_version': 1,
        'request_id': 'REQUEST-terminal-fixture',
        'source': {'type': 'external_discussion', 'ref': 'terminal-test'},
        'approval': {'approved_by': 'owner', 'approved_at': '2026-09-09T10:00:00Z'},
        'change_class': 'C', 'changes': changes,
        'expected_targets': expected, 'notes': 'Terminal lifecycle fixture.',
    }
    raw = yaml.safe_dump(request, sort_keys=False).encode('utf-8')
    title = '[SYNC REQUEST][TEST] terminal lifecycle'
    transport = {
        'kind': 'github_issue', 'repository': REPO, 'issue_number': 10,
        'issue_url': f'https://github.com/{REPO}/issues/10',
        'issue_author': 'owner', 'issue_updated_at': '2026-09-09T10:00:00Z',
        'body_sha256': sha256(raw).hexdigest(),
        'request_sha256': sha256(raw).hexdigest(),
        'title_sha256': sha256(title.encode()).hexdigest(),
    }
    path, _ = intake_sync(root, '-', stdin=BytesIO(raw), plan=True, transport=transport)
    monkeypatch.setattr(sync_finalization, '_transport_snapshot_unchanged', lambda root, pack: True)
    return root, obj, path, request, raw, transport, remote


def apply_and_verify(root, obj, path):
    obj.write_text(obj.read_text(encoding='utf-8') + '\nApproved terminal edit.\n', encoding='utf-8')
    verify_sync(root, path)


def test_github_commit_only_is_awaiting_push_and_keeps_active_pack(tmp_path, monkeypatch):
    root, obj, path, *_ = setup_project(tmp_path, monkeypatch)
    apply_and_verify(root, obj, path)
    _, report = finalize_sync(root, path, commit=True)
    assert report['state'] == 'committed'
    assert report['transport_state'] == 'awaiting_push'
    assert path.is_file()
    assert not durable_roots(root)[0].exists()


def test_github_commit_push_archives_exact_pack_and_cleans_baseline(tmp_path, monkeypatch):
    root, obj, path, _, _, _, remote = setup_project(tmp_path, monkeypatch, with_remote=True)
    original = path.read_bytes()
    apply_and_verify(root, obj, path)
    _, report = finalize_sync(root, path, commit=True, push=True)
    assert report['state'] == 'completed' and report['terminal_outcome'] == 'pushed'
    assert not path.exists()
    bindings = load_sync_bindings(root)
    assert len(bindings) == 1 and bindings[0].state == 'completed'
    assert bindings[0].raw == original
    assert git(root, 'status', '--short', '--untracked-files=all') == ''
    assert git(remote, 'rev-parse', 'HEAD') == report['commit_sha']


def test_later_push_archives_and_repeated_finalize_is_idempotent(tmp_path, monkeypatch):
    root, obj, path, _, _, _, _ = setup_project(tmp_path, monkeypatch, with_remote=True)
    apply_and_verify(root, obj, path)
    _, committed = finalize_sync(root, path, commit=True)
    assert path.exists() and committed['transport_state'] == 'awaiting_push'
    _, pushed = finalize_sync(root, path, push=True)
    assert pushed['state'] == 'completed' and not path.exists()
    _, repeated = finalize_sync(root, pushed['pack_id'], push=True)
    assert repeated['push_result'] == 'already_synchronized'


def test_push_failure_preserves_active_pack_then_retry_terminalizes(tmp_path, monkeypatch):
    root, obj, path, _, _, _, _ = setup_project(tmp_path, monkeypatch, with_remote=True)
    apply_and_verify(root, obj, path)
    finalize_sync(root, path, commit=True)
    real_push = sync_finalization._push
    monkeypatch.setattr(sync_finalization, '_push', lambda *args: (_ for _ in ()).throw(SyncPushError('offline')))
    with pytest.raises(SyncPushError, match='offline'):
        finalize_sync(root, path, push=True)
    assert path.exists()
    monkeypatch.setattr(sync_finalization, '_push', real_push)
    _, report = finalize_sync(root, path, push=True)
    assert report['state'] == 'completed' and not path.exists()


def test_reviewed_no_change_requires_verify_reason_and_terminalizes(tmp_path, monkeypatch):
    root, _, path, *_ = setup_project(tmp_path, monkeypatch, no_change=True)
    verify_sync(root, path)
    finalize_sync(root, path)
    assert path.exists()  # prepared is not terminal approval
    with pytest.raises(SyncFinalizeIntegrityError, match='requires --outcome and --reason'):
        finalize_sync(root, path, complete=True)
    original = path.read_bytes()
    _, report = finalize_sync(
        root, path, complete=True, outcome='reviewed-no-change', reason='Reviewed by owner.',
    )
    assert report['state'] == 'completed' and not path.exists()
    assert load_sync_bindings(root)[0].raw == original


@pytest.mark.parametrize('outcome', ['rejected', 'abandoned'])
def test_rejected_and_abandoned_need_clean_baseline(tmp_path, monkeypatch, outcome):
    root, obj, path, *_ = setup_project(tmp_path, monkeypatch)
    obj.write_text(obj.read_text(encoding='utf-8') + '\nUnfinished edit.\n', encoding='utf-8')
    with pytest.raises(SyncFinalizeScopeError, match='clean canonical baseline'):
        finalize_sync(root, path, complete=True, outcome=outcome, reason='Explicit owner disposition.')
    git(root, 'checkout', '--', obj.relative_to(root).as_posix())
    _, report = finalize_sync(
        root, path, complete=True, outcome=outcome, reason='Explicit owner disposition.',
    )
    assert report['terminal_outcome'] == outcome and not path.exists()


def test_crash_after_publish_is_recoverable_but_mismatch_blocks(tmp_path, monkeypatch):
    root, _, path, *_ = setup_project(tmp_path, monkeypatch, no_change=True)
    verify_sync(root, path)

    def crash(stage, **kwargs):
        if stage == 'after_publish':
            raise OSError('simulated crash')

    monkeypatch.setattr(sync_bindings, '_terminalize_hook', crash)
    with pytest.raises(SyncFinalizeIntegrityError, match='simulated crash'):
        finalize_sync(root, path, complete=True, outcome='reviewed-no-change', reason='Reviewed.')
    assert path.exists()
    assert durable_roots(root)[0].joinpath(path.stem).is_dir()
    monkeypatch.setattr(sync_bindings, '_terminalize_hook', lambda *args, **kwargs: None)
    _, report = finalize_sync(root, path.stem, complete=True, outcome='reviewed-no-change', reason='Reviewed.')
    assert report['state'] == 'completed' and not path.exists()


def test_crash_before_publish_reuses_sealed_transaction(tmp_path, monkeypatch):
    root, _, path, *_ = setup_project(tmp_path, monkeypatch, no_change=True)
    verify_sync(root, path)

    def crash(stage, **kwargs):
        if stage == 'before_publish':
            raise OSError('before publish')

    monkeypatch.setattr(sync_bindings, '_terminalize_hook', crash)
    with pytest.raises(SyncFinalizeIntegrityError, match='before publish'):
        finalize_sync(root, path, complete=True, outcome='reviewed-no-change', reason='Reviewed.')
    assert path.exists()
    monkeypatch.setattr(sync_bindings, '_terminalize_hook', lambda *args, **kwargs: None)
    _, report = finalize_sync(root, path, complete=True, outcome='reviewed-no-change', reason='Reviewed.')
    assert report['state'] == 'completed' and not path.exists()


def test_active_archive_mismatch_is_blocking(tmp_path, monkeypatch):
    root, _, path, *_ = setup_project(tmp_path, monkeypatch, no_change=True)
    verify_sync(root, path)
    finalize_sync(root, path, complete=True, outcome='reviewed-no-change', reason='Reviewed.')
    archived = load_sync_bindings(root)[0]
    path.write_bytes(archived.raw + b'\n')
    with pytest.raises(SyncBindingError, match='active/completed integrity conflict'):
        load_sync_bindings(root)


def test_transaction_tampering_blocks_retry(tmp_path, monkeypatch):
    root, _, path, *_ = setup_project(tmp_path, monkeypatch, no_change=True)
    verify_sync(root, path)
    monkeypatch.setattr(
        sync_bindings,
        '_terminalize_hook',
        lambda stage, **kwargs: (_ for _ in ()).throw(OSError('stop')) if stage == 'before_publish' else None,
    )
    with pytest.raises(SyncFinalizeIntegrityError):
        finalize_sync(root, path, complete=True, outcome='reviewed-no-change', reason='Reviewed.')
    _, transactions = durable_roots(root)
    transaction = transactions / f'{path.stem}.json'
    value = json.loads(transaction.read_text(encoding='utf-8'))
    value['pack_sha256'] = '0' * 64
    transaction.write_text(json.dumps(value), encoding='utf-8')
    with pytest.raises(SyncBindingError, match='integrity hash mismatch'):
        load_sync_bindings(root)


def test_completed_binding_survives_generated_deletion_and_pull_reuses(tmp_path, monkeypatch):
    root, _, path, request, raw, transport, _ = setup_project(tmp_path, monkeypatch, no_change=True)
    verify_sync(root, path)
    finalize_sync(root, path, complete=True, outcome='reviewed-no-change', reason='Reviewed.')
    shutil.rmtree(root / '.generated')
    issue = {
        'number': 10, 'html_url': transport['issue_url'], 'state': 'open',
        'title': '[SYNC REQUEST][TEST] terminal lifecycle', 'user': {'login': 'owner'},
        'updated_at': transport['issue_updated_at'], 'body': raw.decode('utf-8'),
    }

    def gh_get(root, endpoint, fields=()):
        if endpoint == 'search/issues':
            return {'total_count': 1, 'incomplete_results': False, 'items': [deepcopy(issue)]}
        return deepcopy(issue)

    monkeypatch.setattr(sync_pull, 'gh_get', gh_get)
    report = pull_sync(root, issue_number=10)
    assert report['status'] == 'completed'
    assert report['pack_id'] == path.stem
    assert not path.exists()
    issue['updated_at'] = '2026-09-09T11:00:00Z'
    with pytest.raises(SyncPullError, match='drift/conflict'):
        pull_sync(root, issue_number=10)


def test_completed_binding_and_transaction_tampering_are_detected(tmp_path, monkeypatch):
    root, _, path, *_ = setup_project(tmp_path, monkeypatch, no_change=True)
    verify_sync(root, path)
    finalize_sync(root, path, complete=True, outcome='reviewed-no-change', reason='Reviewed.')
    completed, transactions = durable_roots(root)
    binding_path = completed / path.stem / 'binding.json'
    binding = json.loads(binding_path.read_text(encoding='utf-8'))
    binding['terminal']['reason'] = 'tampered'
    binding_path.write_text(json.dumps(binding), encoding='utf-8')
    with pytest.raises(SyncBindingError, match='integrity hash mismatch'):
        load_sync_bindings(root)


def test_migration_audit_does_not_guess_active_or_prepared(tmp_path, monkeypatch):
    root, _, path, *_ = setup_project(tmp_path, monkeypatch, no_change=True)
    audit = migrate_bindings(root)
    assert audit == [{'pack_id': path.stem, 'status': 'planned_unverified'}]
    verify_sync(root, path)
    finalize_sync(root, path)
    audit = migrate_bindings(root)
    assert audit[0]['status'] == 'prepared_not_terminal'
    assert path.exists()


def test_migration_apply_archives_only_proven_legacy_push_and_is_idempotent(tmp_path, monkeypatch):
    root, obj, path, _, _, _, _ = setup_project(tmp_path, monkeypatch, with_remote=True)
    apply_and_verify(root, obj, path)
    _, committed = finalize_sync(root, path, commit=True)
    git(root, 'push')
    output = root / '.generated' / 'sync' / path.stem
    report_path = output / 'finalization.json'
    report = json.loads(report_path.read_text(encoding='utf-8'))
    report.update(state='pushed', push_requested=True, push_result='pushed')
    sync_finalization._seal_finalization_report(report)
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    assert migrate_bindings(root) == [{'pack_id': path.stem, 'status': 'ready_to_archive'}]
    assert migrate_bindings(root, apply=True) == [{'pack_id': path.stem, 'status': 'archived'}]
    assert not path.exists()
    assert migrate_bindings(root, apply=True) == [{'pack_id': path.stem, 'status': 'already_completed'}]


def test_git_admin_store_uses_git_discovery_not_root_assumption(tmp_path, monkeypatch):
    root, _, _, *_ = setup_project(tmp_path, monkeypatch, no_change=True)
    expected = Path(git(root, 'rev-parse', '--absolute-git-dir')).resolve()
    assert git_admin_dir(root) == expected
    completed, transactions = durable_roots(root, create=True)
    assert completed.parent.parent == expected / 'project-system'
    assert transactions.parent == completed.parent
