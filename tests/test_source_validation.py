"""Source provenance validation is part of the normal project validator."""

from copy import deepcopy
import json
from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator

import project_system.source_layer as module
from project_system.source_capture import capture_source
from project_system.source_layer import (
    SourceError, capture_id, source_id, inspect_source_layer, load_receipt,
    serialize_receipt, validate_capture, validate_source,
)
from project_system.init_project import init_project
from project_system.object_loader import load_object_layer
from project_system.utils import distribution_root, load_yaml
from project_system.validation import validate


def test_project_validate_rejects_unbound_snapshot(tmp_path):
    root = init_project('Demo', tmp_path / 'project')
    payload = root / ('sources/snapshots/CAP-' + '1' * 32 + '/payload.bin')
    payload.parent.mkdir(parents=True)
    payload.write_bytes(b'unbound source')
    assert any(severity == 'ERROR' and location.startswith('sources/')
               for severity, location, _ in validate(root))


@pytest.fixture
def captured(tmp_path):
    root = init_project('Demo', tmp_path / 'project')
    source = tmp_path / 'private.bin'
    source.write_bytes(b'opaque input')
    report = capture_source(root, source, key='brief', provider='generic', kind='document',
                            retention='repository_snapshot')
    definition = root / f'sources/definitions/{report["source_id"]}.json'
    capture = root / f'sources/captures/{report["capture_id"]}.json'
    payload = root / f'sources/snapshots/{report["capture_id"]}/payload.bin'
    return root, definition, capture, payload


def issues(root):
    return [item for item in validate(root) if item[1].startswith('sources')]


@pytest.mark.parametrize('filename', ['source.schema.json', 'source-capture.schema.json'])
def test_strict_schemas_are_packaged(filename):
    schema = json.loads((distribution_root() / 'schemas' / filename).read_bytes())
    Draft202012Validator.check_schema(schema)
    assert schema['additionalProperties'] is False
    assert set(schema['required']) == set(schema['properties'])


def test_new_layout_ignore_boundary_and_legacy_without_sources(tmp_path):
    root = init_project('Demo', tmp_path / 'project')
    for directory in ('definitions', 'captures', 'snapshots'):
        assert (root / 'sources' / directory / '.gitkeep').read_bytes() == b''
    assert 'sources/snapshots/**' in (root / '.llmignore').read_text().splitlines()
    assert not any('sources' in line for line in (root / '.gitignore').read_text().splitlines())
    before = validate(root)
    shutil.rmtree(root / 'sources')
    assert validate(root) == before


def test_absent_source_layer_does_not_add_parent_path_constraints_to_legacy_project(tmp_path, monkeypatch):
    root = init_project('Demo', tmp_path / 'legacy')
    shutil.rmtree(root / 'sources')
    original = module.os.lstat
    def legacy_alias(target, *args, **kwargs):
        info = original(target, *args, **kwargs)
        if Path(target) == root:
            return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400)
        return info
    monkeypatch.setattr(module.os, 'lstat', legacy_alias)
    assert inspect_source_layer(root, load_yaml(root / 'project.yaml')).issues == ()


def test_valid_source_layer_is_not_a_knowledge_layer(captured):
    root = captured[0]
    assert issues(root) == []
    assert load_object_layer(root).objects == {}
    inspected = inspect_source_layer(root, load_yaml(root / 'project.yaml'))
    assert len(inspected.definitions) == len(inspected.captures) == 1


@pytest.mark.parametrize('index', [1, 2])
@pytest.mark.parametrize('raw', [
    b'{', b'{"schema_version":1,"schema_version":1}', b'{"bytes":NaN}',
    b'{"bytes":Infinity}', b'{"bytes":-Infinity}', b'\xff', b'[]', b'null', b'"text"',
    b'[' * 2000 + b']' * 2000,
])
def test_malformed_receipt_is_controlled_validation_error(captured, index, raw):
    captured[index].write_bytes(raw)
    found = issues(captured[0])
    assert any(item[0] == 'ERROR' and item[1] == captured[index].relative_to(captured[0]).as_posix()
               for item in found)


@pytest.mark.parametrize('index', [1, 2])
def test_oversize_receipt_rejected(captured, index):
    captured[index].write_bytes(b' ' * (module.MAX_RECEIPT_BYTES + 1))
    assert any('size limit' in item[2] for item in issues(captured[0]))


@pytest.mark.parametrize('index', [1, 2])
@pytest.mark.parametrize('field,value', [('unknown', 'x'), ('schema_version', True),
                                        ('schema_version', 1.0), ('schema_version', 2)])
def test_strict_receipt_fields(captured, index, field, value):
    doc = json.loads(captured[index].read_bytes())
    doc[field] = value
    captured[index].write_bytes(serialize_receipt(doc))
    assert issues(captured[0])


@pytest.mark.parametrize('index,field,prefix', [(1, 'source_id', 'SRC-'), (2, 'capture_id', 'CAP-')])
def test_tampered_id_and_filename_rejected(captured, index, field, prefix):
    doc = json.loads(captured[index].read_bytes())
    doc[field] = prefix + '0' * 32
    captured[index].write_bytes(serialize_receipt(doc))
    assert any('semantic identity' in item[2] for item in issues(captured[0]))


@pytest.mark.parametrize('index', [1, 2])
def test_filename_mismatch_rejected(captured, index):
    captured[index].rename(captured[index].with_name('wrong.json'))
    assert any('filename/internal ID' in item[2] for item in issues(captured[0]))


def test_duplicate_keys_even_with_valid_independent_ids_rejected(captured):
    root, path, _, _ = captured
    doc = json.loads(path.read_bytes())
    doc['provider'] = 'email'
    doc['source_id'] = source_id(doc)
    (path.parent / (doc['source_id'] + '.json')).write_bytes(serialize_receipt(doc))
    assert any('duplicate Source key' in item[2] for item in issues(root))


@pytest.mark.parametrize('index', [1, 2])
def test_duplicate_ids_detected_independently_of_filename(captured, index):
    captured[index].with_name('duplicate.json').write_bytes(captured[index].read_bytes())
    found = issues(captured[0])
    assert any('duplicate source/capture ID' in item[2] for item in found)
    assert any('filename/internal ID' in item[2] for item in found)


@pytest.mark.parametrize('index', [1, 2])
def test_project_mismatch_even_after_correct_identity_rehash(captured, index):
    root, path = captured[0], captured[index]
    doc = json.loads(path.read_bytes())
    doc['project_id'] = 'different-project'
    field, builder = ('source_id', source_id) if index == 1 else ('capture_id', capture_id)
    doc[field] = builder(doc)
    if index == 2:
        doc['snapshot']['path'] = f'sources/snapshots/{doc[field]}/payload.bin'
    path.unlink()
    (path.parent / (doc[field] + '.json')).write_bytes(serialize_receipt(doc))
    assert any('project_id mismatch' in item[2] for item in issues(root))


def test_missing_source_reference_rejected(captured):
    captured[1].unlink()
    assert any('missing valid Source' in item[2] for item in issues(captured[0]))


@pytest.mark.parametrize('fault', ['tamper', 'delete', 'directory', 'reparse'])
def test_snapshot_integrity_is_checked_by_project_validate(captured, monkeypatch, fault):
    root, _, _, payload = captured
    if fault == 'tamper': payload.write_bytes(b'tampered')
    elif fault == 'delete': payload.unlink()
    elif fault == 'directory':
        payload.unlink()
        payload.mkdir()
    else:
        original = module.os.lstat
        def reparse(target, *args, **kwargs):
            value = original(target, *args, **kwargs)
            if Path(target) == payload:
                return SimpleNamespace(st_mode=value.st_mode, st_file_attributes=0x400)
            return value
        monkeypatch.setattr(module.os, 'lstat', reparse)
    assert any(item[0] == 'ERROR' and item[1].endswith('payload.bin') for item in issues(root))


@pytest.mark.parametrize('field,value', [('path', '../outside'), ('sha256', '1' * 64),
                                        ('bytes', 999), ('bytes', True), ('unknown', 'x')])
def test_snapshot_metadata_cannot_escape_or_contradict_capture(captured, field, value):
    doc = json.loads(captured[2].read_bytes())
    doc['snapshot'][field] = value
    captured[2].write_bytes(serialize_receipt(doc))
    assert issues(captured[0])


@pytest.mark.parametrize('retention,snapshot', [('reference', {'path': 'x', 'sha256': '0' * 64, 'bytes': 0}),
                                              ('repository_snapshot', None)])
def test_retention_snapshot_invariant(retention, snapshot, captured):
    doc = json.loads(captured[2].read_bytes())
    doc.update(retention=retention, snapshot=snapshot)
    doc['capture_id'] = capture_id(doc)
    with pytest.raises(SourceError):
        validate_capture(doc)


@pytest.mark.parametrize('relative', [
    'definitions/unexpected.yaml', 'definitions/nested/file.json', 'captures/a.md',
    'captures/a.JSON', 'snapshots/raw.bin', 'snapshots/bad-id/payload.bin',
    'snapshots/CAP-' + '1' * 32 + '/extra/data.bin', 'other.txt',
    'definitions/.gitkeep',
])
def test_unexpected_files_fail_instead_of_being_ignored(captured, relative):
    path = captured[0] / 'sources' / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'unexpected bytes')
    assert issues(captured[0])


def test_reference_does_not_require_original_or_raw_snapshot(tmp_path):
    root = init_project('Demo', tmp_path / 'project')
    source = tmp_path / 'private'
    source.write_bytes(b'bytes')
    capture_source(root, source, key='brief', provider='generic', kind='document')
    source.unlink()
    assert issues(root) == []


def test_reference_cannot_bind_orphan_payload(captured):
    root, _, path, _ = captured
    doc = json.loads(path.read_bytes())
    doc.update(retention='reference', snapshot=None)
    doc['capture_id'] = capture_id(doc)
    path.unlink()
    (path.parent / (doc['capture_id'] + '.json')).write_bytes(serialize_receipt(doc))
    assert any('not bound' in item[2] for item in issues(root))


@pytest.mark.parametrize('relative', ['sources', 'sources/definitions', 'sources/snapshots'])
def test_storage_parent_reparse_fails_closed(captured, monkeypatch, relative):
    root = captured[0]
    original = module.os.lstat
    def reparse(target, *args, **kwargs):
        info = original(target, *args, **kwargs)
        if Path(target) == root / relative:
            return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400)
        return info
    monkeypatch.setattr(module.os, 'lstat', reparse)
    assert issues(root)


def test_snapshot_symlink_fails_closed(captured, tmp_path):
    payload = captured[3]
    external = tmp_path / 'outside'
    external.write_bytes(payload.read_bytes())
    payload.unlink()
    try:
        payload.symlink_to(external)
    except OSError:
        pytest.skip('symlink creation unavailable')
    assert issues(captured[0])


def test_issue_ordering_is_deterministic(captured):
    root = captured[0]
    for name in ['z.yaml', 'a.yaml', 'm.yaml']:
        (root / 'sources/definitions' / name).write_bytes(b'invalid')
    first = issues(root)
    assert first == sorted(first) == issues(root)


@pytest.mark.parametrize('relative', ['../escape', 'sources/../escape', '/sources/x',
                                    'sources\\definitions\\x', 'sources//x', 'sources/a:b'])
def test_storage_path_traversal_is_rejected(tmp_path, relative):
    with pytest.raises(SourceError):
        module.source_path(tmp_path, relative)


@pytest.mark.parametrize('field,value', [('source_id', 'SRC-' + '0' * 32 + '\n'),
                                        ('bytes', 12.0), ('media_type', 'text/plain\n')])
def test_semantically_malformed_values_rejected_after_rehash(captured, field, value):
    doc = json.loads(captured[2].read_bytes())
    doc[field] = value
    doc['capture_id'] = capture_id(doc)
    doc['snapshot']['path'] = f'sources/snapshots/{doc["capture_id"]}/payload.bin'
    with pytest.raises(SourceError):
        validate_capture(doc)
