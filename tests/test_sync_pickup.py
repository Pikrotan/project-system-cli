from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess

import pytest
import yaml

from project_system.cli import main
from project_system.init_project import init_project
from project_system.objects import create_object
from project_system import sync_bindings, sync_pickup, sync_pull, sync_watcher
from project_system.sync_finalization import finalize_sync
from project_system.sync_intake import _intake_lock
from project_system.sync_bindings import durable_roots
from project_system.sync_pickup import pickup_once
from project_system.sync_verification import verify_sync
from project_system.sync_watcher import (
    SyncWatcherAlreadyRunningError, SyncWatcherError, run_watcher, watcher_lock,
)


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


class FakeRuntime:
    def __init__(self):
        self.now = datetime(2026, 9, 10, 10, 0, tzinfo=timezone.utc)
        self.delays = []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.delays.append(seconds)
        self.now += timedelta(seconds=seconds)


def fake_result(status, **fields):
    return {'schema_version': 1, 'repository': REPO, 'status': status, **fields}


def load_events(root):
    path = root / '.generated/sync/auto/events.jsonl'
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]


def run_one_idle_cycle(root):
    runtime = FakeRuntime()
    return run_watcher(
        root, interval=60, pickup=lambda current: fake_result('no_pending'),
        clock=runtime.clock, sleeper=runtime.sleep, max_cycles=1,
    )


def test_persistent_watcher_repeats_no_pending_without_real_sleep(fixture):
    runtime = FakeRuntime()
    calls = []

    def pickup(root):
        calls.append(root)
        return fake_result('no_pending')

    result = run_watcher(
        fixture[0], interval=60, pickup=pickup, clock=runtime.clock,
        sleeper=runtime.sleep, max_cycles=3,
    )
    assert result['status'] == 'stopped' and result['cycles'] == 3
    assert len(calls) == 3 and runtime.delays == [60, 60]
    state = json.loads((fixture[0] / '.generated/sync/auto/state.json').read_text())
    assert state['watcher_status'] == 'stopped'
    assert state['last_cycle_status'] == 'no_pending'
    assert state['consecutive_failures'] == 0
    assert state['next_check_at'] is None


def test_created_then_active_block_uses_existing_pickup_and_exactly_one_pack(fixture):
    root, *_ = fixture
    add_issue(fixture, 1)
    runtime = FakeRuntime()
    before = canonical(root)
    head = git(root, 'rev-parse', 'HEAD')
    index = (root / '.git/index').read_bytes()
    reports = []
    result = run_watcher(
        root, interval=60, clock=runtime.clock, sleeper=runtime.sleep,
        max_cycles=2, on_cycle=lambda report, state: reports.append(report),
    )
    assert result['cycles'] == 2
    assert [report['status'] for report in reports] == ['created', 'blocked_active']
    assert len(list((root / 'inbox/sync').glob('*.yaml'))) == 1
    assert canonical(root) == before
    assert git(root, 'rev-parse', 'HEAD') == head
    assert (root / '.git/index').read_bytes() == index


def test_retryable_backoff_grows_caps_and_success_resets(fixture):
    runtime = FakeRuntime()
    statuses = iter([
        'blocked_transport', 'blocked_transport', 'blocked_transport',
        'blocked_transport', 'blocked_transport', 'blocked_transport',
        'no_pending', 'blocked_transport',
    ])
    states = []
    run_watcher(
        fixture[0], interval=120,
        pickup=lambda root: fake_result(next(statuses)),
        clock=runtime.clock, sleeper=runtime.sleep, max_cycles=8,
        on_cycle=lambda report, state: states.append(state),
    )
    assert runtime.delays == [120, 240, 480, 960, 1800, 1800, 120]
    assert [state['current_delay_seconds'] for state in states] == [
        120, 240, 480, 960, 1800, 1800, 120, 120,
    ]
    assert states[6]['consecutive_failures'] == 0


@pytest.mark.parametrize('status', [
    'blocked_dirty', 'blocked_awaiting_push', 'blocked_transaction',
])
def test_operational_block_remains_live_with_backoff(fixture, status):
    runtime = FakeRuntime()
    reports = []
    result = run_watcher(
        fixture[0], interval=60, pickup=lambda root: fake_result(status),
        clock=runtime.clock, sleeper=runtime.sleep, max_cycles=2,
        on_cycle=lambda report, state: reports.append(report['status']),
    )
    assert result['cycles'] == 2 and reports == [status, status]
    assert runtime.delays == [60]


@pytest.mark.parametrize('status', [
    'blocked_malformed', 'blocked_conflict', 'blocked_config',
])
def test_human_required_status_stops_fail_closed(fixture, status):
    runtime = FakeRuntime()
    with pytest.raises(SyncWatcherError, match=status):
        run_watcher(
            fixture[0], interval=60, pickup=lambda root: fake_result(status),
            clock=runtime.clock, sleeper=runtime.sleep,
        )
    assert runtime.delays == []
    state = json.loads((fixture[0] / '.generated/sync/auto/state.json').read_text())
    assert state['watcher_status'] == 'error'
    assert state['last_cycle_status'] == status
    assert load_events(fixture[0])[-1]['event'] == 'watcher_stopped'


def test_invalid_config_is_fatal_before_lock_or_pickup(fixture):
    root = fixture[0]
    config_path = root / 'project.yaml'
    config = yaml.safe_load(config_path.read_text(encoding='utf-8'))
    config['external_systems']['github']['enabled'] = False
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding='utf-8')
    called = []
    with pytest.raises(SyncWatcherError, match='startup configuration'):
        run_watcher(root, pickup=lambda root: called.append(root))
    assert called == []
    assert not (root / '.generated/sync/auto/state.json').exists()


def test_ctrl_c_writes_clean_stop_and_releases_lock(fixture):
    runtime = FakeRuntime()

    def interrupt(seconds):
        raise KeyboardInterrupt

    result = run_watcher(
        fixture[0], interval=60, pickup=lambda root: fake_result('no_pending'),
        clock=runtime.clock, sleeper=interrupt,
    )
    assert result['interrupted'] is True and result['cycles'] == 1
    state = json.loads((fixture[0] / '.generated/sync/auto/state.json').read_text())
    assert state['watcher_status'] == 'stopped' and state['stopped_at']
    assert load_events(fixture[0])[-1]['stop_reason'] == 'keyboard_interrupt'
    with watcher_lock(fixture[0]):
        pass


def test_watcher_lock_rejects_second_owner_and_recovers_after_release(fixture):
    root = fixture[0]
    with watcher_lock(root):
        with pytest.raises(SyncWatcherAlreadyRunningError, match='already running'):
            with watcher_lock(root):
                pass
    with watcher_lock(root):
        pass


def test_watcher_lock_released_after_cycle_exception(fixture):
    def fail(root):
        raise RuntimeError('sensitive body: TOKEN=secret')

    with pytest.raises(SyncWatcherError, match='RuntimeError'):
        run_watcher(fixture[0], interval=60, pickup=fail)
    with watcher_lock(fixture[0]):
        pass
    serialized = (fixture[0] / '.generated/sync/auto/events.jsonl').read_text(encoding='utf-8')
    assert 'TOKEN' not in serialized and 'sensitive body' not in serialized


def test_watcher_rejects_symlinked_runtime_directory(fixture, tmp_path):
    root = fixture[0]
    auto = root / '.generated/sync/auto'
    auto.parent.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / 'outside-watcher'
    outside.mkdir()
    try:
        auto.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip('cannot create a symlink or junction')
    with pytest.raises(SyncWatcherError, match='symlink/junction'):
        run_watcher(root, interval=60, max_cycles=1)


def test_state_is_atomic_and_event_log_is_sanitized_valid_jsonl(fixture, monkeypatch):
    runtime = FakeRuntime()
    replacements = []
    original_replace = sync_watcher.os.replace

    def record_replace(source, target):
        replacements.append((source, target))
        return original_replace(source, target)

    monkeypatch.setattr(sync_watcher.os, 'replace', record_replace)
    report = fake_result(
        'blocked_transport', issue_number=7, pack_id='SYNC-20260910-1234abcd',
        reason='TOKEN=secret full issue body',
    )
    run_watcher(
        fixture[0], interval=60, pickup=lambda root: report,
        clock=runtime.clock, sleeper=runtime.sleep, max_cycles=1,
    )
    state_path = fixture[0] / '.generated/sync/auto/state.json'
    state = json.loads(state_path.read_text(encoding='utf-8'))
    events = load_events(fixture[0])
    assert replacements and all(Path(target) == state_path for _, target in replacements)
    assert not list(state_path.parent.glob('*.tmp'))
    assert state['last_issue'] == 7 and state['last_pack'] == 'SYNC-20260910-1234abcd'
    assert all(event['schema_version'] == 1 for event in events)
    serialized = json.dumps({'state': state, 'events': events})
    assert 'TOKEN' not in serialized and 'full issue body' not in serialized


def test_event_recovery_accepts_empty_log(fixture):
    path = fixture[0] / '.generated/sync/auto/events.jsonl'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'')
    run_one_idle_cycle(fixture[0])
    events = load_events(fixture[0])
    assert events[0]['event'] == 'watcher_started'
    assert all(isinstance(event, dict) for event in events)


def test_event_recovery_preserves_valid_newline_terminated_log(fixture):
    path = fixture[0] / '.generated/sync/auto/events.jsonl'
    path.parent.mkdir(parents=True, exist_ok=True)
    original = b'{"event":"existing","schema_version":1}\n'
    path.write_bytes(original)
    run_one_idle_cycle(fixture[0])
    raw = path.read_bytes()
    assert raw.startswith(original)
    assert b'event_log_recovered' not in raw
    assert all(isinstance(json.loads(line), dict) for line in raw.splitlines() if line)


def test_event_recovery_preserves_valid_final_record_without_newline(fixture):
    path = fixture[0] / '.generated/sync/auto/events.jsonl'
    path.parent.mkdir(parents=True, exist_ok=True)
    original = b'{"event":"existing","schema_version":1}'
    path.write_bytes(original)
    run_one_idle_cycle(fixture[0])
    lines = path.read_bytes().splitlines()
    assert lines[0] == original
    parsed = [json.loads(line) for line in lines if line]
    assert parsed[0]['event'] == 'existing'
    assert parsed[1]['event'] == 'watcher_started'
    assert parsed[2]['event'] == 'event_log_recovered'
    assert parsed[2]['recovery_action'] == 'normalized_final_newline'


def test_event_recovery_discards_only_partial_tail_and_preserves_records(fixture):
    path = fixture[0] / '.generated/sync/auto/events.jsonl'
    path.parent.mkdir(parents=True, exist_ok=True)
    complete = (
        b'{"event":"first","schema_version":1}\n'
        b'{"event":"second","schema_version":1}\n'
    )
    partial = b'{"event":"crash_test_partial"'
    path.write_bytes(complete + partial)
    run_one_idle_cycle(fixture[0])
    raw = path.read_bytes()
    assert raw.startswith(complete)
    assert partial not in raw
    assert b'}{' not in raw
    parsed = [json.loads(line) for line in raw.splitlines() if line]
    assert [item['event'] for item in parsed[:2]] == ['first', 'second']
    assert parsed[2]['event'] == 'watcher_started'
    recovery = parsed[3]
    assert recovery['event'] == 'event_log_recovered'
    assert recovery['removed_tail_bytes'] == len(partial)
    assert len(recovery['removed_tail_sha256']) == 64


@pytest.mark.parametrize('raw', [
    b'{"event":"valid"}\n{"event":bad}\n{"event":"later"}\n',
    b'{"event":"valid"}\n{"event":bad}\n',
])
def test_event_recovery_fails_closed_on_complete_corruption(fixture, raw):
    root = fixture[0]
    path = root / '.generated/sync/auto/events.jsonl'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    with pytest.raises(SyncWatcherError, match='malformed complete record'):
        run_one_idle_cycle(root)
    assert path.read_bytes() == raw
    assert not (root / '.generated/sync/auto/state.json').exists()
    with watcher_lock(root):
        pass


def test_event_recovery_never_exposes_removed_fragment(fixture):
    root = fixture[0]
    path = root / '.generated/sync/auto/events.jsonl'
    path.parent.mkdir(parents=True, exist_ok=True)
    secret = b'{"event":"partial","body":"SUPER_SECRET_REQUEST_PAYLOAD"'
    path.write_bytes(b'{"event":"valid"}\n' + secret)
    run_one_idle_cycle(root)
    serialized = path.read_bytes() + (root / '.generated/sync/auto/state.json').read_bytes()
    assert b'SUPER_SECRET_REQUEST_PAYLOAD' not in serialized
    recovery = next(event for event in load_events(root)
                    if event['event'] == 'event_log_recovered')
    assert recovery['removed_tail_bytes'] == len(secret)


def test_event_recovery_occurs_only_after_watcher_lock(fixture):
    root = fixture[0]
    path = root / '.generated/sync/auto/events.jsonl'
    path.parent.mkdir(parents=True, exist_ok=True)
    damaged = b'{"event":"valid"}\n{"event":"partial"'
    path.write_bytes(damaged)
    with watcher_lock(root):
        with pytest.raises(SyncWatcherAlreadyRunningError):
            run_one_idle_cycle(root)
        assert path.read_bytes() == damaged
    run_one_idle_cycle(root)
    assert all(isinstance(json.loads(line), dict)
               for line in path.read_bytes().splitlines() if line)


@pytest.mark.parametrize('interval', [0, 59, 3601, True])
def test_watcher_rejects_invalid_interval(fixture, interval):
    with pytest.raises(SyncWatcherError, match='60..3600'):
        run_watcher(fixture[0], interval=interval, max_cycles=1)


def test_cli_persistent_watcher_uses_runtime_and_interval(fixture, monkeypatch, capsys):
    monkeypatch.chdir(fixture[0])
    captured = {}

    def fake_watch(root, interval, on_cycle):
        captured.update(root=root, interval=interval)
        on_cycle(fake_result('no_pending'), {
            'last_cycle_at': '2026-09-10T10:00:00Z',
            'current_delay_seconds': interval,
        })
        return {'cycles': 1, 'interrupted': False}

    monkeypatch.setattr('project_system.cli.run_watcher', fake_watch)
    main(['sync', 'watch', '--interval', '60'])
    assert captured == {'root': fixture[0], 'interval': 60}
    output = capsys.readouterr().out
    assert 'Persistent foreground watcher starting' in output
    assert 'Status: no_pending' in output


def test_cli_ctrl_c_result_exits_130(fixture, monkeypatch, capsys):
    monkeypatch.chdir(fixture[0])
    monkeypatch.setattr('project_system.cli.run_watcher', lambda *args, **kwargs: {
        'cycles': 1, 'interrupted': True,
    })
    with pytest.raises(SystemExit) as exc:
        main(['sync', 'watch'])
    assert exc.value.code == 130
    assert 'Watcher stopped after 1 cycle(s)' in capsys.readouterr().out
