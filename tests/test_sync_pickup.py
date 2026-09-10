from copy import deepcopy
import json
import subprocess

import pytest
import yaml

from project_system.cli import main
from project_system.init_project import init_project
from project_system.objects import create_object
from project_system import sync_bindings, sync_pickup, sync_pull
from project_system.sync_finalization import finalize_sync
from project_system.sync_intake import _intake_lock
from project_system.sync_bindings import durable_roots
from project_system.sync_pickup import pickup_once
from project_system.sync_verification import verify_sync


REPO = 'pickup-owner/pickup-project'


def git(root, *args, check=True):
    result = subprocess.run(
        ['git', *args], cwd=root, capture_output=True, text=True, check=False,
    )
    if check and result.returncode:
        raise AssertionError(result.stderr or result.stdout)
    return result.stdout.strip()


def canonical(root):
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob('*')
        if path.is_file() and path.relative_to(root).parts[0] not in {'.git', '.generated', 'inbox'}
    }


def request_for(object_id, number):
    return {
        'schema_version': 1,
        'request_id': f'REQUEST-pickup-{number}',
        'source': {'type': 'external_discussion', 'ref': f'pickup-{number}'},
        'approval': {
            'approved_by': 'owner',
            'approved_at': '2026-09-09T10:00:00Z',
        },
        'change_class': 'C',
        'changes': [{
            'change_id': f'update-{number}',
            'kind': 'update_object',
            'target_id': object_id,
            'summary': f'Approved fixture update {number}.',
            'patch': {'body': f'Fixture text {number}.'},
        }],
        'expected_targets': [object_id],
        'notes': 'Bounded pickup fixture.',
    }


def issue_for(object_id, number, *, author='owner', marker=True, request=None):
    request = request or request_for(object_id, number)
    return {
        'number': number,
        'html_url': f'https://github.com/{REPO}/issues/{number}',
        'state': 'open',
        'title': ('[SYNC REQUEST][TEST] ' if marker else 'Ordinary issue ') + str(number),
        'user': {'login': author},
        'updated_at': f'2026-09-09T10:{number:02d}:00Z',
        'body': yaml.safe_dump(request, sort_keys=False),
    }


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    root = init_project('Pickup project', tmp_path / 'project')
    obj, object_id = create_object(root, 'feature', 'Pickup fixture', 'general', 'owner')
    config_path = root / 'project.yaml'
    config = yaml.safe_load(config_path.read_text(encoding='utf-8'))
    config['external_systems']['github'].update(enabled=True, sync_pull={
        'allowed_authors': ['owner'], 'expected_repository': REPO,
    })
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding='utf-8')
    git(root, 'init', '-q')
    git(root, 'config', 'user.name', 'Pickup Tests')
    git(root, 'config', 'user.email', 'pickup@example.invalid')
    git(root, 'remote', 'add', 'origin', f'git@github.com:{REPO}.git')
    git(root, 'add', '.')
    git(root, 'commit', '-qm', 'baseline')
    state = {'issues': {}, 'calls': []}

    def gh_get(root, endpoint, fields=()):
        state['calls'].append((endpoint, fields))
        if endpoint == 'search/issues':
            items = [
                deepcopy(issue) for issue in state['issues'].values()
                if issue['state'] == 'open'
            ]
            return {
                'total_count': len(items),
                'incomplete_results': False,
                'items': items,
            }
        number = int(endpoint.rsplit('/', 1)[1])
        return deepcopy(state['issues'][number])

    monkeypatch.setattr(sync_pull, 'gh_get', gh_get)
    return root, obj, object_id, state


def add_issue(fixture, number, **kwargs):
    root, obj, object_id, state = fixture
    issue = issue_for(object_id, number, **kwargs)
    state['issues'][number] = issue
    return issue


def complete_rejected(root, state, pack_id):
    _, report = finalize_sync(
        root, pack_id, complete=True, outcome='rejected', reason='Owner rejected fixture.',
    )
    assert report['state'] == 'completed'


def test_no_pending_is_structured_and_writes_nothing(fixture):
    root, *_ = fixture
    before = canonical(root)
    result = pickup_once(root)
    assert result == {
        'schema_version': 1,
        'repository': REPO,
        'status': 'no_pending',
        'reason': 'no authorized marked open request is pending',
        'processed_count': 0,
        'pending_count': 0,
    }
    assert canonical(root) == before
    assert not list((root / 'inbox/sync').glob('*.yaml'))


def test_one_pending_creates_and_plans_without_canonical_or_git_mutation(fixture, monkeypatch):
    root, obj, _, state = fixture
    add_issue(fixture, 1)
    before = canonical(root)
    head = git(root, 'rev-parse', 'HEAD')
    index = (root / '.git/index').read_bytes()
    commands = []
    original_run = subprocess.run

    def record(args, **kwargs):
        commands.append(args)
        return original_run(args, **kwargs)

    monkeypatch.setattr(subprocess, 'run', record)
    result = pickup_once(root)
    assert result['status'] == 'created'
    assert result['issue_number'] == 1 and result['plan'] == 'ready'
    assert (root / result['pack_path']).is_file()
    output = root / '.generated/sync' / result['pack_id']
    manifest = json.loads((output / 'manifest.json').read_text(encoding='utf-8'))
    assert manifest['allowed_write_set'] == [obj.relative_to(root).as_posix()]
    assert canonical(root) == before
    assert git(root, 'rev-parse', 'HEAD') == head
    assert (root / '.git/index').read_bytes() == index
    assert all(command[1] not in {'add', 'commit', 'push'} for command in commands if command[0] == 'git')
    assert all(endpoint == 'search/issues' or endpoint.startswith(f'repos/{REPO}/issues/')
               for endpoint, _ in state['calls'])


def test_two_pending_selects_oldest_and_only_one_pack(fixture):
    add_issue(fixture, 8)
    add_issue(fixture, 3)
    result = pickup_once(fixture[0])
    assert result['status'] == 'created' and result['issue_number'] == 3
    assert result['pending_count'] == 2
    assert len(list((fixture[0] / 'inbox/sync').glob('*.yaml'))) == 1


def test_completed_older_issue_is_processed_skip_and_next_is_selected(fixture):
    root, *_ = fixture
    add_issue(fixture, 1)
    first = pickup_once(root)
    complete_rejected(root, fixture[3], first['pack_id'])
    add_issue(fixture, 2)
    second = pickup_once(root)
    assert second['status'] == 'created' and second['issue_number'] == 2
    assert second['processed_count'] == 1
    assert len(list((root / 'inbox/sync').glob('*.yaml'))) == 1


def test_existing_completed_same_issue_returns_no_pending(fixture):
    root, *_ = fixture
    add_issue(fixture, 1)
    first = pickup_once(root)
    complete_rejected(root, fixture[3], first['pack_id'])
    result = pickup_once(root)
    assert result['status'] == 'no_pending' and result['processed_count'] == 1
    assert not list((root / 'inbox/sync').glob('*.yaml'))


@pytest.mark.parametrize('mutation', [
    'closed',
    'updated_at',
    'state_reason_closed_at',
    'reopened',
])
def test_completed_lifecycle_metadata_is_provenance_only(fixture, mutation):
    root, *_ = fixture
    issue = add_issue(fixture, 1)
    first = pickup_once(root)
    complete_rejected(root, fixture[3], first['pack_id'])
    completed = durable_roots(root)[0] / first['pack_id'] / 'binding.json'
    binding = json.loads(completed.read_text(encoding='utf-8'))
    assert binding['schema_version'] == 1
    assert binding['transport']['issue_updated_at'] == '2026-09-09T10:01:00Z'

    if mutation == 'closed':
        issue.update(state='closed', state_reason='completed', closed_at='2026-09-10T08:00:00Z',
                     updated_at='2026-09-10T08:00:00Z')
    elif mutation == 'updated_at':
        issue['updated_at'] = '2026-09-10T08:00:00Z'
    elif mutation == 'state_reason_closed_at':
        issue.update(state_reason='completed', closed_at='2026-09-10T08:00:00Z')
    else:
        issue.update(state='closed', state_reason='completed', closed_at='2026-09-10T08:00:00Z',
                     updated_at='2026-09-10T08:00:00Z')
        assert pickup_once(root)['status'] == 'no_pending'
        issue.update(state='open', state_reason=None, updated_at='2026-09-10T09:00:00Z')

    before = canonical(root)
    head = git(root, 'rev-parse', 'HEAD')
    index = (root / '.git/index').read_bytes()
    result = pickup_once(root)
    assert result['status'] == 'no_pending'
    assert result['processed_count'] == 1
    assert canonical(root) == before
    assert git(root, 'rev-parse', 'HEAD') == head
    assert (root / '.git/index').read_bytes() == index
    assert not list((root / 'inbox/sync').glob('*.yaml'))


def test_completed_lifecycle_change_does_not_block_newer_pending(fixture):
    root, *_ = fixture
    completed_issue = add_issue(fixture, 1)
    first = pickup_once(root)
    complete_rejected(root, fixture[3], first['pack_id'])
    completed_issue.update(
        state='closed', state_reason='completed', closed_at='2026-09-10T08:00:00Z',
        updated_at='2026-09-10T08:00:00Z',
    )
    add_issue(fixture, 2)
    result = pickup_once(root)
    assert result['status'] == 'created' and result['issue_number'] == 2
    packs = list((root / 'inbox/sync').glob('*.yaml'))
    assert len(packs) == 1 and packs[0].stem == result['pack_id']


@pytest.mark.parametrize('mutation', ['body', 'title', 'author'])
def test_completed_request_identity_drift_still_blocks(fixture, mutation):
    root, *_ = fixture
    issue = add_issue(fixture, 1)
    first = pickup_once(root)
    complete_rejected(root, fixture[3], first['pack_id'])
    if mutation == 'body':
        issue['body'] += '\n# request changed'
    elif mutation == 'title':
        issue['title'] += ' changed'
    else:
        issue['user']['login'] = 'intruder'
    add_issue(fixture, 2)
    result = pickup_once(root)
    assert result['status'] == 'blocked_conflict'
    assert 'completed issue #1' in result['reason']
    assert not list((root / 'inbox/sync').glob('*.yaml'))


def test_completed_archive_hash_mismatch_blocks(fixture):
    root, *_ = fixture
    add_issue(fixture, 1)
    first = pickup_once(root)
    complete_rejected(root, fixture[3], first['pack_id'])
    archived_pack = durable_roots(root)[0] / first['pack_id'] / 'pack.yaml'
    archived_pack.write_bytes(archived_pack.read_bytes() + b'\n')
    result = pickup_once(root)
    assert result['status'] == 'blocked_conflict'
    assert 'archived pack bytes differ' in result['reason']
    assert not list((root / 'inbox/sync').glob('*.yaml'))


def test_active_pack_blocks_without_new_github_intake(fixture):
    root, *_ = fixture
    add_issue(fixture, 1)
    first = pickup_once(root)
    add_issue(fixture, 2)
    calls = len(fixture[3]['calls'])
    result = pickup_once(root)
    assert result['status'] == 'blocked_active'
    assert result['active_packs'] == [first['pack_id']]
    assert len(fixture[3]['calls']) == calls
    assert len(list((root / 'inbox/sync').glob('*.yaml'))) == 1


def test_awaiting_push_has_specific_block_status(fixture):
    root, obj, *_ = fixture
    add_issue(fixture, 1)
    first = pickup_once(root)
    obj.write_text(obj.read_text(encoding='utf-8') + '\nVerified edit.\n', encoding='utf-8')
    verify_sync(root, first['pack_id'])
    finalize_sync(root, first['pack_id'], commit=True)
    result = pickup_once(root)
    assert result['status'] == 'blocked_awaiting_push'
    assert result['active_packs'] == [first['pack_id']]


def test_dirty_baseline_blocks_before_github_access(fixture):
    root, obj, *_ = fixture
    add_issue(fixture, 1)
    obj.write_text(obj.read_text(encoding='utf-8') + '\nDirty.\n', encoding='utf-8')
    result = pickup_once(root)
    assert result['status'] == 'blocked_dirty'
    assert obj.relative_to(root).as_posix() in result['dirty_paths']
    assert fixture[3]['calls'] == []
    assert not list((root / 'inbox/sync').glob('*.yaml'))


def test_non_sync_github_mode_blocks_before_transport(fixture):
    root, *_ = fixture
    config_path = root / 'project.yaml'
    config = yaml.safe_load(config_path.read_text(encoding='utf-8'))
    config['external_systems']['github']['mode'] = 'reference'
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding='utf-8')
    result = pickup_once(root)
    assert result['status'] == 'blocked_config'
    assert 'mode: sync' in result['reason']
    assert fixture[3]['calls'] == []


@pytest.mark.parametrize('kind', ['ordinary', 'unauthorized'])
def test_ordinary_and_unauthorized_issues_are_ignored(fixture, kind):
    kwargs = {'marker': False} if kind == 'ordinary' else {'author': 'intruder'}
    add_issue(fixture, 1, **kwargs)
    result = pickup_once(fixture[0])
    assert result['status'] == 'no_pending'
    assert not list((fixture[0] / 'inbox/sync').glob('*.yaml'))


def test_malformed_authorized_oldest_blocks_without_skipping_to_next(fixture):
    oldest = add_issue(fixture, 1)
    oldest['body'] = 'schema_version: ['
    add_issue(fixture, 2)
    result = pickup_once(fixture[0])
    assert result['status'] == 'blocked_malformed'
    assert 'oldest issue #1' in result['reason']
    assert not list((fixture[0] / 'inbox/sync').glob('*.yaml'))


def test_duplicate_request_id_blocks_entire_queue(fixture):
    first = add_issue(fixture, 1)
    duplicate = yaml.safe_load(first['body'])
    add_issue(fixture, 2, request=duplicate)
    result = pickup_once(fixture[0])
    assert result['status'] == 'blocked_conflict'
    assert 'duplicate request_id' in result['reason']
    assert not list((fixture[0] / 'inbox/sync').glob('*.yaml'))


def test_completed_transport_drift_blocks_newer_pending(fixture):
    root, *_ = fixture
    issue = add_issue(fixture, 1)
    first = pickup_once(root)
    complete_rejected(root, fixture[3], first['pack_id'])
    issue['body'] += '\n# request identity changed'
    add_issue(fixture, 2)
    result = pickup_once(root)
    assert result['status'] == 'blocked_conflict'
    assert 'completed issue #1' in result['reason']
    assert not list((root / 'inbox/sync').glob('*.yaml'))


def test_unfinished_transaction_blocks_cycle(fixture, monkeypatch):
    root, *_ = fixture
    add_issue(fixture, 1)
    first = pickup_once(root)

    def crash(stage, **kwargs):
        if stage == 'before_publish':
            raise OSError('simulated crash')

    monkeypatch.setattr(sync_bindings, '_terminalize_hook', crash)
    with pytest.raises(Exception, match='simulated crash'):
        finalize_sync(
            root, first['pack_id'], complete=True, outcome='rejected', reason='Rejected.',
        )
    monkeypatch.setattr(sync_bindings, '_terminalize_hook', lambda *args, **kwargs: None)
    result = pickup_once(root)
    assert result['status'] == 'blocked_transaction'


def test_existing_intake_lock_prevents_selection_race(fixture):
    add_issue(fixture, 1)
    with _intake_lock(fixture[0]):
        result = pickup_once(fixture[0])
    assert result['status'] == 'blocked_conflict'
    assert 'already running' in result['reason']
    assert not list((fixture[0] / 'inbox/sync').glob('*.yaml'))


def test_cli_prints_structured_cycle_result(fixture, monkeypatch, capsys):
    add_issue(fixture, 1)
    monkeypatch.chdir(fixture[0])
    main(['sync', 'watch', '--once'])
    output = capsys.readouterr().out
    assert f'Repository: {REPO}' in output
    assert 'Status: created' in output
    assert 'Issue: 1' in output and 'Plan: ready' in output
