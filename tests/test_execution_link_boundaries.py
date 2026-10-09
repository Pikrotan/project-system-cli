"""Additional B3 publication, independent replay and strict boundary proofs."""
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from extraction_test_helpers import proposal, seal, submission
from project_system import execution_attempt as e
from project_system.source_layer import canonical_bytes
from test_execution_link import project, prepare, cap, complete, create, path, blob, errors, module


def test_valid_but_different_xrun_cannot_match_predeclared_hashes(project, tmp_path):
    a, _, _, _ = complete(project, tmp_path)
    raw = submission([proposal('risk', 'different untrusted proposal',
                               [project['segment_ids'][0]], 'ambiguous')])
    _, other, _, _ = seal(project, tmp_path, raw,
                          instruction_sha=a.contract['instruction_sha256'], filename='other.json')
    with pytest.raises(module().LinkError, match='actual normalized response'):
        create(project, a, other)


def test_inspection_repeats_actual_raw_normalization(project, tmp_path, monkeypatch):
    a, raw, run, _ = complete(project, tmp_path)
    calls = []
    original = module().normalize_submission
    def observed(actual, contract):
        assert actual == raw and contract == project['contract_receipt']
        calls.append(True)
        return original(actual, contract)
    monkeypatch.setattr(module(), 'normalize_submission', observed)
    result = create(project, a, run)
    assert calls
    calls.clear()
    module().inspect_execution_link(project['root'], result.link_id)
    assert calls


@pytest.mark.parametrize('field', ['schema_version', 'attempt_version', 'raw_bytes',
                                  'normalized_bytes', 'proposal_count'])
def test_integral_float_link_metadata_rejected(project, tmp_path, field):
    a, _, run, _ = complete(project, tmp_path)
    result = create(project, a, run)
    target = path(project, result)
    doc = json.loads(target.read_bytes())
    owner, key = {
        'schema_version': (doc, 'schema_version'),
        'attempt_version': (doc['attempt'], 'schema_version'),
        'raw_bytes': (doc['raw_response'], 'bytes'),
        'normalized_bytes': (doc['normalized_submission'], 'bytes'),
        'proposal_count': (doc['normalized_submission'], 'proposals'),
    }[field]
    owner[key] = float(owner[key])
    doc['link_id'] = module().link_identity(doc)
    target.unlink()
    target.with_name(doc['link_id'] + '.json').write_bytes(canonical_bytes(doc))
    with pytest.raises(module().LinkError, match='exact integers'):
        module().inspect_execution_link(project['root'], doc['link_id'])


@pytest.mark.parametrize('field', ['max_pack_bytes', 'max_response_bytes', 'timeout_seconds'])
def test_v4_float_limits_rejected_before_publication(project, field):
    limits = {'max_pack_bytes': 2097152, 'max_response_bytes': 65536, 'timeout_seconds': 30}
    limits[field] = float(limits[field])
    with pytest.raises(e.AttemptError, match='integers'):
        e.prepare_attempt(project['root'], project['pack']['pack_id'], b'instruction',
                          schema_version=4, retention='sealed_local',
                          adapter='deterministic-extraction-fixture', model='synthetic-not-ai', limits=limits)
    assert not list((project['root'] / 'intake/executions').glob('ATTEMPT-*'))


def test_v4_abandonment_preserved(project):
    a = prepare(project)
    result = e.close_test_attempt(project['root'], a.attempt_id, cap(project, a), outcome='abandoned')
    assert result.state == 'ABANDONED' and result.terminal and not blob(project, a).exists()


@pytest.mark.parametrize('point', ['after_response_observation', 'after_blob_publication'])
def test_v4_real_crash_never_fabricates_link_or_retries(project, point):
    a = prepare(project)
    script = """
import os, sys
from project_system import execution_attempt as e
root, ident, point = sys.argv[1:]
a = e.inspect_attempt(root, ident)
cap = e.authorize_test_attempt(root, ident, expected_contract_sha256=e._digest(a.contract))
def crash(name):
    if name == point:
        os._exit(23)
e._boundary = crash
e.dispatch_attempt(root, ident, cap)
"""
    crashed = subprocess.run([sys.executable, '-c', script, str(project['root']),
                              a.attempt_id, point], capture_output=True, text=True, timeout=30)
    assert crashed.returncode == 23, (crashed.stdout, crashed.stderr)
    assert e.inspect_attempt(project['root'], a.attempt_id).state == 'DELIVERY_UNKNOWN'
    with pytest.raises(e.AttemptError, match='no redispatch'):
        e.dispatch_attempt(project['root'], a.attempt_id, cap(project, a))
    assert not list((project['root'] / 'intake/execution-links').glob('XLINK-*'))


@pytest.mark.parametrize('kind', ['symlink', 'reparse'])
def test_unsafe_payload_links_rejected(project, tmp_path, monkeypatch, kind):
    a, raw, run, _ = complete(project, tmp_path)
    result = create(project, a, run)
    target = blob(project, a)
    if kind == 'symlink':
        outside = tmp_path / 'outside-response'
        outside.write_bytes(raw)
        target.unlink()
        try:
            target.symlink_to(outside)
        except OSError:
            pytest.skip('symlink creation unavailable for this Windows user')
    else:
        original = os.lstat
        def reparse(p, *args, **kwargs):
            info = original(p, *args, **kwargs)
            if Path(p) == target:
                values = {k: getattr(info, k) for k in dir(info) if k.startswith('st_')}
                values['st_file_attributes'] = 0x400
                return SimpleNamespace(**values)
            return info
        monkeypatch.setattr(os, 'lstat', reparse)
    with pytest.raises(module().LinkError):
        module().inspect_execution_link(project['root'], result.link_id)
    assert any(x[0] == 'ERROR' for x in errors(project))


@pytest.mark.parametrize('mode', ['race', 'unsupported'])
def test_atomic_fsync_no_clobber_and_own_staging_cleanup(project, tmp_path, monkeypatch, mode):
    a, _, run, _ = complete(project, tmp_path)
    synced, attempted = set(), []
    original_sync, original_link = os.fsync, os.link
    def sync(fd):
        info = os.fstat(fd)
        synced.add((info.st_dev, info.st_ino))
        return original_sync(fd)
    def link(source, destination, *args, **kwargs):
        if Path(destination).parent.name == 'execution-links':
            info = os.lstat(source)
            assert (info.st_dev, info.st_ino) in synced
            module()._parse(Path(source).read_bytes())
            if mode == 'unsupported':
                raise NotImplementedError
            Path(destination).write_bytes(b'unrelated writer bytes')
            attempted.append(Path(destination))
        return original_link(source, destination, *args, **kwargs)
    monkeypatch.setattr(os, 'fsync', sync)
    monkeypatch.setattr(os, 'link', link)
    monkeypatch.setattr(os, 'replace', lambda *_: pytest.fail('clobber fallback'))
    with pytest.raises(module().LinkError, match='no-clobber'):
        create(project, a, run)
    if mode == 'race':
        assert attempted[0].read_bytes() == b'unrelated writer bytes'
    else:
        assert not list((project['root'] / 'intake/execution-links').glob('XLINK-*'))
    assert not list((project['root'] / '.generated/execution-link-staging').iterdir())


@pytest.mark.parametrize('value', ['../XLINK-' + '0' * 32, 'D:/outside.json',
                                   'XLINK-' + '0' * 32 + '/other', [], None])
def test_selector_traversal_and_invalid_identity(project, value):
    with pytest.raises(module().LinkError):
        module().inspect_execution_link(project['root'], value)
@pytest.mark.parametrize('mode', ['id', 'unknown_field', 'missing_attempt', 'authorization', 'xinv_id'])
def test_forged_identity_or_orphan_link_rejected(project, tmp_path, mode):
    a, _, run, _ = complete(project, tmp_path)
    result = create(project, a, run)
    target = path(project, result)
    doc = json.loads(target.read_bytes())
    if mode == 'missing_attempt':
        shutil = __import__('shutil')
        shutil.rmtree(project['root'] / 'intake/executions' / a.attempt_id)
    else:
        if mode == 'id':
            doc['link_id'] = 'XLINK-' + '0' * 32
        elif mode == 'unknown_field':
            doc['unexpected'] = True
        elif mode == 'authorization':
            doc['attempt']['authorization_sha256'] = '0' * 64
        else:
            doc['invocation']['id'] = 'XINV-' + '0' * 32
        if mode != 'id':
            doc['link_id'] = module().link_identity(doc)
            target.unlink()
            target = target.with_name(doc['link_id'] + '.json')
        target.write_bytes(canonical_bytes(doc))
    with pytest.raises(module().LinkError):
        module().inspect_execution_link(project['root'], doc['link_id'])
    assert any(x[0] == 'ERROR' for x in errors(project))


def test_present_payload_after_structural_inspection_is_revalidated(project, tmp_path, monkeypatch):
    a, _, run, _ = complete(project, tmp_path)
    result = create(project, a, run)
    original = e.inspect_attempt
    reads = []
    def swap(*args, **kwargs):
        state = original(*args, **kwargs)
        if not reads:
            reads.append(True)
            blob(project, a).write_bytes(b'replaced after inspection')
        return state
    monkeypatch.setattr(e, 'inspect_attempt', swap)
    with pytest.raises(module().LinkError):
        module().inspect_execution_link(project['root'], result.link_id)
