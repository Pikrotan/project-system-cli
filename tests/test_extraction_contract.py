"""Stage 13B2a immutable extraction-contract behavior."""

from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest

import project_system.intake_layer as intake_module
import project_system.source_extraction as extraction_module
import project_system.source_layer as source_layer_module
from extraction_test_helpers import represented_project
from project_system.cli import main
from project_system.extraction_layer import (
    EXTRACTION_CONTRACT_PROFILE,
    MAX_XCON_BYTES,
    PROPOSAL_KINDS,
    ExtractionError,
    extraction_contract_id,
)
from project_system.init_project import init_project
from project_system.intake_layer import KNOWN_INTAKE_DIRECTORIES
from project_system.source_capture import capture_source
from project_system.source_extraction import create_extraction_contract
from project_system.source_layer import canonical_bytes
from project_system.source_representation import represent_source
from project_system.validation import validate


def intake_issues(root):
    return [item for item in validate(root) if item[1].startswith('intake')]


def test_extraction_contract_module_and_profile_exist():
    assert EXTRACTION_CONTRACT_PROFILE == 'project-system-extraction-contract-v1'
    assert PROPOSAL_KINDS == ('requirement', 'decision', 'risk', 'question')
    assert MAX_XCON_BYTES == 4 * 1024 * 1024


def test_central_intake_namespace_knows_stage13b2a_roots():
    assert KNOWN_INTAKE_DIRECTORIES == (
        'representations', 'extraction-contracts', 'extraction-runs',
        'proposals', 'extraction-submissions', 'executions', 'execution-links')


def test_known_intake_siblings_and_stage13b1_only_project_validate(tmp_path):
    root = init_project('Demo', tmp_path / 'project')
    assert not intake_issues(root)
    for name in KNOWN_INTAKE_DIRECTORIES[1:]:
        shutil.rmtree(root / 'intake' / name)
    assert not intake_issues(root)


def test_legacy_project_without_intake_stays_valid(tmp_path):
    root = init_project('Demo', tmp_path / 'project')
    shutil.rmtree(root / 'intake')
    assert not intake_issues(root)


@pytest.mark.parametrize('name', ['unknown-stage', '.gitkeep'])
def test_unknown_top_level_intake_entry_fails_closed(tmp_path, name):
    root = init_project('Demo', tmp_path / 'project')
    path = root / 'intake' / name
    path.mkdir() if '.' not in name else path.write_text('')
    assert any(location == f'intake/{name}' for _, location, _ in intake_issues(root))


def test_unsafe_intake_reparse_fails_closed(tmp_path, monkeypatch):
    root = init_project('Demo', tmp_path / 'project')
    target = root / 'intake/extraction-contracts'
    original = source_layer_module.os.lstat

    def reparse(path, *args, **kwargs):
        info = original(path, *args, **kwargs)
        if Path(path) == target:
            return SimpleNamespace(
                st_mode=info.st_mode, st_file_attributes=0x400,
                st_dev=info.st_dev, st_ino=info.st_ino, st_size=info.st_size,
                st_mtime_ns=info.st_mtime_ns)
        return info

    monkeypatch.setattr(source_layer_module.os, 'lstat', reparse)
    assert any(location == 'intake/extraction-contracts'
               for _, location, _ in intake_issues(root))


def test_intake_symlink_section_fails_closed(tmp_path):
    root = init_project('Demo', tmp_path / 'project')
    section = root / 'intake/extraction-contracts'
    (section / '.gitkeep').unlink()
    section.rmdir()
    try:
        section.symlink_to(tmp_path, target_is_directory=True)
    except OSError:
        pytest.skip('symlink creation unavailable')
    assert intake_issues(root)


def test_deterministic_xcon_default_all_and_exact_identity(tmp_path):
    project = represented_project(tmp_path)
    first = create_extraction_contract(
        project['root'], project['representation']['representation_id'])
    second = create_extraction_contract(
        project['root'], project['representation']['representation_id'])
    path = project['root'] / f'intake/extraction-contracts/{first["contract_id"]}.json'
    document = json.loads(path.read_bytes())
    identity = {
        'profile': EXTRACTION_CONTRACT_PROFILE,
        'project_id': document['project_id'],
        'representation_id': document['representation_id'],
        'presented_segment_ids': document['presented_segment_ids'],
        'allowed_kinds': document['allowed_kinds'],
    }
    assert first['status'] == 'created' and second['status'] == 'existing'
    assert first['contract_id'] == 'XCON-' + sha256(canonical_bytes(identity)).hexdigest()[:32]
    assert first['contract_id'] == extraction_contract_id(document)
    assert document['presented_segment_ids'] == project['segment_ids']
    assert document['allowed_kinds'] == list(PROPOSAL_KINDS)
    assert document['coverage'] == {
        'presented_segments': 3, 'representation_segments': 3, 'complete': True}
    assert document['canonical_authority'] is False


def test_xcon_canonicalizes_cli_segment_and_kind_order(tmp_path):
    project = represented_project(tmp_path)
    reverse_segments = list(reversed(project['segment_ids'][:2]))
    first = create_extraction_contract(
        project['root'], project['representation']['representation_id'],
        segment_ids=reverse_segments, allowed_kinds=['question', 'decision'])
    second = create_extraction_contract(
        project['root'], project['representation']['representation_id'],
        segment_ids=project['segment_ids'][:2], allowed_kinds=['decision', 'question'])
    document = json.loads((project['root'] /
        f'intake/extraction-contracts/{first["contract_id"]}.json').read_bytes())
    assert first['contract_id'] == second['contract_id']
    assert document['presented_segment_ids'] == project['segment_ids'][:2]
    assert document['allowed_kinds'] == ['decision', 'question']
    assert document['coverage'] == {
        'presented_segments': 2, 'representation_segments': 3, 'complete': False}


@pytest.mark.parametrize(('segments', 'kinds', 'message'), [
    ('duplicate', None, 'duplicate --segment'),
    (None, ['risk', 'risk'], 'duplicate --allow-kind'),
    (['SEG-' + 'f' * 32], None, 'does not belong'),
    (None, ['future-kind'], 'unsupported proposal kind'),
    (None, [], 'requires an allowed kind'),
])
def test_xcon_rejects_invalid_selection(tmp_path, segments, kinds, message):
    project = represented_project(tmp_path)
    selected = ([project['segment_ids'][0]] * 2 if segments == 'duplicate' else segments)
    with pytest.raises(ExtractionError, match=message):
        create_extraction_contract(
            project['root'], project['representation']['representation_id'],
            segment_ids=selected, allowed_kinds=kinds)


def test_segment_from_another_rep_is_rejected(tmp_path):
    project = represented_project(tmp_path)
    other_source = tmp_path / 'other.txt'
    other_source.write_bytes(b'other')
    capture = capture_source(
        project['root'], other_source, key='other', provider='generic',
        kind='document', retention='repository_snapshot')
    other = represent_source(project['root'], capture['capture_id'], adapter='utf8-lines')
    other_receipt = project['root'] / f'intake/representations/{other["representation_id"]}.json'
    foreign = json.loads(other_receipt.with_suffix('.segments.jsonl').read_bytes().splitlines()[0])
    with pytest.raises(ExtractionError, match='does not belong'):
        create_extraction_contract(
            project['root'], project['representation']['representation_id'],
            segment_ids=[foreign['segment_id']])


def test_zero_segment_rep_cannot_create_xcon(tmp_path):
    project = represented_project(tmp_path, b'')
    with pytest.raises(ExtractionError, match='zero-segment'):
        create_extraction_contract(
            project['root'], project['representation']['representation_id'])


def test_xcon_bound_requires_explicit_subset_when_default_too_large(tmp_path, monkeypatch):
    project = represented_project(tmp_path)
    monkeypatch.setattr(extraction_module, 'MAX_XCON_SEGMENTS', 1)
    with pytest.raises(ExtractionError, match='segment-count limit'):
        create_extraction_contract(
            project['root'], project['representation']['representation_id'])
    report = create_extraction_contract(
        project['root'], project['representation']['representation_id'],
        segment_ids=[project['segment_ids'][0]])
    assert report['presented_segments'] == 1


def test_xcon_receipt_has_no_semantic_text_or_private_path(tmp_path):
    project = represented_project(tmp_path, b'CUSTOMER_SECRET_927\n')
    report = create_extraction_contract(
        project['root'], project['representation']['representation_id'])
    raw = (project['root'] /
        f'intake/extraction-contracts/{report["contract_id"]}.json').read_bytes()
    assert b'CUSTOMER_SECRET_927' not in raw
    assert project['source'].name.encode() not in raw
    for forbidden in (b'timestamp', b'hostname', b'username', b'pid'):
        assert forbidden not in raw.lower()


def test_xcon_no_clobber_conflict(tmp_path):
    project = represented_project(tmp_path)
    report = create_extraction_contract(
        project['root'], project['representation']['representation_id'])
    path = project['root'] / f'intake/extraction-contracts/{report["contract_id"]}.json'
    path.write_bytes(b'contradiction')
    before = path.read_bytes()
    with pytest.raises(ExtractionError):
        create_extraction_contract(
            project['root'], project['representation']['representation_id'])
    assert path.read_bytes() == before


def test_contract_cli_machine_output_and_duplicate_failure(tmp_path, monkeypatch, capsys):
    project = represented_project(tmp_path)
    monkeypatch.chdir(project['root'])
    main(['source', 'extraction', 'contract', project['representation']['representation_id'],
          '--segment', project['segment_ids'][1], '--segment', project['segment_ids'][0],
          '--allow-kind', 'question', '--allow-kind', 'requirement'])
    report = json.loads(capsys.readouterr().out)
    assert report['presented_segments'] == 2 and report['status'] == 'created'
    with pytest.raises(SystemExit) as error:
        main(['source', 'extraction', 'contract', project['representation']['representation_id'],
              '--segment', project['segment_ids'][0], '--segment', project['segment_ids'][0]])
    assert error.value.code == 2
    assert 'source extraction contract failed' in capsys.readouterr().err


def test_init_adds_only_b2a_directories_and_privacy_ignore(tmp_path):
    root = init_project('Demo', tmp_path / 'project')
    assert sorted(path.name for path in (root / 'intake').iterdir()) == sorted(
        KNOWN_INTAKE_DIRECTORIES)
    for name in KNOWN_INTAKE_DIRECTORIES:
        assert (root / 'intake' / name / '.gitkeep').read_bytes() == b''
    assert 'intake/extraction-submissions/**' in (root / '.llmignore').read_text()


def test_existing_project_ignore_is_not_rewritten_by_contract(tmp_path):
    project = represented_project(tmp_path)
    ignore = project['root'] / '.llmignore'
    ignore.write_text('owner-defined\n', encoding='utf-8')
    create_extraction_contract(
        project['root'], project['representation']['representation_id'])
    assert ignore.read_text(encoding='utf-8') == 'owner-defined\n'
