"""Explicit execution context for safe external process invocation."""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import locale
import os
import subprocess
import threading


_BACKGROUND = ContextVar('project_system_background_processes', default=False)

_PIPE_DRAIN_GRACE_SECONDS = 0.5


class ProcessOutputLimitExceeded(RuntimeError):
    """Raised when one captured child-process stream exceeds its byte budget."""

    def __init__(self, stream, max_capture_bytes):
        self.stream = stream
        self.max_capture_bytes = max_capture_bytes
        super().__init__(
            f'{stream} exceeded max_capture_bytes={max_capture_bytes}'
        )


class ProcessPipeDrainError(RuntimeError):
    """Raised when captured process pipes do not drain within the grace period."""

    def __init__(self):
        super().__init__('bounded process pipe drain did not complete')


def background_processes_enabled():
    return _BACKGROUND.get()


@contextmanager
def background_process_context():
    token = _BACKGROUND.set(True)
    try:
        yield
    finally:
        _BACKGROUND.reset(token)


def background_processes(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        with background_process_context():
            return function(*args, **kwargs)
    return wrapped


def _is_windows():
    return os.name == 'nt'


def _decoded_capture(data, *, text_mode, encoding, errors):
    if not text_mode:
        return data

    codec = encoding or locale.getpreferredencoding(False)
    decoded = data.decode(codec, errors or 'strict')

    # Match subprocess text-mode universal-newline behavior.
    return decoded.replace('\r\n', '\n').replace('\r', '\n')


def _run_bounded_capture(arguments, max_capture_bytes, kwargs):
    capture_output = kwargs.pop('capture_output', False)
    if capture_output is not True:
        raise ValueError(
            'max_capture_bytes requires capture_output=True'
        )

    if 'stdout' in kwargs or 'stderr' in kwargs:
        raise ValueError(
            'stdout and stderr may not be supplied with capture_output=True'
        )

    if 'input' in kwargs or 'stdin' in kwargs:
        raise ValueError(
            'bounded capture does not support input or stdin'
        )

    check = kwargs.pop('check', False)
    timeout = kwargs.pop('timeout', None)

    text = kwargs.pop('text', False)
    encoding = kwargs.pop('encoding', None)
    errors = kwargs.pop('errors', None)
    universal_newlines = kwargs.pop('universal_newlines', False)

    text_mode = bool(
        text
        or universal_newlines
        or encoding is not None
        or errors is not None
    )

    process = subprocess.Popen(
        arguments,
        shell=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=False,
        **kwargs,
    )

    buffers = {
        'stdout': bytearray(),
        'stderr': bytearray(),
    }
    overflows = []
    reader_errors = []

    def read_stream(name, stream):
        try:
            while True:
                remaining = max_capture_bytes - len(buffers[name])
                chunk = stream.read(min(65536, remaining + 1))
                if not chunk:
                    return

                if len(chunk) > remaining:
                    overflows.append(name)
                    try:
                        process.kill()
                    except OSError:
                        pass
                    return

                buffers[name].extend(chunk)
        except BaseException as exc:
            reader_errors.append(exc)
            try:
                process.kill()
            except OSError:
                pass
        finally:
            stream.close()

    stdout_thread = threading.Thread(
        target=read_stream,
        args=('stdout', process.stdout),
        daemon=True,
    )
    stderr_thread = threading.Thread(
        target=read_stream,
        args=('stderr', process.stderr),
        daemon=True,
    )
    stdout_thread.start()
    stderr_thread.start()

    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()

        stdout_thread.join(timeout=_PIPE_DRAIN_GRACE_SECONDS)
        stderr_thread.join(timeout=_PIPE_DRAIN_GRACE_SECONDS)

        raise subprocess.TimeoutExpired(
            arguments,
            timeout,
            output=bytes(buffers['stdout']),
            stderr=bytes(buffers['stderr']),
        ) from None

    stdout_thread.join(timeout=_PIPE_DRAIN_GRACE_SECONDS)
    stderr_thread.join(timeout=_PIPE_DRAIN_GRACE_SECONDS)

    if reader_errors:
        raise reader_errors[0]

    if overflows:
        raise ProcessOutputLimitExceeded(
            overflows[0],
            max_capture_bytes,
        )

    if stdout_thread.is_alive() or stderr_thread.is_alive():
        raise ProcessPipeDrainError()

    stdout = _decoded_capture(
        bytes(buffers['stdout']),
        text_mode=text_mode,
        encoding=encoding,
        errors=errors,
    )
    stderr = _decoded_capture(
        bytes(buffers['stderr']),
        text_mode=text_mode,
        encoding=encoding,
        errors=errors,
    )

    completed = subprocess.CompletedProcess(
        arguments,
        process.returncode,
        stdout,
        stderr,
    )

    if check and completed.returncode != 0:
        raise subprocess.CalledProcessError(
            completed.returncode,
            arguments,
            output=stdout,
            stderr=stderr,
        )

    return completed


def run_process(arguments, **kwargs):
    """Run an argument-list command with explicit safe process policy."""
    shell = kwargs.pop('shell', False)
    if shell is not False:
        raise ValueError('Project System external processes require shell=False')

    creationflags = kwargs.pop('creationflags', 0)
    if type(creationflags) is not int or creationflags < 0:
        raise ValueError('creationflags must be a non-negative integer')

    if _is_windows() and background_processes_enabled():
        creationflags |= subprocess.CREATE_NO_WINDOW
    if creationflags:
        kwargs['creationflags'] = creationflags

    max_capture_bytes = kwargs.pop('max_capture_bytes', None)
    if max_capture_bytes is None:
        return subprocess.run(arguments, shell=False, **kwargs)

    if (
        type(max_capture_bytes) is not int
        or max_capture_bytes <= 0
    ):
        raise ValueError(
            'max_capture_bytes must be a positive integer'
        )

    return _run_bounded_capture(
        arguments,
        max_capture_bytes,
        kwargs,
    )
