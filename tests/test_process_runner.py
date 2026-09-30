import subprocess
import sys

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


def test_bounded_capture_preserves_completed_process_text_contract():
    completed = process_runner.run_process(
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                "sys.stdout.write('hello'); "
                "sys.stderr.write('warning')"
            ),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=5,
        max_capture_bytes=64,
    )

    assert completed.returncode == 0
    assert completed.stdout == "hello"
    assert completed.stderr == "warning"


@pytest.mark.parametrize(
    ("stream", "program"),
    [
        (
            "stdout",
            "import sys; sys.stdout.buffer.write(b'x' * 65)",
        ),
        (
            "stderr",
            "import sys; sys.stderr.buffer.write(b'x' * 65)",
        ),
    ],
)
def test_bounded_capture_fails_closed_when_stream_exceeds_limit(
    stream,
    program,
):
    with pytest.raises(
        process_runner.ProcessOutputLimitExceeded,
        match=stream,
    ):
        process_runner.run_process(
            [sys.executable, "-c", program],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=5,
            max_capture_bytes=64,
        )


def test_bounded_capture_limit_is_measured_in_encoded_bytes():
    completed = process_runner.run_process(
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                "sys.stdout.buffer.write(bytes.fromhex('e282ac') * 2)"
            ),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=5,
        max_capture_bytes=6,
    )

    assert completed.stdout == "\u20ac\u20ac"

    with pytest.raises(
        process_runner.ProcessOutputLimitExceeded,
        match="stdout",
    ):
        process_runner.run_process(
            [
                sys.executable,
                "-c",
                (
                    "import sys; "
                    "sys.stdout.buffer.write(bytes.fromhex('e282ac') * 3)"
                ),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=5,
            max_capture_bytes=8,
        )


@pytest.mark.parametrize(
    "value",
    [0, -1, True, "64"],
)
def test_bounded_capture_rejects_invalid_limits(value):
    with pytest.raises(ValueError, match="max_capture_bytes"):
        process_runner.run_process(
            [sys.executable, "-c", "pass"],
            capture_output=True,
            check=False,
            max_capture_bytes=value,
        )


def test_bounded_capture_requires_capture_output():
    with pytest.raises(ValueError, match="capture_output=True"):
        process_runner.run_process(
            [sys.executable, "-c", "pass"],
            check=False,
            max_capture_bytes=64,
        )


def test_bounded_capture_timeout_survives_partial_multibyte_output():
    with pytest.raises(subprocess.TimeoutExpired) as captured:
        process_runner.run_process(
            [
                sys.executable,
                "-c",
                (
                    "import sys, time; "
                    "sys.stdout.buffer.write(bytes([0xe2])); "
                    "sys.stdout.buffer.flush(); "
                    "time.sleep(5)"
                ),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=0.1,
            max_capture_bytes=64,
        )

    assert captured.value.timeout == 0.1
    assert captured.value.output == b"\xe2"
    assert isinstance(captured.value.output, bytes)


def test_bounded_capture_reader_thread_join_is_time_bounded(
    monkeypatch,
):
    joins = []

    class FakeProcess:
        stdout = object()
        stderr = object()
        returncode = 0

        def wait(self, timeout=None):
            return 0

        def kill(self):
            self.returncode = -9

    class FakeThread:
        def __init__(self, *, target, args, daemon):
            self.target = target
            self.args = args
            self.daemon = daemon

        def start(self):
            pass

        def join(self, timeout=None):
            joins.append(timeout)
            if timeout is None:
                raise AssertionError(
                    "reader thread join must be time-bounded"
                )

        def is_alive(self):
            return False

    monkeypatch.setattr(
        process_runner.subprocess,
        "Popen",
        lambda *args, **kwargs: FakeProcess(),
    )
    monkeypatch.setattr(
        process_runner.threading,
        "Thread",
        FakeThread,
    )

    completed = process_runner.run_process(
        ["fake"],
        capture_output=True,
        check=False,
        max_capture_bytes=64,
    )

    assert completed.returncode == 0
    assert len(joins) == 2
    assert all(
        type(timeout) in {int, float} and timeout > 0
        for timeout in joins
    )


def test_bounded_capture_fails_closed_if_reader_does_not_drain(
    monkeypatch,
):
    class FakeProcess:
        stdout = object()
        stderr = object()
        returncode = 0

        def wait(self, timeout=None):
            return 0

        def kill(self):
            self.returncode = -9

    class FakeThread:
        def __init__(self, *, target, args, daemon):
            self.target = target
            self.args = args
            self.daemon = daemon

        def start(self):
            pass

        def join(self, timeout=None):
            pass

        def is_alive(self):
            return True

    monkeypatch.setattr(
        process_runner.subprocess,
        "Popen",
        lambda *args, **kwargs: FakeProcess(),
    )
    monkeypatch.setattr(
        process_runner.threading,
        "Thread",
        FakeThread,
    )

    with pytest.raises(RuntimeError, match="pipe drain"):
        process_runner.run_process(
            ["fake"],
            capture_output=True,
            check=False,
            max_capture_bytes=64,
        )
