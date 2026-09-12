"""Explicit execution context for safe external process invocation."""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import os
import subprocess


_BACKGROUND = ContextVar('project_system_background_processes', default=False)


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


def run_process(arguments, **kwargs):
    """Run an argument-list command, hiding Windows console children only in background."""
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
    return subprocess.run(arguments, shell=False, **kwargs)
