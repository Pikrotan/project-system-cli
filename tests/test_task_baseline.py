"""Stage 12C: a task starts at effective dirty state, not merely HEAD."""

import json
from hashlib import sha256
from copy import deepcopy
from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator

import project_system.task_baseline as module
from project_system.task_baseline import (
    MAX_BASELINE_BYTES, TaskBaselineError, capture_task_state, load_task_baseline,
    serialize_task_baseline, task_delta, validate_task_baseline,
)
from project_system.utils import distribution_root

from project_system.init_project import init_project
from project_system.objects import create_object
from project_system.tasking import task
from test_sync_finalize import _commit_project, _git


def test_task_creates_exact_persisted_baseline_bindings(tmp_path):
    root = init_project('Demo', tmp_path / 'demo')
    _, target = create_object(root, 'feature', 'Target', 'product', 'owner')
    head = _commit_project(root)
    (root / 'owner-review.txt').write_bytes(b'pre-existing unrelated state\n')
    output, _ = task(root, target, budget='small')
    baseline = json.loads((output / 'task-baseline.json').read_bytes())
    assert baseline['base_commit'] == head
    assert baseline['task_spec_sha256'] == sha256((output / 'task-spec.json').read_bytes()).hexdigest()
    assert baseline['task_obligations_sha256'] == sha256((output / 'task-obligations.json').read_bytes()).hexdigest()
    assert baseline['entries'] == [{'path': 'owner-review.txt', 'state': 'file',
                                   'sha256': sha256(b'pre-existing unrelated state\n').hexdigest(),
                                   'bytes': 29}]


@pytest.fixture(scope='module')
def prepared(tmp_path_factory):
    root = init_project('Demo', tmp_path_factory.mktemp('baseline-base') / 'demo')
    _, target = create_object(root, 'feature', 'Target', 'product', 'owner')
    (root / 'tracked.txt').write_bytes(b'canonical\n')
    head = _commit_project(root)
    (root / 'tracked.txt').write_bytes(b'dirty\n')
    (root / 'untracked.txt').write_bytes(b'pre-existing\n')
    output, _ = task(root, target, budget='small')
    return root, target, head, output


@pytest.fixture
def project(prepared, tmp_path):
    root = tmp_path / 'relocated'
    shutil.copytree(prepared[0], root)
    return root, prepared[1], prepared[2], root / prepared[3].relative_to(prepared[0])


@pytest.fixture
def baseline(prepared):
    return load_task_baseline(prepared[3] / 'task-baseline.json')


def test_dirty_baseline_sorted_safe_and_generated_excluded(project, baseline):
    root, _, head, _ = project
    assert [entry['path'] for entry in baseline['entries']] == ['tracked.txt', 'untracked.txt']
    assert capture_task_state(root, head) == baseline['entries']
    assert all(not item['path'].startswith('.generated/') for item in baseline['entries'])


def test_baseline_serialization_schema_and_portability(project, baseline):
    root, target, _, output = project
    before = (output / 'task-baseline.json').read_bytes()
    task(root, target, budget='small')
    assert (output / 'task-baseline.json').read_bytes() == before == serialize_task_baseline(baseline).encode('utf-8')
    assert before.endswith(b'\n') and not before.endswith(b'\n\n') and b'\r' not in before
    assert str(root).encode() not in before
    schema = json.loads((distribution_root() / 'schemas/task-baseline.schema.json').read_bytes())
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(baseline)


@pytest.mark.parametrize('fault', ['dirty', 'untracked', 'head', 'mode', 'spec', 'obligations'])
def test_immutable_baseline_is_not_implicitly_reset(project, fault):
    root, target, _, output = project
    names = ['task-spec.json', 'task-obligations.json', 'task-baseline.json']
    if fault == 'dirty':
        (root / 'tracked.txt').write_bytes(b'further edit')
    elif fault == 'untracked':
        (root / 'untracked.txt').unlink()
    elif fault == 'head':
        _git(root, 'commit', '--allow-empty', '-qm', 'new head')
    elif fault in {'spec', 'obligations'}:
        (output / ('task-spec.json' if fault == 'spec' else 'task-obligations.json')).write_bytes(b'{}')
    before = {name: (output / name).read_bytes() for name in names}
    with pytest.raises(RuntimeError):
        task(root, target, mode='review' if fault == 'mode' else 'implement', budget='small')
    assert {name: (output / name).read_bytes() for name in names} == before


@pytest.mark.parametrize('raw', [b'{', b'[]', b'{"x":1,"x":2}', b'{"x":NaN}',
                                b'{"x":Infinity}', b'\xff', b' ' * (MAX_BASELINE_BYTES + 1)],
                         ids=['json', 'array', 'duplicate', 'nan', 'infinity', 'utf8', 'oversize'])
def test_baseline_bounded_loader_rejects_invalid(tmp_path, raw):
    path = tmp_path / 'baseline.json'
    path.write_bytes(raw)
    with pytest.raises(TaskBaselineError):
        load_task_baseline(path)


@pytest.mark.parametrize('field,value', [('schema_version', True), ('base_commit', 'x' * 40),
    ('base_commit', 'a' * 40 + '\n'), ('task_spec_sha256', 'A' * 64),
    ('task_spec_sha256', 'a' * 64 + '\n'), ('task_obligations_sha256', None), ('extra', 1)])
def test_baseline_contract_rejects_malformed_root(baseline, field, value):
    baseline[field] = value
    with pytest.raises(TaskBaselineError):
        validate_task_baseline(baseline)


@pytest.mark.parametrize('fault', ['unsorted', 'duplicate', 'unknown', 'null-file', 'absent-hash', 'negative', 'bool',
    '../escape', '/absolute', 'C:/absolute', '.git/config', '.generated/data', 'a/../b', 'a\\b', 'a//b'])
def test_baseline_entries_fail_closed(baseline, fault):
    entry = baseline['entries'][0]
    if fault == 'unsorted':
        baseline['entries'].reverse()
    elif fault == 'duplicate':
        baseline['entries'].insert(0, deepcopy(entry))
    elif fault == 'unknown':
        entry['extra'] = True
    elif fault == 'null-file':
        entry['sha256'] = None
    elif fault == 'absent-hash':
        entry['state'] = 'absent'
    elif fault == 'negative':
        entry['bytes'] = -1
    elif fault == 'bool':
        entry['bytes'] = True
    else:
        baseline['entries'] = [entry | {'path': fault}]
    with pytest.raises(TaskBaselineError):
        validate_task_baseline(baseline)


@pytest.mark.parametrize('change,expected', [
    ('unchanged', []), ('dirty-more', ['tracked.txt']), ('dirty-revert', ['tracked.txt']),
    ('untracked-edit', ['untracked.txt']), ('untracked-delete', ['untracked.txt']),
    ('new', ['new.txt']), ('delete', ['tracked.txt']), ('rename', ['renamed.txt', 'tracked.txt']),
    ('clean-edit', ['README.md']), ('stage-add', ['added.txt']), ('stage-delete', ['README.md']),
])
def test_delta_since_effective_task_start_not_head(project, baseline, change, expected):
    root, _, head, _ = project
    if change == 'dirty-more': (root / 'tracked.txt').write_bytes(b'more')
    elif change == 'dirty-revert': (root / 'tracked.txt').write_bytes(b'canonical\n')
    elif change == 'untracked-edit': (root / 'untracked.txt').write_bytes(b'edited')
    elif change == 'untracked-delete': (root / 'untracked.txt').unlink()
    elif change == 'new': (root / 'new.txt').write_bytes(b'new')
    elif change == 'delete': (root / 'tracked.txt').unlink()
    elif change == 'rename': (root / 'tracked.txt').rename(root / 'renamed.txt')
    elif change == 'clean-edit': (root / 'README.md').write_bytes(b'changed')
    elif change == 'stage-add':
        (root / 'added.txt').write_bytes(b'added')
        _git(root, 'add', 'added.txt')
    elif change == 'stage-delete': _git(root, 'rm', 'README.md')
    current = capture_task_state(root, head)
    assert task_delta(baseline['entries'], current) == expected


def test_ignored_build_trees_not_enumerated(project):
    root, _, head, _ = project
    (root / '.gitignore').write_text((root / '.gitignore').read_text() + '\nignored-build/\n')
    (root / 'ignored-build').mkdir()
    (root / 'ignored-build/output.bin').write_bytes(b'ignored')
    assert 'ignored-build/output.bin' not in {item['path'] for item in capture_task_state(root, head)}


def test_symlink_input_and_generated_parent_fail_closed(project, tmp_path):
    root, target, head, output = project
    outside = tmp_path / 'outside'
    outside.write_bytes(b'outside')
    try:
        (root / 'linked.txt').symlink_to(outside)
    except OSError:
        pytest.skip('symlink permission unavailable')
    with pytest.raises(TaskBaselineError, match='symlink|reparse'):
        capture_task_state(root, head)
    (root / 'linked.txt').unlink()
    shutil.rmtree(output)
    output.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(TaskBaselineError, match='symlink|reparse'):
        task(root, target, budget='small')


def test_bounded_shell_false_git_capture(project, monkeypatch):
    original = module.run_process
    seen = []
    def probe(args, **options):
        seen.append((args, options))
        return original(args, **options)
    monkeypatch.setattr(module, 'run_process', probe)
    capture_task_state(project[0], project[2])
    assert any('--no-renames' in args for args, _ in seen)
    assert any('--exclude-standard' in args for args, _ in seen)
    for _, options in seen:
        assert options['shell'] is False and options['timeout'] == 30
        assert options['max_capture_bytes'] == module.GIT_MAX_CAPTURE_BYTES


@pytest.mark.parametrize('failure', ['exception', 'exit', 'malformed', 'utf8'])
def test_git_capture_failure_is_controlled(project, monkeypatch, failure):
    def broken(args, **options):
        if failure == 'exception': raise TimeoutError()
        if '--show-toplevel' in args: return SimpleNamespace(returncode=0, stdout=str(project[0]).encode() + b'\n')
        return SimpleNamespace(returncode=1 if failure == 'exit' else 0,
                               stdout=b'no-nul' if failure == 'malformed' else b'\xff\x00')
    monkeypatch.setattr(module, 'run_process', broken)
    with pytest.raises(TaskBaselineError):
        capture_task_state(project[0], project[2])


def test_capture_precedes_context_artifacts(project, monkeypatch):
    import project_system.tasking as tasking_module
    seen = []
    capture, context = tasking_module.capture_task_state, tasking_module.build_context
    def recording_capture(*args):
        seen.append('capture')
        return capture(*args)
    def recording_context(*args, **kwargs):
        seen.append('context')
        return context(*args, **kwargs)
    monkeypatch.setattr(tasking_module, 'capture_task_state', recording_capture)
    monkeypatch.setattr(tasking_module, 'build_context', recording_context)
    task(project[0], project[1], budget='small')
    assert seen[:2] == ['capture', 'context']


def test_reparse_input_fails_closed_without_symlink_privilege(project, monkeypatch):
    original = module.os.lstat
    selected = project[0] / 'tracked.txt'
    def reparse(path, *args, **kwargs):
        info = original(path, *args, **kwargs)
        if Path(path) == selected:
            return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400)
        return info
    monkeypatch.setattr(module.os, 'lstat', reparse)
    with pytest.raises(TaskBaselineError, match='reparse'):
        capture_task_state(project[0], project[2])


def test_parent_escape_safe_write_and_nonregular_fail_closed(project):
    root = project[0]
    with pytest.raises(TaskBaselineError): module.safe_task_path(root, '../outside')
    with pytest.raises(TaskBaselineError): module.write_task_artifact(root, root / 'docs/out.md', 'forbidden')
    (root / 'not-file').mkdir()
    with pytest.raises(TaskBaselineError, match='regular file'):
        module.snapshot_paths(root, ['not-file'])


def test_staged_change_with_worktree_equal_to_head_still_has_baseline_entry(project):
    root, _, head, _ = project
    _git(root, 'add', 'tracked.txt')  # stage the pre-existing dirty B state
    (root / 'tracked.txt').write_bytes(b'canonical\n')  # effective A, index B, HEAD A
    state = {entry['path']: entry for entry in capture_task_state(root, head)}
    assert state['tracked.txt']['sha256'] == sha256(b'canonical\n').hexdigest()
