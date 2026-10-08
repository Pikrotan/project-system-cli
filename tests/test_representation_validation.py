"""Durable representation evidence is checked by normal project validation."""

from copy import deepcopy
import json
from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator

import project_system.representation_layer as module
from project_system.init_project import init_project
from project_system.object_loader import load_object_layer
from project_system.representation_layer import (
    RepresentationError, adapter_fingerprint, inspect_representation_layer,
    representation_id, segment_id, validate_representation,
)
from project_system.source_capture import capture_source
from project_system.source_layer import canonical_bytes, inspect_source_layer, serialize_receipt
from project_system.source_representation import represent_source
from project_system.utils import distribution_root, load_yaml
from project_system.validation import validate


def test_project_validate_rejects_unbound_representation_index(tmp_path):
    root = init_project('Demo', tmp_path / 'project')
    path = root / ('intake/representations/REP-' + '1' * 32 + '.segments.jsonl')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'')
    assert any(severity == 'ERROR' and location.startswith('intake/')
               for severity, location, _ in validate(root))


@pytest.fixture
def represented(tmp_path):
    root = init_project('Demo', tmp_path / 'project')
    source = tmp_path / 'private.txt'
    source.write_bytes('first\r\n\nβ'.encode())
    capture = capture_source(root, source, key='chat', provider='generic',
                             kind='conversation', retention='repository_snapshot')
    report = represent_source(root, capture['capture_id'], adapter='utf8-lines')
    receipt = root / f'intake/representations/{report["representation_id"]}.json'
    index = receipt.with_suffix('.segments.jsonl')
    generated = root / f'.generated/source-representations/{report["representation_id"]}'
    return root, source, capture, report, receipt, index, generated


def representation_issues(root):
    return [item for item in validate(root) if item[1].startswith('intake')]


def lines(index):
    return [json.loads(line) for line in index.read_bytes().splitlines()]


def write_lines(index, descriptors):
    index.write_bytes(b''.join(canonical_bytes(item) + b'\n' for item in descriptors))


def test_schema_is_strict_packaged_and_exact():
    schema = json.loads((distribution_root() / 'schemas/representation.schema.json').read_bytes())
    Draft202012Validator.check_schema(schema)
    assert schema['additionalProperties'] is False
    assert set(schema['required']) == set(schema['properties'])
    assert schema['properties']['adapter']['additionalProperties'] is False
    assert schema['properties']['segment_index']['additionalProperties'] is False
    assert schema['properties']['canonical_authority'] == {'const': False}


def test_valid_representation_survives_generated_cache_deletion(represented):
    root, _, _, report, _, _, generated = represented
    assert representation_issues(root) == []
    shutil.rmtree(generated)
    assert representation_issues(root) == []
    assert report['representation_id'] in inspect_representation_layer(
        root, load_yaml(root / 'project.yaml'),
        inspect_source_layer(root, load_yaml(root / 'project.yaml'))).representations
    assert load_object_layer(root).objects == {}


def test_project_init_creates_current_known_intake_directories(tmp_path):
    root = init_project('Demo', tmp_path / 'project')
    assert (root / 'intake/representations/.gitkeep').read_bytes() == b''
    assert sorted(path.name for path in (root / 'intake').iterdir()) == [
        'extraction-contracts', 'extraction-runs', 'extraction-submissions',
        'proposals', 'representations']


def test_legacy_project_without_intake_remains_identical(tmp_path):
    root = init_project('Demo', tmp_path / 'project')
    before = validate(root)
    shutil.rmtree(root / 'intake')
    assert validate(root) == before


def test_absent_intake_does_not_add_parent_reparse_constraints(tmp_path, monkeypatch):
    root = init_project('Demo', tmp_path / 'legacy')
    shutil.rmtree(root / 'intake')
    original = module.os.lstat
    def reparse(target, *args, **kwargs):
        info = original(target, *args, **kwargs)
        if Path(target) == root:
            return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400)
        return info
    monkeypatch.setattr(module.os, 'lstat', reparse)
    source = inspect_source_layer(root, load_yaml(root / 'project.yaml'))
    assert inspect_representation_layer(root, load_yaml(root / 'project.yaml'), source).issues == ()


@pytest.mark.parametrize('raw', [
    b'{', b'{"schema_version":1,"schema_version":1}', b'{"x":NaN}', b'\xff',
    b'[]', b'null', b'"text"', b'[' * 2000 + b']' * 2000,
])
def test_malformed_receipt_rejected(represented, raw):
    represented[4].write_bytes(raw)
    assert representation_issues(represented[0])


def test_oversized_receipt_rejected(represented):
    represented[4].write_bytes(b' ' * (module.MAX_REPRESENTATION_RECEIPT_BYTES + 1))
    assert any('size limit' in item[2] for item in representation_issues(represented[0]))


@pytest.mark.parametrize('mutate', [
    lambda doc: doc.update(extra='x'),
    lambda doc: doc.update(schema_version=True),
    lambda doc: doc.update(canonical_authority=True),
    lambda doc: doc['adapter'].update(options={'future': True}),
    lambda doc: doc['adapter'].update(fingerprint='0' * 64),
    lambda doc: doc['segment_index'].update(path='../escape'),
    lambda doc: doc['segment_index'].update(bytes=True),
])
def test_strict_receipt_and_adapter_contract(represented, mutate):
    doc = json.loads(represented[4].read_bytes())
    mutate(doc)
    represented[4].write_bytes(serialize_receipt(doc))
    assert representation_issues(represented[0])


def test_filename_internal_id_mismatch(represented):
    represented[4].rename(represented[4].with_name('REP-' + '0' * 32 + '.json'))
    assert any('pair is incomplete' in item[2] for item in representation_issues(represented[0]))


def test_recomputed_rep_id_still_rejects_project_mismatch(represented):
    root, _, _, _, receipt, index, _ = represented
    doc = json.loads(receipt.read_bytes())
    old = doc['representation_id']
    doc['project_id'] = 'other-project'
    doc['representation_id'] = representation_id(doc)
    doc['segment_index']['path'] = f'intake/representations/{doc["representation_id"]}.segments.jsonl'
    receipt.unlink()
    index.rename(index.with_name(doc['representation_id'] + '.segments.jsonl'))
    (receipt.parent / (doc['representation_id'] + '.json')).write_bytes(serialize_receipt(doc))
    assert any('project_id mismatch' in item[2] for item in representation_issues(root))
    assert old != doc['representation_id']


def test_missing_capture_and_capture_hash_binding_mismatch(represented):
    root, _, capture, _, receipt, _, _ = represented
    (root / f'sources/captures/{capture["capture_id"]}.json').unlink()
    assert any('missing valid Capture' in item[2] for item in representation_issues(root))
    # Restore a structurally valid Capture, then prove independent hash binding.
    represented2 = json.loads(receipt.read_bytes())
    represented2['capture_content_sha256'] = '0' * 64
    represented2['representation_id'] = representation_id(represented2)
    with pytest.raises(RepresentationError, match='representation_id|path'):
        validate_representation(represented2)


@pytest.mark.parametrize('fault', ['missing', 'hash', 'bytes', 'count', 'oversize'])
def test_index_receipt_binding_failures(represented, fault):
    root, _, _, _, receipt, index, _ = represented
    if fault == 'missing':
        index.unlink()
    elif fault == 'oversize':
        index.write_bytes(b'x' * (module.MAX_SEGMENT_INDEX_BYTES + 1))
    else:
        doc = json.loads(receipt.read_bytes())
        key = {'hash': 'sha256', 'bytes': 'bytes', 'count': 'segments'}[fault]
        doc['segment_index'][key] = ('0' * 64 if key == 'sha256'
                                      else doc['segment_index'][key] + 1)
        doc['representation_id'] = representation_id(doc)
        receipt.write_bytes(serialize_receipt(doc))
    assert representation_issues(root)


@pytest.mark.parametrize('raw', [
    b'{}', b'{}\r\n', b'{}\n\n', b'{"ordinal":NaN}\n', b'\xff\n',
    b'{"ordinal":0,"ordinal":0}\n', b' {"ordinal":0}\n',
])
def test_malformed_or_noncanonical_jsonl_rejected(represented, raw):
    represented[5].write_bytes(raw)
    assert representation_issues(represented[0])


@pytest.mark.parametrize('fault', [
    'segment_id', 'duplicate_segment', 'ordinal', 'line', 'span_gap', 'span_overlap',
    'span_reverse', 'source_bytes', 'source_hash', 'rendered_bytes', 'unknown',
])
def test_descriptor_integrity_failures(represented, fault):
    descriptors = lines(represented[5])
    item = descriptors[0]
    if fault == 'segment_id': item['segment_id'] = 'SEG-' + '0' * 32
    elif fault == 'duplicate_segment':
        descriptors[1]['segment_id'] = item['segment_id']
    elif fault == 'ordinal': item['ordinal'] = 1
    elif fault == 'line': item['locator']['line'] = 2
    elif fault == 'span_gap': item['source_span']['byte_start'] = 1
    elif fault == 'span_overlap': descriptors[1]['source_span']['byte_start'] = 0
    elif fault == 'span_reverse': item['source_span']['byte_end'] = 0
    elif fault == 'source_bytes': item['source_bytes'] += 1
    elif fault == 'source_hash': item['source_sha256'] = '0' * 64
    elif fault == 'rendered_bytes': item['rendered_bytes'] = item['source_bytes'] + 1
    elif fault == 'unknown': item['text'] = 'leak'
    write_lines(represented[5], descriptors)
    assert representation_issues(represented[0])


def test_descriptor_count_and_capture_coverage_enforced(represented):
    descriptors = lines(represented[5])[:-1]
    raw = b''.join(canonical_bytes(item) + b'\n' for item in descriptors)
    doc = json.loads(represented[4].read_bytes())
    with pytest.raises(RepresentationError, match='cover Capture bytes'):
        module.parse_segment_index(
            raw, project_id=doc['project_id'], capture_id=doc['capture_id'],
            fingerprint=doc['adapter']['fingerprint'],
            capture_bytes=json.loads(
                (represented[0] / f'sources/captures/{doc["capture_id"]}.json').read_bytes()
            )['bytes'])


@pytest.mark.parametrize('relative', [
    'intake/unexpected.txt', 'intake/future/file.json',
    'intake/representations/unexpected.yaml', 'intake/representations/nested/file.json',
    'intake/representations/.gitkeep',
])
def test_unexpected_intake_files_rejected(represented, relative):
    path = represented[0] / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'unexpected')
    assert representation_issues(represented[0])


def test_only_empty_regular_gitkeep_is_ignored(represented):
    keep = represented[0] / 'intake/representations/.gitkeep'
    assert representation_issues(represented[0]) == []
    keep.write_bytes(b'not empty')
    assert representation_issues(represented[0])


@pytest.mark.parametrize('relative', ['intake', 'intake/representations'])
def test_intake_parent_reparse_fails_closed(represented, monkeypatch, relative):
    root = represented[0]
    original = module.os.lstat
    def reparse(target, *args, **kwargs):
        info = original(target, *args, **kwargs)
        if Path(target) == root / relative:
            return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400)
        return info
    monkeypatch.setattr(module.os, 'lstat', reparse)
    assert representation_issues(root)


def test_index_symlink_rejected(represented, tmp_path):
    index = represented[5]
    external = tmp_path / 'outside'
    external.write_bytes(index.read_bytes())
    index.unlink()
    try:
        index.symlink_to(external)
    except OSError:
        pytest.skip('symlink creation unavailable')
    assert representation_issues(represented[0])


def test_representation_issue_ordering_deterministic(represented):
    root = represented[0]
    for name in ('z.yaml', 'a.yaml', 'm.yaml'):
        (root / 'intake/representations' / name).write_bytes(b'invalid')
    first = representation_issues(root)
    assert first == sorted(first) == representation_issues(root)
