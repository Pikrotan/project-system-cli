"""Stage 13A: opaque bytes become provenance, never product truth."""

import json
import os
from pathlib import Path
from hashlib import sha256
from types import SimpleNamespace

import pytest

import project_system.source_capture as capture_module
import project_system.source_layer as layer_module
from project_system.source_capture import capture_source
from project_system.source_layer import SourceError, source_id, capture_id, validate_source

from project_system.cli import main
from project_system.init_project import init_project
from project_system.validation import validate


def test_capture_cli_defaults_to_exact_reference_receipt(tmp_path, monkeypatch):
    root = init_project('Demo', tmp_path / 'project')
    source = tmp_path / 'private-original-export.html'
    raw = b'<html>opaque customer source\x00</html>'
    source.write_bytes(raw)
    monkeypatch.chdir(root)
    main(['source', 'capture', str(source), '--key', 'client-chat',
          '--provider', 'telegram', '--kind', 'conversation'])
    definitions = list((root / 'sources/definitions').glob('*.json'))
    captures = list((root / 'sources/captures').glob('*.json'))
    assert len(definitions) == len(captures) == 1
    receipt = json.loads(captures[0].read_bytes())
    assert receipt['retention'] == 'reference' and receipt['snapshot'] is None
    assert receipt['content_sha256'] == sha256(raw).hexdigest()
    assert receipt['bytes'] == len(raw)
    assert not list((root / 'sources/snapshots').rglob('payload.bin'))
    assert source.name.encode() not in definitions[0].read_bytes() + captures[0].read_bytes()


@pytest.fixture
def project(tmp_path):
    root = init_project('Demo', tmp_path / 'project')
    path = tmp_path / 'private.bin'
    path.write_bytes(b'opaque bytes\x00\xff\r\n')
    return root, path


def capture(project, **kwargs):
    return capture_source(*project, **({'key': 'client-brief', 'provider': 'generic',
                                        'kind': 'document'} | kwargs))


def receipts(root, report):
    definition = root / f'sources/definitions/{report["source_id"]}.json'
    receipt = root / f'sources/captures/{report["capture_id"]}.json'
    return definition, receipt


def tree_bytes(root):
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob('*') if p.is_file()}


def test_independent_id_formula_and_relocation(project, tmp_path):
    root, path = project
    report = capture(project)
    source, receipt = receipts(root, report)
    source_doc, capture_doc = map(lambda p: json.loads(p.read_bytes()), (source, receipt))
    for doc, fields, prefix, field in (
            (source_doc, ['project_id', 'key', 'provider', 'kind'], 'SRC-', 'source_id'),
            (capture_doc, ['project_id', 'source_id', 'content_sha256', 'bytes', 'media_type', 'retention'], 'CAP-', 'capture_id')):
        payload = json.dumps({key: doc[key] for key in fields}, ensure_ascii=False,
                             sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
        assert doc[field] == prefix + sha256(payload).hexdigest()[:32]
    other = init_project('Demo', tmp_path / 'other-project')
    other_input = tmp_path / 'unrelated-filename'
    other_input.write_bytes(path.read_bytes())
    other_report = capture((other, other_input))
    assert report == other_report
    assert [p.read_bytes() for p in receipts(other, other_report)] == [source.read_bytes(), receipt.read_bytes()]
    assert b'\r' not in source.read_bytes() and source.read_bytes().endswith(b'\n')


@pytest.mark.parametrize('field,value', [('project_id', 'other'), ('key', 'other'),
                                        ('provider', 'email'), ('kind', 'conversation')])
def test_source_semantic_identity_changes_id(project, field, value):
    report = capture(project)
    doc = json.loads(receipts(project[0], report)[0].read_bytes())
    changed = dict(doc, **{field: value})
    assert source_id(changed) != doc['source_id']
    with pytest.raises(SourceError, match='source_id'):
        validate_source(changed)


@pytest.mark.parametrize('retention', ['reference', 'repository_snapshot'])
def test_idempotence_preserves_existing_bytes_and_mtimes(project, retention):
    root, _ = project
    first = capture(project, retention=retention)
    paths = [p for p in (root / 'sources').rglob('*') if p.is_file()]
    before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in paths}
    second = capture(project, retention=retention)
    assert first['status'] == 'created' and second['status'] == 'existing'
    assert first['source_id'] == second['source_id'] and first['capture_id'] == second['capture_id']
    assert before == {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in paths}
    assert len(list((root / 'sources/captures').glob('*.json'))) == 1
    assert not list((root / 'sources').rglob('*.tmp'))


def test_new_bytes_add_capture_without_changing_source(project):
    first = capture(project)
    source = receipts(project[0], first)[0]
    before = source.read_bytes()
    project[1].write_bytes(b'changed bytes')
    second = capture(project)
    assert first['source_id'] == second['source_id']
    assert first['capture_id'] != second['capture_id']
    assert source.read_bytes() == before
    assert len(list((project[0] / 'sources/captures').glob('*.json'))) == 2


@pytest.mark.parametrize('change', [{'key': 'another-source'}, {'media_type': 'text/plain'},
                                   {'retention': 'repository_snapshot'}])
def test_same_bytes_different_capture_identity(project, change):
    first = capture(project)
    second = capture(project, **change)
    assert first['capture_id'] != second['capture_id']


def test_key_cannot_be_redefined(project):
    capture(project)
    before = tree_bytes(project[0])
    with pytest.raises(SourceError, match='key'):
        capture(project, provider='email')
    assert tree_bytes(project[0]) == before


def test_explicit_snapshot_exact_bytes_and_no_input_identifiers(project, monkeypatch, capsys):
    root, source = project
    monkeypatch.chdir(root)
    main(['source', 'capture', str(source), '--key', 'brief', '--provider', 'generic',
          '--kind', 'document', '--media-type', 'application/pdf', '--retention', 'repository-snapshot'])
    report = json.loads(capsys.readouterr().out)
    definition, path = receipts(root, report)
    doc = json.loads(path.read_bytes())
    expected = f'sources/snapshots/{report["capture_id"]}/payload.bin'
    assert doc['snapshot'] == {'path': expected, 'sha256': sha256(source.read_bytes()).hexdigest(),
                               'bytes': source.stat().st_size}
    assert (root / expected).read_bytes() == source.read_bytes()
    assert set(json.loads(definition.read_bytes())) == {
        'schema_version', 'profile', 'project_id', 'source_id', 'key', 'provider', 'kind'}
    assert set(doc) == {'schema_version', 'profile', 'project_id', 'capture_id', 'source_id',
                        'content_sha256', 'bytes', 'media_type', 'retention', 'snapshot'}
    assert source.name.encode() not in path.read_bytes() + definition.read_bytes()
    assert str(source.parent).encode() not in path.read_bytes() + definition.read_bytes()
    assert not [issue for issue in validate(root) if issue[0] in {'ERROR', 'BLOCKING'}]


def test_empty_file_is_a_valid_exact_capture(project):
    project[1].write_bytes(b'')
    report = capture(project, retention='repository_snapshot')
    doc = json.loads(receipts(project[0], report)[1].read_bytes())
    assert doc['bytes'] == 0 and doc['content_sha256'] == sha256(b'').hexdigest()
    assert (project[0] / doc['snapshot']['path']).read_bytes() == b''


@pytest.mark.parametrize('kwargs', [
    {'key': '../escape'}, {'key': 'Upper'}, {'provider': 'bad/name'}, {'provider': 'Generic'},
    {'kind': 'script'}, {'media_type': 'TEXT/PLAIN'}, {'media_type': 'text/plain; charset=utf-8'},
    {'media_type': 'text/plain\n'}, {'retention': 'auto'}, {'key': 'x\n'},
])
def test_invalid_capture_metadata_makes_no_writes(project, kwargs):
    before = tree_bytes(project[0])
    with pytest.raises(SourceError):
        capture(project, **kwargs)
    assert tree_bytes(project[0]) == before


@pytest.mark.parametrize('case', ['directory', 'missing', 'oversize', 'reparse'])
@pytest.mark.parametrize('retention', ['reference', 'repository_snapshot'])
def test_unsafe_input_fails_without_receipts_or_temporary_bytes(project, monkeypatch, case, retention):
    root, path = project
    if case == 'directory':
        path.unlink()
        path.mkdir()
    elif case == 'missing':
        path.unlink()
    elif case == 'oversize':
        monkeypatch.setattr(layer_module, 'MAX_SOURCE_BYTES', 2)
    else:
        original = layer_module.os.lstat
        def reparse(target, *args, **kwargs):
            value = original(target, *args, **kwargs)
            if Path(target) == path:
                return SimpleNamespace(st_mode=value.st_mode, st_file_attributes=0x400)
            return value
        monkeypatch.setattr(layer_module.os, 'lstat', reparse)
    with pytest.raises(SourceError):
        capture(project, retention=retention)
    assert not [p for p in (root / 'sources').rglob('*') if p.is_file() and p.name != '.gitkeep']


@pytest.mark.parametrize('parent_link', [False, True])
def test_input_symlink_rejected(project, tmp_path, parent_link):
    root, source = project
    link = tmp_path / 'linked'
    try:
        link.symlink_to(source.parent if parent_link else source, target_is_directory=parent_link)
    except OSError:
        pytest.skip('symlink creation unavailable')
    with pytest.raises(SourceError, match='symlink|reparse'):
        capture((root, link / source.name if parent_link else link))


@pytest.mark.parametrize('retention', ['reference', 'repository_snapshot'])
def test_changed_input_during_real_stream_fails_and_cleans_temporary_bytes(project, monkeypatch, retention):
    root, source = project
    original = Path.open
    changed = False
    class MutatingReader:
        def __init__(self, wrapped): self.wrapped = wrapped
        def __enter__(self): return self
        def __exit__(self, *args): self.wrapped.close()
        def fileno(self): return self.wrapped.fileno()
        def read(self, size):
            nonlocal changed
            data = self.wrapped.read(size)
            if not changed:
                changed = True
                with original(source, 'ab') as output:
                    output.write(b'concurrent edit')
            return data
    def opening(path, *args, **kwargs):
        stream = original(path, *args, **kwargs)
        return MutatingReader(stream) if path == source and args == ('rb',) else stream
    monkeypatch.setattr(Path, 'open', opening)
    with pytest.raises(SourceError, match='changed'):
        capture(project, retention=retention)
    assert changed
    assert not [p for p in (root / 'sources').rglob('*') if p.is_file() and p.name != '.gitkeep']


def test_snapshot_reads_input_once_in_bounded_chunks(project, monkeypatch):
    source = project[1]
    source.write_bytes(b'0' * (layer_module.STREAM_CHUNK_BYTES * 2 + 3))
    original = Path.open
    reads, opens = [], []
    class Reader:
        def __init__(self, stream): self.stream = stream
        def __enter__(self): return self
        def __exit__(self, *args): self.stream.close()
        def fileno(self): return self.stream.fileno()
        def read(self, size):
            reads.append(size)
            return self.stream.read(size)
    def opening(path, *args, **kwargs):
        stream = original(path, *args, **kwargs)
        if path == source and args == ('rb',):
            opens.append(path)
            return Reader(stream)
        return stream
    monkeypatch.setattr(Path, 'open', opening)
    capture(project, retention='repository_snapshot')
    assert len(opens) == 1 and len(reads) == 4
    assert set(reads) == {layer_module.STREAM_CHUNK_BYTES}


def test_failed_publication_cleans_only_new_artifacts(project, monkeypatch):
    root, _ = project
    first = capture(project)
    before = tree_bytes(root)
    original = capture_module._publish_receipt
    def fail_capture(root, relative, *args):
        if relative.startswith('sources/captures/'):
            raise OSError('simulated write failure')
        return original(root, relative, *args)
    monkeypatch.setattr(capture_module, '_publish_receipt', fail_capture)
    with pytest.raises(SourceError):
        capture(project, retention='repository_snapshot')
    assert tree_bytes(root) == before
    assert receipts(root, first)[1].is_file()


def test_no_clobber_publication_rejects_racing_conflict(project, monkeypatch):
    root, _ = project
    original = capture_module.os.link
    conflicts = []
    def race(temporary, destination):
        if destination.parent.name == 'captures':
            destination.write_bytes(b'{"conflict":true}\n')
            conflicts.append(destination)
        return original(temporary, destination)
    monkeypatch.setattr(capture_module.os, 'link', race)
    with pytest.raises(SourceError):
        capture(project, retention='repository_snapshot')
    assert conflicts[0].read_bytes() == b'{"conflict":true}\n'
    assert not list((root / 'sources').rglob('*.tmp'))
    assert not list((root / 'sources').rglob('payload.bin'))


@pytest.mark.parametrize('which', ['definition', 'capture'])
def test_contradictory_existing_receipt_is_not_replaced(project, which):
    report = capture(project)
    path = receipts(project[0], report)[0 if which == 'definition' else 1]
    path.write_bytes(b'{"contradiction":true}\n')
    before = tree_bytes(project[0])
    with pytest.raises(SourceError):
        capture(project)
    assert tree_bytes(project[0]) == before


def test_semantically_equal_receipts_keep_original_serialization(project):
    report = capture(project)
    for path in receipts(project[0], report):
        path.write_text(json.dumps(json.loads(path.read_bytes())), encoding='utf-8')
    before = tree_bytes(project[0])
    assert capture(project)['status'] == 'existing'
    assert tree_bytes(project[0]) == before


@pytest.mark.parametrize('action', ['update', 'delete', 'reset'])
def test_no_mutating_source_subcommands(project, monkeypatch, action):
    monkeypatch.chdir(project[0])
    with pytest.raises(SystemExit) as error:
        main(['source', action])
    assert error.value.code == 2


def test_cli_failure_is_controlled_and_does_not_echo_private_path(project, monkeypatch, capsys):
    monkeypatch.chdir(project[0])
    with pytest.raises(SystemExit) as error:
        main(['source', 'capture', str(project[1] / 'missing'), '--key', 'brief',
              '--provider', 'generic', '--kind', 'document'])
    output = capsys.readouterr().err
    assert error.value.code == 2 and 'source capture failed' in output
    assert str(project[1]) not in output and 'Traceback' not in output


def test_capture_writes_only_sources_and_never_invokes_other_workflows(project, monkeypatch):
    root, _ = project
    before = tree_bytes(root)
    import subprocess
    def forbidden(*args, **kwargs): raise AssertionError('capture executed a subprocess')
    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    capture(project, retention='repository_snapshot')
    after = tree_bytes(root)
    assert before == {key: value for key, value in after.items() if key in before}
    assert all(key.startswith('sources/') for key in after.keys() - before.keys())


@pytest.mark.parametrize('field', ['st_ino', 'st_size', 'st_mtime_ns', 'st_mode'])
def test_opened_input_identity_must_match_lstat(project, monkeypatch, field):
    original = layer_module.os.fstat
    def changed(descriptor):
        value = original(descriptor)
        data = {name: getattr(value, name) for name in (
            'st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_mode')}
        data[field] += 1
        return SimpleNamespace(**data)
    monkeypatch.setattr(layer_module.os, 'fstat', changed)
    with pytest.raises(SourceError, match='changed while opening'):
        capture(project)
    assert not list((project[0] / 'sources').rglob('*.json'))


@pytest.mark.parametrize('relative', ['sources', 'sources/definitions', 'sources/captures',
                                    'sources/snapshots'])
def test_storage_reparse_cannot_redirect_capture(project, monkeypatch, relative):
    root, _ = project
    original = layer_module.os.lstat
    def reparse(target, *args, **kwargs):
        info = original(target, *args, **kwargs)
        if Path(target) == root / relative:
            return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400)
        return info
    monkeypatch.setattr(layer_module.os, 'lstat', reparse)
    with pytest.raises(SourceError):
        capture(project, retention='repository_snapshot')
    assert not list((root / 'sources').rglob('*.json'))


def test_atomic_publication_reuses_matching_race_winner(project, monkeypatch):
    original = capture_module.os.link
    raced = []
    def race(temporary, destination):
        if destination.parent.name == 'captures':
            destination.write_bytes(temporary.read_bytes())
            raced.append(destination)
        return original(temporary, destination)
    monkeypatch.setattr(capture_module.os, 'link', race)
    assert capture(project)['status'] == 'existing'
    assert len(raced) == 1
    assert not [item for item in validate(project[0]) if item[0] in {'ERROR', 'BLOCKING'}]


def test_snapshot_does_not_reread_changed_source_after_completed_capture(project, monkeypatch):
    root, source = project
    raw = source.read_bytes()
    original = capture_module._publish_receipt
    def publish(root, relative, *args):
        source.write_bytes(b'changed only after stable capture completed')
        return original(root, relative, *args)
    monkeypatch.setattr(capture_module, '_publish_receipt', publish)
    report = capture(project, retention='repository_snapshot')
    doc = json.loads(receipts(root, report)[1].read_bytes())
    assert (root / doc['snapshot']['path']).read_bytes() == raw
    assert doc['content_sha256'] == sha256(raw).hexdigest()


def test_unsupported_atomic_publication_fails_without_raw_leftovers(project, monkeypatch):
    def unsupported(*args, **kwargs):
        raise OSError('atomic hard-link publication unavailable')
    monkeypatch.setattr(capture_module.os, 'link', unsupported)
    with pytest.raises(SourceError):
        capture(project, retention='repository_snapshot')
    assert not [p for p in (project[0] / 'sources').rglob('*') if p.is_file() and p.name != '.gitkeep']


@pytest.mark.parametrize('relative', ['-', 'https://example.invalid/private.pdf'])
def test_stdin_and_url_are_not_transport_inputs(project, monkeypatch, relative):
    monkeypatch.chdir(project[0])
    with pytest.raises(SourceError):
        capture((project[0], relative))
    assert not list((project[0] / 'sources').rglob('*.json'))
