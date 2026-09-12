from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET

import pytest
import yaml

from project_system import process_runner, sync_pull
from project_system.cli import main
from project_system.init_project import init_project
from project_system.sync_auto import (
    BACKGROUND_RUNNER_MODULE, SyncAutoError, _legacy_arguments,
    _registration_hash, _task_verification_error,
    build_registration, install_auto, load_registration, registration_id_for,
    pythonw_executable, record_background_failure, registration_path,
    remove_auto, run_auto, status_auto,
)
from project_system.sync_auto_runner import main as background_runner_main
from project_system.sync_auto_windows import (
    WindowsSchedulerError, WindowsTaskScheduler, inspect_task_xml, task_xml,
)
from project_system.sync_watcher import watcher_lock


REPO = 'auto-owner/auto-project'
NOW = datetime(2026, 9, 11, 10, 0, tzinfo=timezone.utc)


def git(root, *args):
    result = subprocess.run(
        ['git', *args], cwd=root, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    return result.stdout.strip()


def project_fixture(tmp_path, name='Auto Project', repository=REPO):
    root = init_project(name, tmp_path / f'{name} workspace ü')
    config_path = root / 'project.yaml'
    config = yaml.safe_load(config_path.read_text(encoding='utf-8'))
    config['external_systems']['github'].update(enabled=True, sync_pull={
        'allowed_authors': ['auto-owner'], 'expected_repository': repository,
    })
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding='utf-8')
    git(root, 'init', '-q')
    git(root, 'config', 'user.name', 'Auto Tests')
    git(root, 'config', 'user.email', 'auto@example.invalid')
    git(root, 'remote', 'add', 'origin', f'https://github.com/{repository}.git')
    git(root, 'add', '.')
    git(root, 'commit', '-qm', 'baseline')
    return root


def canonical(root):
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob('*')
        if path.is_file() and path.relative_to(root).parts[0] not in {'.git', '.generated'}
    }


class FakeScheduler:
    def __init__(self):
        self.tasks = {}
        self.creates = []
        self.deletes = []
        self.fail_create = False
        self.fail_inspect = False
        self.fail_delete = False
        self.force_unowned = False
        self.verify_mismatches = None

    def inspect(self, registration):
        if self.fail_inspect:
            raise RuntimeError('simulated scheduler inspection failure')
        stored = self.tasks.get(registration['task_name'])
        if stored is None:
            return {
                'exists': False, 'owned': False, 'matches': False,
                'interval_seconds': None, 'task_state': 'missing',
                'next_scheduled_run': None, 'raw_xml': None,
            }
        if self.verify_mismatches is not None:
            ownership = [
                item for item in self.verify_mismatches if item.get('ownership')
            ]
            return {
                'exists': True, 'owned': not ownership, 'matches': False,
                'mismatches': deepcopy(self.verify_mismatches),
                'ownership_mismatches': deepcopy(ownership),
                'configuration_mismatches': [
                    deepcopy(item) for item in self.verify_mismatches
                    if not item.get('ownership')
                ],
                'interval_seconds': stored['interval_seconds'],
                'task_state': 'registered', 'next_scheduled_run': None,
                'raw_xml': deepcopy(stored),
            }
        owned = not self.force_unowned and all((
            stored['registration_id'] == registration['registration_id'],
            stored['task_name'] == registration['task_name'],
            stored['runner'] == registration['runner'],
        ))
        matches = owned and stored['interval_seconds'] == registration['interval_seconds']
        return {
            'exists': True, 'owned': owned, 'matches': matches,
            'interval_seconds': stored['interval_seconds'],
            'task_state': 'registered', 'next_scheduled_run': None,
            'raw_xml': deepcopy(stored),
        }

    def create(self, registration, *, replace=False):
        self.creates.append((registration['registration_id'], replace))
        if self.fail_create:
            raise RuntimeError('simulated scheduler failure SECRET')
        if registration['task_name'] in self.tasks and not replace:
            raise RuntimeError('task exists')
        self.tasks[registration['task_name']] = deepcopy(registration)

    def restore(self, task_name, raw_xml):
        self.tasks[task_name] = deepcopy(raw_xml)

    def delete(self, registration):
        if self.fail_delete:
            raise RuntimeError('simulated scheduler deletion failure')
        self.deletes.append(registration['registration_id'])
        self.tasks.pop(registration['task_name'], None)


@pytest.fixture
def auto(tmp_path):
    return (
        project_fixture(tmp_path),
        tmp_path / 'Local App Data ü' / 'ProjectSystem' / 'watchers',
        FakeScheduler(),
    )


def install(auto, **kwargs):
    root, store, scheduler = auto
    return install_auto(
        root, adapter=scheduler, store_root=store, clock=lambda: NOW,
        gh_check=lambda: 'gh.exe', **kwargs,
    )


def test_install_success_and_registration_contract(auto):
    root, store, scheduler = auto
    before = canonical(root)
    head = git(root, 'rev-parse', 'HEAD')
    index = (root / '.git/index').read_bytes()
    result = install(auto, interval=120)
    registration = result['registration']
    assert result['status'] == 'installed'
    assert registration['registration_id'] == registration_id_for(
        root, registration['project_id'], REPO,
    )
    assert registration['project_root'] == str(root.resolve())
    assert registration['interval_seconds'] == 120
    assert registration['repository'] == REPO
    assert Path(registration['runner']['executable']).name.casefold() == 'pythonw.exe'
    assert registration['runner']['arguments'] == [
        '-m', BACKGROUND_RUNNER_MODULE,
        '--registration', registration['registration_id'],
    ]
    action_text = ' '.join([
        registration['runner']['executable'], *registration['runner']['arguments'],
    ]).casefold()
    assert 'project.exe' not in action_text
    assert 'cmd.exe' not in action_text
    assert 'powershell.exe' not in action_text
    loaded, path, _ = load_registration(registration['registration_id'], store)
    assert loaded == registration and path.is_file()
    assert scheduler.inspect(registration)['matches']
    assert canonical(root) == before
    assert git(root, 'rev-parse', 'HEAD') == head
    assert (root / '.git/index').read_bytes() == index


def test_pythonw_is_derived_from_the_exact_current_runtime(tmp_path):
    runtime = tmp_path / 'Python Runtime ü' / 'python.exe'
    background = runtime.with_name('pythonw.exe')
    runtime.parent.mkdir()
    runtime.write_bytes(b'MZ')
    background.write_bytes(b'MZ')
    assert pythonw_executable(str(runtime)) == str(background.resolve())
    assert pythonw_executable(str(background)) == str(background.resolve())
    with pytest.raises(SyncAutoError, match='python.exe/pythonw.exe'):
        pythonw_executable(str(runtime.with_name('project.exe')))


def test_console_executable_and_extra_arguments_fail_ownership(auto):
    sid = 'S-1-5-21-123-456-789-1001'
    registration = build_registration(
        auto[0], 'auto-project', REPO, 60,
        installed_at='2026-09-11T10:00:00Z',
    )
    root = ET.fromstring(task_xml(registration, sid, start_at=NOW))
    root.find('.//{*}Command').text = str(
        Path(registration['runner']['executable']).with_name('python.exe')
    )
    root.find('.//{*}Arguments').text += ' --unexpected'
    inspection = inspect_task_xml(
        ET.tostring(root, encoding='unicode'), registration,
        expected_user_sid=sid,
    )
    fields = {item['field'] for item in inspection['ownership_mismatches']}
    assert {'executable', 'arguments'} <= fields
    assert not inspection['owned'] and not inspection['matches']


def test_legacy_console_registration_requires_explicit_replace(auto):
    installed = install(auto)
    registration = deepcopy(installed['registration'])
    registration['runner'] = {
        'executable': str(
            Path(registration['runner']['executable']).with_name('python.exe')
        ),
        'arguments': _legacy_arguments(registration['registration_id']),
    }
    registration['registration_sha256'] = _registration_hash(registration)
    path = registration_path(registration['registration_id'], auto[1])
    path.write_text(json.dumps(registration), encoding='utf-8')
    auto[2].tasks[registration['task_name']] = deepcopy(registration)

    report = status_auto(auto[0], adapter=auto[2], store_root=auto[1])
    assert report['installed'] is False
    assert report['runner_type'] == 'legacy_console'
    assert any('replace required' in warning for warning in report['warnings'])
    with pytest.raises(SyncAutoError, match='--replace'):
        install(auto)
    replaced = install(auto, replace=True)
    assert replaced['status'] == 'replaced'
    assert replaced['registration']['runner']['arguments'][1] == BACKGROUND_RUNNER_MODULE
    assert Path(replaced['registration']['runner']['executable']).name.casefold() == 'pythonw.exe'


def test_identical_install_is_idempotent(auto):
    first = install(auto)
    second = install(auto)
    assert second['status'] == 'already_installed'
    assert second['registration'] == first['registration']
    assert len(auto[2].creates) == 1


def test_conflicting_install_requires_replace_and_replace_succeeds(auto):
    first = install(auto, interval=120)
    with pytest.raises(SyncAutoError, match='--replace'):
        install(auto, interval=300)
    replaced = install(auto, interval=300, replace=True)
    assert replaced['status'] == 'replaced'
    assert replaced['registration']['interval_seconds'] == 300
    assert auto[2].inspect(replaced['registration'])['matches']
    assert first['registration']['registration_id'] == replaced['registration']['registration_id']


def test_install_rolls_back_registration_when_scheduler_fails(auto):
    auto[2].fail_create = True
    with pytest.raises(SyncAutoError, match='rolled back'):
        install(auto)
    registration_id = registration_id_for(auto[0], 'auto-project', REPO)
    assert not registration_path(registration_id, auto[1]).exists()
    assert auto[2].tasks == {}


def test_status_installed_and_machine_schema(auto):
    installed = install(auto)
    report = status_auto(auto[0], adapter=auto[2], store_root=auto[1])
    assert report['schema_version'] == 1
    assert report['installed'] is True
    assert report['registration_exists'] and report['task_exists']
    assert report['task_owned'] and report['task_matches']
    assert report['registration_id'] == installed['registration']['registration_id']
    assert report['task_state'] == 'registered'
    assert report['runner_type'] == 'background'
    assert Path(report['runner_executable']).name.casefold() == 'pythonw.exe'


def test_status_missing_task_and_missing_registration(auto):
    installed = install(auto)
    auto[2].tasks.clear()
    missing_task = status_auto(auto[0], adapter=auto[2], store_root=auto[1])
    assert not missing_task['installed'] and missing_task['registration_exists']
    assert not missing_task['task_exists']
    registration_path(installed['registration']['registration_id'], auto[1]).unlink()
    missing_both = status_auto(auto[0], adapter=auto[2], store_root=auto[1])
    assert not missing_both['installed'] and not missing_both['registration_exists']


def test_status_treats_running_state_as_advisory(auto):
    install(auto)
    state = auto[0] / '.generated/sync/auto/state.json'
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(json.dumps({
        'watcher_status': 'running', 'last_cycle_at': '2026-09-11T09:00:00Z',
        'last_cycle_status': 'no_pending', 'last_issue': None, 'last_pack': None,
        'last_error_category': None,
    }), encoding='utf-8')
    report = status_auto(auto[0], adapter=auto[2], store_root=auto[1])
    assert report['installed'] is True
    assert report['watcher_status_advisory'] == 'running'
    assert report['task_state'] == 'registered'


def test_status_json_cli_is_stable(auto, monkeypatch, capsys):
    monkeypatch.chdir(auto[0])
    expected = {'schema_version': 1, 'installed': False, 'warnings': []}
    monkeypatch.setattr('project_system.cli.status_auto', lambda root: expected)
    main(['sync', 'auto', 'status', '--json'])
    assert json.loads(capsys.readouterr().out) == expected


def test_remove_success_missing_task_cleanup_and_idempotency(auto):
    installed = install(auto)
    removed = remove_auto(auto[0], adapter=auto[2], store_root=auto[1])
    assert removed['status'] == 'removed'
    assert not registration_path(installed['registration']['registration_id'], auto[1]).exists()
    assert auto[2].tasks == {}
    again = remove_auto(auto[0], adapter=auto[2], store_root=auto[1])
    assert again['status'] == 'not_installed'

    install(auto)
    auto[2].tasks.clear()
    cleanup = remove_auto(auto[0], adapter=auto[2], store_root=auto[1])
    assert cleanup['status'] == 'removed'


def test_remove_refuses_ownership_mismatch(auto):
    installed = install(auto)
    auto[2].force_unowned = True
    with pytest.raises(SyncAutoError, match='ownership proof'):
        remove_auto(auto[0], adapter=auto[2], store_root=auto[1])
    assert registration_path(installed['registration']['registration_id'], auto[1]).exists()
    assert auto[2].tasks


def test_damaged_registration_is_fail_safe(auto):
    installed = install(auto)
    path = registration_path(installed['registration']['registration_id'], auto[1])
    path.write_text('{"schema_version":', encoding='utf-8')
    with pytest.raises(SyncAutoError, match='malformed JSON'):
        status_auto(auto[0], adapter=auto[2], store_root=auto[1])
    with pytest.raises(SyncAutoError, match='malformed JSON'):
        remove_auto(auto[0], adapter=auto[2], store_root=auto[1])
    assert auto[2].tasks


@pytest.mark.parametrize('status', ['no_pending', 'created'])
def test_auto_run_executes_exactly_one_bounded_cycle(auto, status):
    installed = install(auto)
    calls = []
    report = run_auto(
        installed['registration']['registration_id'], store_root=auto[1],
        pickup=lambda root: calls.append(root) or {
            'schema_version': 1, 'repository': REPO, 'status': status,
            'issue_number': 9 if status == 'created' else None,
            'pack_id': 'SYNC-20260911-1234abcd' if status == 'created' else None,
        }, clock=lambda: NOW,
    )
    assert report['status'] == status and report['cycles'] == 1
    assert calls == [auto[0]]
    state = json.loads((auto[0] / '.generated/sync/auto/state.json').read_text())
    assert state['mode'] == 'scheduled'
    assert state['registration_id'] == installed['registration']['registration_id']


@pytest.mark.parametrize('status', [
    'no_pending', 'processed', 'created', 'blocked_active',
])
def test_background_runner_preserves_success_exit_without_terminal_io(
    auto, status, monkeypatch,
):
    installed = install(auto)
    registration_id = installed['registration']['registration_id']
    calls = []

    class ForbiddenTerminal:
        def write(self, value):
            raise AssertionError('background runner attempted terminal output')

        def flush(self):
            pass

    monkeypatch.setattr(sys, 'stdout', ForbiddenTerminal())
    monkeypatch.setattr(sys, 'stderr', ForbiddenTerminal())
    code = background_runner_main(
        ['--registration', registration_id],
        run=lambda value: calls.append(value) or {'status': status},
        recorder=lambda *args: pytest.fail('success must not record a failure'),
    )
    assert code == 0
    assert calls == [registration_id]


def test_background_runner_executes_one_real_bounded_cycle(auto):
    installed = install(auto)
    registration_id = installed['registration']['registration_id']
    calls = []
    code = background_runner_main(
        ['--registration', registration_id],
        run=lambda value: run_auto(
            value, store_root=auto[1],
            pickup=lambda root: calls.append(root) or {
                'schema_version': 1, 'repository': REPO,
                'status': 'no_pending', 'issue_number': None,
                'pack_id': None,
            },
            clock=lambda: NOW,
        ),
    )
    assert code == 0 and calls == [auto[0]]
    state = json.loads(
        (auto[0] / '.generated/sync/auto/state.json').read_text(encoding='utf-8')
    )
    assert state['last_cycle_status'] == 'no_pending'
    assert state['mode'] == 'scheduled'
    assert git(auto[0], 'status', '--short') == ''


def test_auto_run_propagates_no_console_to_nested_git_and_gh(auto, monkeypatch):
    installed = install(auto)
    registration_id = installed['registration']['registration_id']
    calls = []
    no_window = 0x08000000

    def external(arguments, **kwargs):
        calls.append((list(arguments), dict(kwargs)))
        if arguments[0] == 'git':
            return subprocess.CompletedProcess(
                arguments, 0, f'https://github.com/{REPO}.git\n', '',
            )
        return subprocess.CompletedProcess(arguments, 0, b'{}', b'')

    monkeypatch.setattr(process_runner, '_is_windows', lambda: True)
    monkeypatch.setattr(
        process_runner.subprocess, 'CREATE_NO_WINDOW', no_window, raising=False,
    )
    monkeypatch.setattr(process_runner.subprocess, 'run', external)
    monkeypatch.setattr(sync_pull, '_gh_executable', lambda: 'gh.exe')

    def pickup(root):
        assert sync_pull.github_repository(root) == REPO
        assert sync_pull.gh_get(root, f'repos/{REPO}/issues/1') == {}
        return {
            'schema_version': 1, 'repository': REPO,
            'status': 'no_pending', 'issue_number': None, 'pack_id': None,
        }

    report = run_auto(
        registration_id, store_root=auto[1], pickup=pickup, clock=lambda: NOW,
    )
    children = [call for call in calls if call[0][0] in {'git', 'gh.exe'}]
    assert report['status'] == 'no_pending'
    assert {call[0][0] for call in children} == {'git', 'gh.exe'}
    assert all(call[1]['creationflags'] & no_window for call in children)
    assert all(call[1]['shell'] is False for call in children)


def test_background_runner_failure_is_nonzero_and_sanitized(auto):
    installed = install(auto)
    registration_id = installed['registration']['registration_id']

    def fail(value):
        raise RuntimeError('TOKEN=SUPER_SECRET_FROM_BACKGROUND')

    code = background_runner_main(
        ['--registration', registration_id], run=fail,
        recorder=lambda value, category: record_background_failure(
            value, category, store_root=auto[1], clock=lambda: NOW,
        ),
    )
    assert code == 1
    state_raw = (auto[0] / '.generated/sync/auto/state.json').read_text(
        encoding='utf-8'
    )
    events_raw = (auto[0] / '.generated/sync/auto/events.jsonl').read_text(
        encoding='utf-8'
    )
    assert 'background_runner_exception' in state_raw + events_raw
    assert 'SUPER_SECRET_FROM_BACKGROUND' not in state_raw + events_raw


def test_background_runner_rejects_non_exact_arguments_and_maps_sync_error():
    calls = []
    assert background_runner_main([], run=lambda value: calls.append(value)) == 2
    assert background_runner_main([
        '--registration', 'REG-0123456789abcdef', '--extra', 'value',
    ], run=lambda value: calls.append(value)) == 2

    def fail(value):
        raise SyncAutoError('sensitive detail')

    recorded = []
    code = background_runner_main(
        ['--registration', 'REG-0123456789abcdef'], run=fail,
        recorder=lambda *values: recorded.append(values),
    )
    assert code == SyncAutoError.exit_code
    assert recorded == [(
        'REG-0123456789abcdef', 'scheduled_run_error',
    )]
    assert calls == []


def test_concurrent_auto_run_is_blocked_by_watcher_lock(auto):
    installed = install(auto)
    with watcher_lock(auto[0]):
        with pytest.raises(SyncAutoError, match='already running'):
            run_auto(
                installed['registration']['registration_id'], store_root=auto[1],
                pickup=lambda root: {'status': 'no_pending'}, clock=lambda: NOW,
            )


def test_multiple_projects_have_independent_registration_and_tasks(tmp_path):
    store = tmp_path / 'LocalAppData/ProjectSystem/watchers'
    scheduler = FakeScheduler()
    first = project_fixture(tmp_path / 'one', 'Project One', 'owner/project-one')
    second = project_fixture(tmp_path / 'two', 'Project Two', 'owner/project-two')
    results = [install_auto(
        root, interval=interval, adapter=scheduler, store_root=store,
        clock=lambda: NOW, gh_check=lambda: 'gh.exe',
    ) for root, interval in ((first, 60), (second, 300))]
    ids = {item['registration']['registration_id'] for item in results}
    assert len(ids) == 2 and len(scheduler.tasks) == 2
    assert {item['registration']['interval_seconds'] for item in results} == {60, 300}


@pytest.mark.parametrize('registration_id', [
    '../REG-0123456789abcdef', 'REG-0123456789abcdef/other',
    r'REG-0123456789abcdef\other', 'REG-XYZ',
])
def test_registration_id_traversal_is_rejected(auto, registration_id):
    with pytest.raises(SyncAutoError, match='registration ID'):
        registration_path(registration_id, auto[1])


def test_task_xml_escapes_paths_and_uses_safe_action(auto):
    root = auto[0]
    registration = build_registration(
        root, 'auto-project', REPO, 120, installed_at='2026-09-11T10:00:00Z',
        executable=r'C:\Program Files\Python & Tools\python.exe',
    )
    raw = task_xml(registration, 'S-1-5-21-123-456-789-1001', start_at=NOW)
    assert b'&amp;' in raw or '&amp;' in raw.decode('utf-16')
    inspection = inspect_task_xml(raw.decode('utf-16'), registration)
    assert inspection['owned'] and inspection['matches']
    text = raw.decode('utf-16')
    assert 'shell' not in text.lower()
    assert registration['project_root'] not in text
    assert '<Command>C:\\Program Files\\Python &amp; Tools\\pythonw.exe</Command>' in text
    assert f'-m {BACKGROUND_RUNNER_MODULE} --registration' in text


def test_task_xml_principal_is_part_of_ownership(auto):
    registration = build_registration(
        auto[0], 'auto-project', REPO, 120,
        installed_at='2026-09-11T10:00:00Z',
    )
    raw = task_xml(registration, 'S-1-5-21-123-456-789-1001', start_at=NOW)
    inspection = inspect_task_xml(
        raw.decode('utf-16'), registration, 'S-1-5-21-999-888-777-1001',
    )
    assert not inspection['owned'] and not inspection['matches']


def test_windows_normalized_task_is_semantically_equivalent(auto):
    sid = 'S-1-5-21-123-456-789-1001'
    registration = build_registration(
        auto[0], 'auto-project', REPO, 60,
        installed_at='2026-09-11T10:00:00Z',
        executable=r'C:\Program Files\Python Runtime\python.exe',
    )
    root = ET.fromstring(task_xml(registration, sid, start_at=NOW))
    root.find('.//{*}UserId').text = r'DESKTOP\Auto User'
    root.find('.//{*}Command').text = r'c:/program files/python runtime/PYTHONW.EXE'
    root.find('.//{*}Arguments').text = '  ' + '   '.join(
        f'"{value}"' for value in registration['runner']['arguments']
    ) + '  '
    root.find('.//{*}Interval').text = 'PT1M'
    root.find('.//{*}Duration').text = 'PT24H'
    root.find('.//{*}StartWhenAvailable').text = '1'
    root.find('.//{*}ExecutionTimeLimit').text = 'PT600S'
    root.find('.//{*}MultipleInstancesPolicy').text = 'ignorenew'
    root.find('.//{*}LogonType').text = 'interactivetoken'
    root.find('.//{*}RunLevel').text = 'leastprivilege'
    trigger_enabled = root.find('.//{*}CalendarTrigger/{*}Enabled')
    root.find('.//{*}CalendarTrigger').remove(trigger_enabled)
    stop_at_end = root.find('.//{*}StopAtDurationEnd')
    root.find('.//{*}Repetition').remove(stop_at_end)

    inspection = inspect_task_xml(
        ET.tostring(root, encoding='unicode'), registration,
        expected_user_sid=sid, expected_user_name=r'desktop\auto user',
        account_sid_resolver=lambda account: sid,
    )
    assert inspection['owned'] and inspection['matches']
    assert inspection['mismatches'] == []

def test_missing_run_level_uses_only_its_documented_least_privilege_default(auto):
    sid = 'S-1-5-21-123-456-789-1001'
    registration = build_registration(
        auto[0], 'auto-project', REPO, 60,
        installed_at='2026-09-11T10:00:00Z',
    )
    root = ET.fromstring(task_xml(registration, sid, start_at=NOW))
    principal = root.find('.//{*}Principals/{*}Principal')
    principal.remove(principal.find('{*}RunLevel'))

    inspection = inspect_task_xml(
        ET.tostring(root, encoding='unicode'), registration,
        expected_user_sid=sid,
    )
    assert inspection['owned'] and inspection['matches']
    assert inspection['mismatches'] == []

    namespace = principal.tag[1:].split('}', 1)[0]
    ET.SubElement(principal, f'{{{namespace}}}RunLevel')
    empty_inspection = inspect_task_xml(
        ET.tostring(root, encoding='unicode'), registration,
        expected_user_sid=sid,
    )
    assert {item['field'] for item in empty_inspection['mismatches']} == {
        'run_level',
    }


def test_explicit_least_privilege_run_level_matches(auto):
    sid = 'S-1-5-21-123-456-789-1001'
    registration = build_registration(
        auto[0], 'auto-project', REPO, 60,
        installed_at='2026-09-11T10:00:00Z',
    )
    raw = task_xml(registration, sid, start_at=NOW).decode('utf-16')
    inspection = inspect_task_xml(raw, registration, expected_user_sid=sid)
    assert inspection['owned'] and inspection['matches']


def test_highest_available_run_level_remains_a_hard_mismatch(auto):
    sid = 'S-1-5-21-123-456-789-1001'
    registration = build_registration(
        auto[0], 'auto-project', REPO, 60,
        installed_at='2026-09-11T10:00:00Z',
    )
    root = ET.fromstring(task_xml(registration, sid, start_at=NOW))
    root.find('.//{*}RunLevel').text = 'HighestAvailable'
    inspection = inspect_task_xml(
        ET.tostring(root, encoding='unicode'), registration,
        expected_user_sid=sid,
    )
    assert inspection['owned'] is True and inspection['matches'] is False
    assert inspection['configuration_mismatches'] == [{
        'field': 'run_level', 'expected': 'LeastPrivilege',
        'actual': 'HighestAvailable', 'ownership': False,
    }]
    message = str(_task_verification_error(inspection))
    assert '- run_level: expected=LeastPrivilege actual=HighestAvailable' in message


def test_missing_security_critical_logon_type_does_not_gain_a_default(auto):
    sid = 'S-1-5-21-123-456-789-1001'
    registration = build_registration(
        auto[0], 'auto-project', REPO, 60,
        installed_at='2026-09-11T10:00:00Z',
    )
    root = ET.fromstring(task_xml(registration, sid, start_at=NOW))
    principal = root.find('.//{*}Principals/{*}Principal')
    principal.remove(principal.find('{*}LogonType'))
    inspection = inspect_task_xml(
        ET.tostring(root, encoding='unicode'), registration,
        expected_user_sid=sid,
    )
    fields = {item['field'] for item in inspection['mismatches']}
    assert fields == {'logon_type'}
    assert inspection['owned'] is True and inspection['matches'] is False


def test_install_accepts_windows_exported_task_without_run_level(auto):
    sid = 'S-1-5-21-123-456-789-1001'

    class MissingRunLevelScheduler:
        def __init__(self):
            self.raw_xml = None

        def inspect(self, registration):
            if self.raw_xml is None:
                return {
                    'exists': False, 'owned': False, 'matches': False,
                    'interval_seconds': None, 'task_state': 'missing',
                    'next_scheduled_run': None, 'raw_xml': None,
                }
            return inspect_task_xml(
                self.raw_xml, registration, expected_user_sid=sid,
            )

        def create(self, registration, *, replace=False):
            root = ET.fromstring(task_xml(registration, sid, start_at=NOW))
            principal = root.find('.//{*}Principals/{*}Principal')
            principal.remove(principal.find('{*}RunLevel'))
            self.raw_xml = ET.tostring(root, encoding='unicode')

        def delete(self, registration):
            self.raw_xml = None

        def restore(self, task_name, raw_xml):
            self.raw_xml = raw_xml

    result = install_auto(
        auto[0], interval=60, adapter=MissingRunLevelScheduler(),
        store_root=auto[1], clock=lambda: NOW, gh_check=lambda: 'gh.exe',
    )
    assert result['status'] == 'installed'


def test_verifier_reports_exact_configuration_mismatch_fields(auto):
    sid = 'S-1-5-21-123-456-789-1001'
    registration = build_registration(
        auto[0], 'auto-project', REPO, 60,
        installed_at='2026-09-11T10:00:00Z',
    )
    root = ET.fromstring(task_xml(registration, sid, start_at=NOW))
    root.find('.//{*}Interval').text = 'PT2M'
    root.find('.//{*}StartWhenAvailable').text = 'false'
    root.find('.//{*}ExecutionTimeLimit').text = 'PT9M'
    root.find('.//{*}LogonType').text = 'Password'
    inspection = inspect_task_xml(
        ET.tostring(root, encoding='unicode'), registration,
        expected_user_sid=sid,
    )
    fields = {item['field'] for item in inspection['mismatches']}
    assert fields == {
        'interval_seconds', 'start_when_available',
        'execution_time_limit_seconds', 'logon_type',
    }
    assert inspection['owned'] is True and inspection['matches'] is False
    message = str(_task_verification_error(inspection))
    for field in fields:
        assert f'- {field}: expected=' in message
    assert '<Task' not in message


def test_user_name_must_resolve_to_the_exact_expected_sid(auto):
    sid = 'S-1-5-21-123-456-789-1001'
    registration = build_registration(
        auto[0], 'auto-project', REPO, 120,
        installed_at='2026-09-11T10:00:00Z',
    )
    root = ET.fromstring(task_xml(registration, sid, start_at=NOW))
    root.find('.//{*}UserId').text = r'DESKTOP\DifferentUser'
    wrong_sid = 'S-1-5-21-999-888-777-1001'
    inspection = inspect_task_xml(
        ET.tostring(root, encoding='unicode'), registration,
        expected_user_sid=sid,
        account_sid_resolver=lambda account: wrong_sid,
    )
    assert not inspection['owned'] and not inspection['matches']
    assert {item['field'] for item in inspection['ownership_mismatches']} == {'user_id'}


def test_account_alias_is_accepted_only_when_it_resolves_to_same_sid(auto):
    sid = 'S-1-5-21-123-456-789-1001'
    registration = build_registration(
        auto[0], 'auto-project', REPO, 120,
        installed_at='2026-09-11T10:00:00Z',
    )
    root = ET.fromstring(task_xml(registration, sid, start_at=NOW))
    root.find('.//{*}UserId').text = 'auto.user@example.invalid'
    inspection = inspect_task_xml(
        ET.tostring(root, encoding='unicode'), registration,
        expected_user_sid=sid, expected_user_name=r'DESKTOP\Auto User',
        account_sid_resolver=lambda account: sid,
    )
    assert inspection['owned'] and inspection['matches']


def test_genuine_action_ownership_mismatch_remains_fail_closed(auto):
    sid = 'S-1-5-21-123-456-789-1001'
    registration = build_registration(
        auto[0], 'auto-project', REPO, 120,
        installed_at='2026-09-11T10:00:00Z',
    )
    root = ET.fromstring(task_xml(registration, sid, start_at=NOW))
    root.find('.//{*}Description').text = 'unrelated task'
    root.find('.//{*}Command').text = r'C:\Windows\System32\cmd.exe'
    namespace = root.tag[1:].split('}', 1)[0]
    ET.SubElement(root.find('.//{*}Actions'), f'{{{namespace}}}Exec')
    inspection = inspect_task_xml(
        ET.tostring(root, encoding='unicode'), registration,
        expected_user_sid=sid,
    )
    fields = {item['field'] for item in inspection['ownership_mismatches']}
    assert {'description', 'executable', 'action_count', 'exec_action_count'} <= fields
    assert not inspection['owned'] and not inspection['matches']


def test_verification_mismatch_survives_rollback_and_task_is_removed(auto):
    mismatch = {
        'field': 'start_when_available', 'expected': 'True',
        'actual': 'False', 'ownership': False,
    }
    auto[2].verify_mismatches = [mismatch]
    with pytest.raises(SyncAutoError) as captured:
        install(auto)
    message = str(captured.value)
    assert 'start_when_available: expected=True actual=False' in message
    assert 'registration rolled back' in message
    registration_id = registration_id_for(auto[0], 'auto-project', REPO)
    assert not registration_path(registration_id, auto[1]).exists()
    assert auto[2].tasks == {}


def test_mismatch_diagnostics_do_not_leak_arguments_or_raw_xml(auto):
    sid = 'S-1-5-21-123-456-789-1001'
    registration = build_registration(
        auto[0], 'auto-project', REPO, 120,
        installed_at='2026-09-11T10:00:00Z',
    )
    root = ET.fromstring(task_xml(registration, sid, start_at=NOW))
    root.find('.//{*}Arguments').text = '--token SUPER_SECRET_FROM_TASK_XML'
    raw = ET.tostring(root, encoding='unicode')
    inspection = inspect_task_xml(raw, registration, expected_user_sid=sid)
    message = str(_task_verification_error(inspection))
    assert 'SUPER_SECRET_FROM_TASK_XML' not in message
    assert raw not in message
    assert 'arguments: expected=' in message
    assert 'sha256=' in message


def test_scheduler_query_failure_distinguishes_missing_from_existing_task(
    auto, tmp_path,
):
    registration = build_registration(
        auto[0], 'auto-project', REPO, 120,
        installed_at='2026-09-11T10:00:00Z',
    )
    windows = tmp_path / 'Windows'
    scheduler = WindowsTaskScheduler(
        executable=str(Path(subprocess.__file__).resolve()),
        task_store=windows / 'System32/Tasks',
    )
    scheduler._run = lambda arguments: subprocess.CompletedProcess(arguments, 1, b'', b'')
    assert scheduler.inspect(registration)['exists'] is False
    task_file = windows / 'System32/Tasks' / registration['task_name']
    task_file.parent.mkdir(parents=True)
    task_file.write_text('exists', encoding='utf-8')
    with pytest.raises(WindowsSchedulerError, match='existing task'):
        scheduler.inspect(registration)


def test_invalid_interval_and_missing_runtime_are_reported_safely(auto):
    with pytest.raises(SyncAutoError, match='60..3600'):
        install(auto, interval=59)
    runtime = auto[0].parent / 'missing runtime' / 'python.exe'
    runtime.parent.mkdir()
    runtime.write_bytes(b'MZ')
    with pytest.raises(SyncAutoError, match='matching pythonw.exe is unavailable'):
        install(auto, executable=str(runtime))


def test_scheduler_status_and_remove_errors_are_wrapped(auto):
    install(auto)
    auto[2].fail_inspect = True
    with pytest.raises(SyncAutoError, match='cannot inspect'):
        status_auto(auto[0], adapter=auto[2], store_root=auto[1])
    auto[2].fail_inspect = False
    auto[2].fail_delete = True
    with pytest.raises(SyncAutoError, match='cannot delete'):
        remove_auto(auto[0], adapter=auto[2], store_root=auto[1])


def test_internal_auto_run_is_independent_of_current_working_directory(
    tmp_path, monkeypatch, capsys,
):
    registration_id = 'REG-0123456789abcdef'
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr('project_system.cli.run_auto', lambda value: {
        'schema_version': 1, 'registration_id': value,
        'project_id': 'auto-project', 'repository': REPO,
        'status': 'no_pending', 'issue_number': None,
        'pack_id': None, 'cycles': 1,
    })
    main(['sync', 'auto', 'run', '--registration', registration_id])
    assert json.loads(capsys.readouterr().out)['registration_id'] == registration_id


def test_fatal_scheduled_cycle_records_diagnostics_and_fails(auto):
    installed = install(auto)
    with pytest.raises(SyncAutoError, match='requires human action'):
        run_auto(
            installed['registration']['registration_id'], store_root=auto[1],
            pickup=lambda root: {
                'schema_version': 1, 'repository': REPO,
                'status': 'blocked_malformed', 'reason': 'unsafe secret payload',
            }, clock=lambda: NOW,
        )
    state = json.loads(
        (auto[0] / '.generated/sync/auto/state.json').read_text(encoding='utf-8')
    )
    assert state['watcher_status'] == 'error'
    assert state['last_error_category'] == 'blocked_malformed'
    assert 'unsafe secret payload' not in json.dumps(state)


def test_registration_integrity_and_status_warning_for_cli_drift(auto):
    installed = install(auto)
    path = registration_path(installed['registration']['registration_id'], auto[1])
    registration = deepcopy(installed['registration'])
    registration['cli_version'] = '0.9.0'
    registration['registration_sha256'] = _registration_hash(registration)
    path.write_text(json.dumps(registration), encoding='utf-8')
    report = status_auto(auto[0], adapter=auto[2], store_root=auto[1])
    assert any('differs from runtime' in warning for warning in report['warnings'])


def test_registration_and_events_do_not_contain_secrets(auto):
    installed = install(auto)
    run_auto(
        installed['registration']['registration_id'], store_root=auto[1],
        pickup=lambda root: {
            'schema_version': 1, 'repository': REPO,
            'status': 'blocked_transport', 'reason': 'TOKEN=SUPER_SECRET Issue body',
        }, clock=lambda: NOW,
    )
    registration_raw = registration_path(
        installed['registration']['registration_id'], auto[1],
    ).read_bytes()
    event_raw = (auto[0] / '.generated/sync/auto/events.jsonl').read_bytes()
    assert b'SUPER_SECRET' not in registration_raw + event_raw
