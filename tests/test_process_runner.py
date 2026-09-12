import subprocess

import pytest

from project_system import process_runner, sync_bindings, sync_finalization
from project_system import sync_planning, sync_pull, sync_verification


NO_WINDOW = 0x08000000


def _install_windows_spy(monkeypatch, implementation):
    monkeypatch.setattr(process_runner, '_is_windows', lambda: True)
    monkeypatch.setattr(
        process_runner.subprocess, 'CREATE_NO_WINDOW', NO_WINDOW, raising=False,
    )
    monkeypatch.setattr(process_runner.subprocess, 'run', implementation)


def test_background_flag_is_explicit_and_foreground_is_unchanged(monkeypatch):
    calls = []

    def run(arguments, **kwargs):
        calls.append((arguments, kwargs))
        return subprocess.CompletedProcess(arguments, 0, b'out', b'err')

    _install_windows_spy(monkeypatch, run)
    foreground = process_runner.run_process(
        ['git', 'status'], capture_output=True, check=False, timeout=12,
    )
    with process_runner.background_process_context():
        background = process_runner.run_process(
            ['git', 'status'], capture_output=True, check=False, timeout=12,
        )

    assert foreground.stdout == background.stdout == b'out'
    assert 'creationflags' not in calls[0][1]
    assert calls[1][1]['creationflags'] & NO_WINDOW
    assert all(call[1]['shell'] is False for call in calls)
    assert all(call[1]['capture_output'] is True for call in calls)
    assert all(call[1]['timeout'] == 12 for call in calls)
    assert process_runner.background_processes_enabled() is False


def test_windows_flag_is_guarded_on_posix(monkeypatch):
    calls = []
    monkeypatch.setattr(process_runner, '_is_windows', lambda: False)
    monkeypatch.setattr(
        process_runner.subprocess, 'run',
        lambda arguments, **kwargs: calls.append(kwargs)
        or subprocess.CompletedProcess(arguments, 0),
    )
    with process_runner.background_process_context():
        process_runner.run_process(['git', 'status'])
    assert 'creationflags' not in calls[0]


def test_process_runner_preserves_failure_timeout_capture_and_rejects_shell(
    monkeypatch,
):
    calls = []

    def fail(arguments, **kwargs):
        calls.append(kwargs)
        raise subprocess.TimeoutExpired(arguments, kwargs['timeout'])

    _install_windows_spy(monkeypatch, fail)
    with process_runner.background_process_context():
        with pytest.raises(subprocess.TimeoutExpired):
            process_runner.run_process(
                ['gh.exe', 'api'], capture_output=True, text=True,
                check=False, timeout=30,
            )
    assert calls[0]['creationflags'] & NO_WINDOW
    assert calls[0]['capture_output'] is True
    assert calls[0]['text'] is True
    assert calls[0]['check'] is False
    with pytest.raises(ValueError, match='shell=False'):
        process_runner.run_process(['cmd.exe'], shell=True)


def test_sync_pull_git_and_gh_follow_foreground_and_background_context(
    tmp_path, monkeypatch,
):
    calls = []

    def run(arguments, **kwargs):
        calls.append((list(arguments), dict(kwargs)))
        if arguments[0] == 'git':
            stdout = 'https://github.com/owner/repository.git\n'
        else:
            stdout = b'{}'
        return subprocess.CompletedProcess(arguments, 0, stdout, b'')

    _install_windows_spy(monkeypatch, run)
    monkeypatch.setattr(sync_pull, '_gh_executable', lambda: 'gh.exe')

    assert sync_pull.github_repository(tmp_path) == 'owner/repository'
    assert sync_pull.gh_get(tmp_path, 'repos/owner/repository/issues/1') == {}
    foreground = list(calls)
    calls.clear()
    with process_runner.background_process_context():
        assert sync_pull.github_repository(tmp_path) == 'owner/repository'
        assert sync_pull.gh_get(tmp_path, 'repos/owner/repository/issues/1') == {}
    background = list(calls)

    assert {call[0][0] for call in foreground} == {'git', 'gh.exe'}
    assert all('creationflags' not in kwargs for _, kwargs in foreground)
    assert {call[0][0] for call in background} == {'git', 'gh.exe'}
    assert all(kwargs['creationflags'] & NO_WINDOW for _, kwargs in background)
    assert all(kwargs['shell'] is False for _, kwargs in foreground + background)


@pytest.mark.parametrize('executable', ['git', 'gh.exe'])
def test_external_failure_remains_headless(executable, tmp_path, monkeypatch):
    calls = []

    def run(arguments, **kwargs):
        calls.append(kwargs)
        return subprocess.CompletedProcess(
            arguments, 17, b'', b'SECRET_CHILD_DETAIL',
        )

    _install_windows_spy(monkeypatch, run)
    monkeypatch.setattr(sync_pull, '_gh_executable', lambda: 'gh.exe')
    with process_runner.background_process_context():
        if executable == 'git':
            with pytest.raises(sync_pull.SyncPullError):
                sync_pull.github_repository(tmp_path)
        else:
            with pytest.raises(sync_pull.SyncPullError) as captured:
                sync_pull.gh_get(tmp_path, 'repos/owner/repository/issues/1')
            assert 'SECRET_CHILD_DETAIL' not in str(captured.value)
    assert calls[0]['creationflags'] & NO_WINDOW
    assert calls[0]['shell'] is False


def test_every_reachable_git_boundary_uses_central_background_policy(
    tmp_path, monkeypatch,
):
    calls = []

    def run(arguments, **kwargs):
        calls.append((list(arguments), dict(kwargs)))
        text = kwargs.get('text') is True
        command = list(arguments[1:])
        if command == ['rev-parse', 'HEAD']:
            stdout = 'a' * 40 + '\n'
        elif text:
            stdout = str(tmp_path) + '\n'
        else:
            stdout = b''
        return subprocess.CompletedProcess(
            arguments, 0, stdout, '' if text else b'',
        )

    _install_windows_spy(monkeypatch, run)
    pack_path = tmp_path / 'outside-pack.yaml'
    with process_runner.background_process_context():
        assert sync_planning._git_head(tmp_path) == 'a' * 40
        assert sync_planning._git_status_paths(tmp_path) == []
        assert sync_planning._ignored_untracked_baseline(tmp_path, pack_path) == []
        assert sync_bindings._git(tmp_path, ['rev-parse', '--git-dir']) == str(tmp_path)
        assert sync_verification._git(tmp_path, ['status']).returncode == 0
        assert sync_finalization._run_git(tmp_path, ['status']).returncode == 0

    assert len(calls) == 6
    assert all(call[0][0] == 'git' for call in calls)
    assert all(call[1]['creationflags'] & NO_WINDOW for call in calls)
    assert all(call[1]['shell'] is False for call in calls)
