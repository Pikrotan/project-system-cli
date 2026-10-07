"""Stage 13B1 deterministic representation from verified Capture bytes."""

import json
import os
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest

import project_system.source_representation as module
import project_system.source_layer as source_layer_module
from project_system.representation_layer import (
    RepresentationError, adapter_fingerprint, adapter_identity, parse_segment_index,
    representation_id, segment_id,
)
from project_system.source_representation import represent_source
from project_system.source_layer import canonical_bytes

from project_system.cli import main
from project_system.init_project import init_project
from project_system.source_capture import capture_source


def test_cli_represents_reference_as_durable_index_and_disposable_segments(tmp_path, monkeypatch, capsys):
    root = init_project('Demo', tmp_path / 'project')
    source = tmp_path / 'private-name.txt'
    source.write_bytes('alpha\r\n\nβ'.encode())
    capture = capture_source(root, source, key='chat', provider='generic', kind='conversation')
    monkeypatch.chdir(root)
    main(['source', 'represent', capture['capture_id'], '--adapter', 'utf8-lines',
          '--input', str(source)])
    report = json.loads(capsys.readouterr().out)
    receipt = root / f'intake/representations/{report["representation_id"]}.json'
    index = receipt.with_suffix('.segments.jsonl')
    assert receipt.is_file() and index.is_file()
    assert len(index.read_bytes().splitlines()) == report['segments'] == 3
    assert sha256(index.read_bytes()).hexdigest() == json.loads(receipt.read_bytes())['segment_index']['sha256']
    assert len(list((root / f'.generated/source-representations/{report["representation_id"]}/segments').glob('*.txt'))) == 3


def test_cli_requires_explicit_representation_adapter(tmp_path, monkeypatch):
    root = init_project('Demo', tmp_path / 'project')
    monkeypatch.chdir(root)

    with pytest.raises(SystemExit) as error:
        main(['source', 'represent', 'CAP-' + ('a' * 32)])

    assert error.value.code == 2


def prepared(tmp_path, raw, retention='reference'):
    root = init_project('Demo', tmp_path / 'project')
    source = tmp_path / 'private-original-name.txt'
    source.write_bytes(raw)
    capture = capture_source(root, source, key='chat', provider='generic', kind='conversation',
                             retention=retention)
    return root, source, capture


def represent(prepared_project):
    root, source, capture = prepared_project
    return represent_source(root, capture['capture_id'], adapter='utf8-lines',
                            input_path=source if capture['retention'] == 'reference' else None)


def artifacts(prepared_project, report):
    root = prepared_project[0]
    receipt = root / f'intake/representations/{report["representation_id"]}.json'
    index = receipt.with_suffix('.segments.jsonl')
    doc = json.loads(receipt.read_bytes())
    descriptors = [json.loads(line) for line in index.read_bytes().splitlines()]
    generated = root / f'.generated/source-representations/{report["representation_id"]}/segments'
    return receipt, index, doc, descriptors, generated


def test_adapter_fingerprint_is_canonical_semantic_identity_only():
    expected = sha256(canonical_bytes({'id': 'utf8-lines', 'version': 1, 'options': {}})).hexdigest()
    assert adapter_identity() == {'id': 'utf8-lines', 'version': 1, 'options': {}}
    assert adapter_fingerprint() == expected


@pytest.mark.parametrize(('raw', 'rendered'), [
    (b'a\n', [b'a']), (b'a\r\n', [b'a']), (b'a\rb', [b'a\rb']),
    (b'\n', [b'']), (b'a\n\n', [b'a', b'']), (b'a\n', [b'a']),
    (b'', []), ('α🙂\nβ'.encode(), ['α🙂'.encode(), 'β'.encode()]),
    (b' a \t\n', [b' a \t']), (b'\xef\xbb\xbfBOM', [b'\xef\xbb\xbfBOM']),
])
def test_exact_utf8_lines_semantics(tmp_path, raw, rendered):
    project = prepared(tmp_path, raw)
    report = represent(project)
    _, index, doc, descriptors, generated = artifacts(project, report)
    assert report['segments'] == len(rendered) == doc['segment_index']['segments']
    assert doc['canonical_authority'] is False
    assert index.read_bytes() == b''.join(canonical_bytes(item) + b'\n' for item in descriptors)
    assert [
        (generated / f'{item["segment_id"]}.txt').read_bytes() for item in descriptors
    ] == rendered
    start = 0
    physical = ([part + b'\n' for part in raw.split(b'\n')[:-1]]
                + ([raw.split(b'\n')[-1]] if raw and not raw.endswith(b'\n') else []))
    assert len(physical) == len(descriptors)
    for ordinal, (item, source_line, rendered_line) in enumerate(zip(descriptors, physical, rendered)):
        assert item['ordinal'] == ordinal and item['locator'] == {'kind': 'line', 'line': ordinal + 1}
        assert item['source_span'] == {'byte_start': start, 'byte_end': start + len(source_line)}
        assert item['source_sha256'] == sha256(source_line).hexdigest()
        assert item['source_bytes'] == len(source_line)
        assert item['rendered_sha256'] == sha256(rendered_line).hexdigest()
        assert item['rendered_bytes'] == len(rendered_line)
        start += len(source_line)


def test_multibyte_offsets_are_bytes_and_seg_identity_has_no_rep_cycle(tmp_path):
    project = prepared(tmp_path, 'Я\n🙂'.encode())
    report = represent(project)
    _, _, doc, descriptors, _ = artifacts(project, report)
    assert [item['source_span'] for item in descriptors] == [
        {'byte_start': 0, 'byte_end': 3}, {'byte_start': 3, 'byte_end': 7}]
    first = descriptors[0]
    assert first['segment_id'] == segment_id(doc['project_id'], doc['capture_id'],
                                             doc['adapter']['fingerprint'], first)
    changed = dict(first, ordinal=999)
    assert segment_id(doc['project_id'], doc['capture_id'],
                      doc['adapter']['fingerprint'], changed) == first['segment_id']
    assert report['representation_id'] == representation_id(doc)
    assert report['representation_id'].encode() not in canonical_bytes(first)


def test_rep_and_seg_formulas_are_independently_reproducible(tmp_path):
    project = prepared(tmp_path, b'a\r\n\n')
    report = represent(project)
    _, index, doc, descriptors, _ = artifacts(project, report)
    identity = {
        'project_id': doc['project_id'], 'capture_id': doc['capture_id'],
        'capture_content_sha256': doc['capture_content_sha256'],
        'adapter_fingerprint': doc['adapter']['fingerprint'],
        'segment_index_sha256': sha256(index.read_bytes()).hexdigest(),
        'segment_index_bytes': len(index.read_bytes()), 'segment_count': len(descriptors)}
    assert report['representation_id'] == 'REP-' + sha256(canonical_bytes(identity)).hexdigest()[:32]
    for item in descriptors:
        payload = {key: item[key] for key in (
            'locator', 'source_span', 'source_sha256', 'source_bytes',
            'rendered_sha256', 'rendered_bytes')}
        payload.update(project_id=doc['project_id'], capture_id=doc['capture_id'],
                       adapter_fingerprint=doc['adapter']['fingerprint'])
        assert item['segment_id'] == 'SEG-' + sha256(canonical_bytes(payload)).hexdigest()[:32]


@pytest.mark.parametrize('raw', [b'\xff', b'valid\n\xed\xa0\x80', b'\xc0\xaf'])
def test_strict_utf8_failure_publishes_nothing(tmp_path, raw):
    project = prepared(tmp_path, raw)
    with pytest.raises(RepresentationError, match='strict UTF-8'):
        represent(project)
    assert not list((project[0] / 'intake/representations').glob('REP-*'))
    assert not (project[0] / '.generated/source-representations').exists()


def test_durable_artifacts_contain_no_source_text_or_private_path(tmp_path):
    secret = b'CUSTOMER_SECRET_LITERAL_927\n'
    project = prepared(tmp_path, secret)
    report = represent(project)
    receipt, index, _, _, _ = artifacts(project, report)
    durable = receipt.read_bytes() + index.read_bytes()
    assert b'CUSTOMER_SECRET_LITERAL_927' not in durable
    assert project[1].name.encode() not in durable and str(project[1].parent).encode() not in durable
    for forbidden in (b'timestamp', b'hostname', b'username', b'pid'):
        assert forbidden not in durable.lower()


def test_reference_requires_input_and_exact_capture_bytes(tmp_path, monkeypatch):
    project = prepared(tmp_path, b'expected\n')
    with pytest.raises(RepresentationError, match='requires --input'):
        represent_source(project[0], project[2]['capture_id'], adapter='utf8-lines')
    project[1].write_bytes(b'wrong\n')
    monkeypatch.setattr(module, '_utf8_lines', lambda *a, **k: (_ for _ in ()).throw(
        AssertionError('adapter ran before Capture byte verification')))
    with pytest.raises(RepresentationError, match='do not match'):
        represent(project)
    assert not list((project[0] / 'intake/representations').glob('REP-*'))


def test_input_reparse_is_rejected_before_copy_or_adapter(tmp_path, monkeypatch):
    project = prepared(tmp_path, b'bound\n')
    original = source_layer_module.os.lstat
    def reparse(target, *args, **kwargs):
        info = original(target, *args, **kwargs)
        if Path(target) == project[1]:
            return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400)
        return info
    monkeypatch.setattr(source_layer_module.os, 'lstat', reparse)
    monkeypatch.setattr(module, '_utf8_lines', lambda *a, **k: (_ for _ in ()).throw(
        AssertionError('adapter ran on reparse input')))
    with pytest.raises(RepresentationError, match='symlink|reparse'):
        represent(project)
    assert not list((project[0] / 'intake/representations').glob('REP-*'))


def test_repository_snapshot_forbids_input_and_is_verified(tmp_path):
    project = prepared(tmp_path, b'snapshot\n', retention='repository_snapshot')
    with pytest.raises(RepresentationError, match='forbids --input'):
        represent_source(project[0], project[2]['capture_id'], adapter='utf8-lines',
                         input_path=project[1])
    report = represent(project)
    assert report['segments'] == 1
    snapshot = project[0] / f'sources/snapshots/{project[2]["capture_id"]}/payload.bin'
    snapshot.write_bytes(b'tampered')
    with pytest.raises(RepresentationError, match='source layer is invalid'):
        represent(project)


def test_adapter_reads_verified_copy_after_external_input_changes(tmp_path, monkeypatch):
    project = prepared(tmp_path, b'bound\nbytes')
    original = module._utf8_lines
    def after_verification(verified, *args, **kwargs):
        project[1].write_bytes(b'mutable original changed after verification')
        assert Path(verified).read_bytes() == b'bound\nbytes'
        assert Path(verified) != project[1]
        return original(verified, *args, **kwargs)
    monkeypatch.setattr(module, '_utf8_lines', after_verification)
    report = represent(project)
    _, _, _, descriptors, generated = artifacts(project, report)
    assert [
        (generated / f'{item["segment_id"]}.txt').read_bytes() for item in descriptors
    ] == [b'bound', b'bytes']


def test_input_toctou_and_reparse_fail_closed(tmp_path, monkeypatch):
    project = prepared(tmp_path, b'bound\n')
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
                with original(project[1], 'ab') as output:
                    output.write(b'changed during verified copy')
            return data
    def opening(path, *args, **kwargs):
        stream = original(path, *args, **kwargs)
        return MutatingReader(stream) if path == project[1] and args == ('rb',) else stream
    monkeypatch.setattr(Path, 'open', opening)
    with pytest.raises(RepresentationError, match='changed'):
        represent(project)
    assert changed
    assert not list((project[0] / 'intake/representations').glob('REP-*'))


def test_input_symlink_rejected(tmp_path):
    project = prepared(tmp_path, b'bound\n')
    link = tmp_path / 'private-link'
    try:
        link.symlink_to(project[1])
    except OSError:
        pytest.skip('symlink creation unavailable')
    with pytest.raises(RepresentationError, match='symlink|reparse'):
        represent_source(project[0], project[2]['capture_id'], adapter='utf8-lines',
                         input_path=link)


def test_verified_sensitive_temp_is_cleaned_on_success_and_failure(tmp_path, monkeypatch):
    project = prepared(tmp_path, b'a\n')
    scratch = tmp_path / 'controlled-temp'
    scratch.mkdir()
    monkeypatch.setattr(module.tempfile, 'tempdir', str(scratch))
    represent(project)
    assert list(scratch.iterdir()) == []
    project2 = prepared(tmp_path / 'failure', b'\xff')
    with pytest.raises(RepresentationError):
        represent(project2)
    assert list(scratch.iterdir()) == []


def test_idempotent_durable_artifacts_and_cache_rebuild(tmp_path):
    project = prepared(tmp_path, b'a\n\n')
    first = represent(project)
    receipt, index, _, descriptors, generated = artifacts(project, first)
    durable_before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in (receipt, index)}
    cache_before = {path.name: path.read_bytes() for path in generated.glob('*.txt')}
    second = represent(project)
    assert second == dict(first, status='existing')
    assert durable_before == {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in (receipt, index)}
    assert cache_before == {path.name: path.read_bytes() for path in generated.glob('*.txt')}
    for path in generated.glob('*.txt'):
        path.unlink()
    third = represent(project)
    assert third == dict(first, status='rebuilt_cache')
    assert durable_before == {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in (receipt, index)}
    assert len(list(generated.glob('*.txt'))) == len(descriptors)


@pytest.mark.parametrize('artifact', ['receipt', 'index'])
def test_contradictory_existing_durable_artifact_fails_closed(tmp_path, artifact):
    project = prepared(tmp_path, b'a\n')
    report = represent(project)
    receipt, index, _, _, _ = artifacts(project, report)
    target = receipt if artifact == 'receipt' else index
    target.write_bytes(b'contradiction')
    before = target.read_bytes()
    with pytest.raises(RepresentationError):
        represent(project)
    assert target.read_bytes() == before


@pytest.mark.parametrize('limit,value', [('segments', 1), ('index', 1)])
def test_bounds_fail_without_partial_durable_state(tmp_path, monkeypatch, limit, value):
    project = prepared(tmp_path, b'a\nb\n')
    monkeypatch.setattr(module,
                        'MAX_REPRESENTATION_SEGMENTS' if limit == 'segments' else 'MAX_SEGMENT_INDEX_BYTES',
                        value)
    with pytest.raises(RepresentationError, match='limit'):
        represent(project)
    assert not list((project[0] / 'intake/representations').glob('REP-*'))
    assert not (project[0] / '.generated/source-representations').exists()


@pytest.mark.parametrize('action', ['update', 'delete', 'reset'])
def test_no_mutating_representation_subcommands(tmp_path, monkeypatch, action):
    root = init_project('Demo', tmp_path / 'project')
    monkeypatch.chdir(root)
    with pytest.raises(SystemExit) as error:
        main(['source', action])
    assert error.value.code == 2


def test_no_network_subprocess_or_product_truth_writes(tmp_path, monkeypatch):
    project = prepared(tmp_path, b'a\n')
    protected = ['knowledge', 'docs', '.project/policies', '.agents/skills']
    before = {name: sorted((p.relative_to(project[0]).as_posix(), p.read_bytes())
                           for p in (project[0] / name).rglob('*') if p.is_file())
              for name in protected}
    import subprocess
    monkeypatch.setattr(subprocess, 'Popen', lambda *a, **k: (_ for _ in ()).throw(
        AssertionError('representation invoked a subprocess')))
    represent(project)
    after = {name: sorted((p.relative_to(project[0]).as_posix(), p.read_bytes())
                          for p in (project[0] / name).rglob('*') if p.is_file())
             for name in protected}
    assert before == after


def test_cli_failure_is_controlled_and_hides_private_input_path(tmp_path, monkeypatch, capsys):
    project = prepared(tmp_path, b'expected')
    project[1].write_bytes(b'wrong')
    monkeypatch.chdir(project[0])
    with pytest.raises(SystemExit) as error:
        main(['source', 'represent', project[2]['capture_id'], '--adapter', 'utf8-lines',
              '--input', str(project[1])])
    output = capsys.readouterr().err
    assert error.value.code == 2 and 'source represent failed' in output
    assert str(project[1]) not in output and 'Traceback' not in output
