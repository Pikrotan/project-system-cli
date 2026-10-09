"""B3 real receipts and test-only content provenance, never AI authenticity."""
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest
from jsonschema import Draft202012Validator

from extraction_test_helpers import contracted_project, seal
from project_system import execution_attempt as e
from project_system.extraction_layer import extraction_run_id, normalize_submission
from project_system.extraction_pack import create_extraction_pack
from project_system.source_layer import canonical_bytes
from project_system.utils import distribution_root
from project_system.validation import validate


def module():
    from project_system import execution_link
    return execution_link


@pytest.fixture
def project(tmp_path):
    p = contracted_project(tmp_path, kinds=['risk', 'question'])
    p['pack'] = create_extraction_pack(p['root'], p['contract']['contract_id'])
    return p


def prepare(p):
    return e.prepare_attempt(
        p['root'], p['pack']['pack_id'], b'synthetic instruction', schema_version=4,
        retention='sealed_local', adapter='deterministic-extraction-fixture', model='synthetic-not-ai')


def cap(p, a):
    return e.authorize_test_attempt(p['root'], a.attempt_id,
                                    expected_contract_sha256=e._digest(a.contract))


def complete(p, tmp_path):
    a = prepare(p)
    e.dispatch_attempt(p['root'], a.attempt_id, cap(p, a))
    raw = e.read_response(p['root'], a.attempt_id)
    # Separate established sealer; required "ai" is declared, NOT authenticated.
    _, run, props, _ = seal(p, tmp_path, raw, provider='test-declared-unverified',
                           model='synthetic-not-ai', instruction_sha=a.contract['instruction_sha256'])
    return a, raw, run, props


def create(p, a, run):
    return module().create_execution_link(p['root'], a.attempt_id, run['run_id'])


def path(p, result):
    return p['root'] / 'intake/execution-links' / (result.link_id + '.json')


def blob(p, a):
    return p['root'] / '.project-local/storage/executions' / a.attempt_id / 'response.bin'


def errors(p):
    return [x for x in validate(p['root']) if x[1].startswith('intake/execution-links')]


@pytest.mark.parametrize('version', [1, 2, 3])
def test_published_profiles_keep_fake_protocol(project, version):
    a = e.prepare_attempt(project['root'], project['pack']['pack_id'], b'legacy',
                          schema_version=version, retention='metadata_only')
    report = e.dispatch_attempt(project['root'], a.attempt_id, cap(project, a))
    assert report['state'] == ('DELIVERY_UNKNOWN' if version == 1 else 'RESPONSE_RECORDED')
    if version > 1:
        terminal = project['root'] / 'intake/executions' / a.attempt_id / '03-outcome.json'
        assert json.loads(terminal.read_bytes())['data']['xinv']['response'] == e.execution_fake.response_commitment()
    assert not blob(project, a).exists()


def test_positive_actual_normalization_and_no_authority(project, tmp_path):
    root = project['root']
    protected = {p: p.read_bytes() for prefix in ('knowledge', 'docs', '.github')
                 for p in (root / prefix).rglob('*') if p.is_file()}
    protected[root / 'project.yaml'] = (root / 'project.yaml').read_bytes()
    a, raw, run, props = complete(project, tmp_path)
    before = {p: p.read_bytes() for p in (root / 'intake').rglob('*') if p.is_file()}
    normalized, records = normalize_submission(raw, project['contract_receipt'])
    assert raw != normalized and sha256(raw).digest() != sha256(normalized).digest()
    assert run['run_id'] == extraction_run_id(run)
    proposal = json.loads(raw)['proposals'][0]
    assert proposal['kind'] == 'risk'
    assert proposal['evidence_segment_ids'] == [project['contract_receipt']['presented_segment_ids'][0]]
    assert proposal['support'] == 'ambiguous' and 'SYNTHETIC' in proposal['statement']
    result = create(project, a, run)
    doc = json.loads(path(project, result).read_bytes())
    assert result.reproducibility == 'AVAILABLE'
    assert doc['link_id'] == module().link_identity(doc)
    assert doc['raw_response'] == {'sha256': sha256(raw).hexdigest(), 'bytes': len(raw)}
    assert doc['normalized_submission']['sha256'] == sha256(normalized).hexdigest()
    assert doc['normalized_submission']['bytes'] == len(normalized)
    assert [item['id'] for item in doc['proposals']] == run['proposal_ids']
    for flag in ('canonical_authority', 'semantic_evidence', 'executor_attribution_verified',
                 'model_authenticity_verified', 'human_approval_verified', 'task_completion_claimed'):
        assert doc[flag] is False
    assert doc['declared_executor'] == run['executor']
    assert b'SYNTHETIC' not in path(project, result).read_bytes()
    assert all(p.read_bytes() == value for p, value in {**protected, **before}.items())
    assert module().inspect_execution_link(root, result.link_id) == result
    assert not [x for x in validate(root) if x[0] in {'ERROR', 'BLOCKING'}]


@pytest.mark.parametrize('field', ['project_id', 'attempt', 'invocation', 'contract', 'run',
                                  'proposals', 'raw_response', 'normalized_submission',
                                  'declared_executor', 'executor_attribution_verified'])
def test_rehashed_forged_link_is_rejected(project, tmp_path, field):
    a, _, run, _ = complete(project, tmp_path)
    result = create(project, a, run)
    target = path(project, result)
    doc = json.loads(target.read_bytes())
    if field == 'project_id':
        doc[field] = 'other'
    elif field == 'executor_attribution_verified':
        doc[field] = True
    elif field == 'declared_executor':
        doc[field]['provider'] = 'forged'
    elif field == 'proposals':
        doc[field][0]['sha256'] = '0' * 64
    elif field == 'raw_response':
        doc[field]['bytes'] = float(doc[field]['bytes'])
    elif field == 'normalized_submission':
        doc[field]['sha256'] = '0' * 64
    else:
        key = {'attempt': 'prepared_sha256', 'invocation': 'terminal_sha256',
               'contract': 'sha256', 'run': 'sha256'}[field]
        doc[field][key] = '0' * 64
    doc['link_id'] = module().link_identity(doc)
    target.unlink()
    target.with_name(doc['link_id'] + '.json').write_bytes(canonical_bytes(doc))
    with pytest.raises(module().LinkError):
        module().inspect_execution_link(project['root'], doc['link_id'])
    assert any(x[0] == 'ERROR' for x in errors(project))


def test_missing_local_transfer_has_only_structural_proof(project, tmp_path, monkeypatch):
    a, _, run, _ = complete(project, tmp_path)
    result = create(project, a, run)
    original = path(project, result).read_bytes()
    shutil.rmtree(project['root'] / '.project-local')
    monkeypatch.setattr(module(), 'normalize_submission', lambda *_: pytest.fail('raw missing'))
    assert module().inspect_execution_link(project['root'], result.link_id).reproducibility == 'UNAVAILABLE'
    assert any(x[0] == 'WARNING' and 'unavailable' in x[2] for x in errors(project))
    assert not [x for x in errors(project) if x[0] == 'ERROR']
    with pytest.raises(module().LinkError):
        create(project, a, run)
    assert path(project, result).read_bytes() == original


@pytest.mark.parametrize('point,published', [('before_link_publication', False),
                                           ('after_link_publication', True)])
def test_real_crash_before_and_after_link(project, tmp_path, point, published):
    a, _, run, _ = complete(project, tmp_path)
    script = """
import os, sys
from project_system import execution_link as m
root, attempt, run, point = sys.argv[1:]
def crash(name):
    if name == point:
        os._exit(23)
m._boundary = crash
m.create_execution_link(root, attempt, run)
"""
    crashed = subprocess.run([sys.executable, '-c', script, str(project['root']),
                              a.attempt_id, run['run_id'], point],
                             capture_output=True, text=True, timeout=30)
    assert crashed.returncode == 23, (crashed.stdout, crashed.stderr)
    links = list((project['root'] / 'intake/execution-links').glob('XLINK-*'))
    assert len(links) == int(published)
    assert not [x for x in errors(project) if x[0] == 'ERROR']
    if published:
        assert module().inspect_execution_link(project['root'], links[0].stem).reproducibility == 'AVAILABLE'
    else:
        create(project, a, run)  # Mutex must be crash released; staging is not a link.


def test_new_schemas_packaged_strict():
    for name in ('execution-attempt-v4.schema.json', 'execution-link.schema.json'):
        schema = json.loads((distribution_root() / 'schemas' / name).read_bytes())
        Draft202012Validator.check_schema(schema)
        assert schema['additionalProperties'] is False
        assert set(schema['required']) == set(schema['properties'])



@pytest.mark.parametrize('mode', ['invalid', 'seg', 'kind'])
def test_forged_fixture_output_rejected(project, monkeypatch, mode):
    from project_system import execution_fixture as fixture
    a = prepare(project)
    raw = fixture.response_bytes(project['root'], a.contract['contract_id'])
    doc = json.loads(raw)
    if mode == 'invalid':
        raw = b'{}'
    else:
        doc['proposals'][0]['evidence_segment_ids' if mode == 'seg' else 'kind'] = (
            ['SEG-' + '0' * 32] if mode == 'seg' else 'decision')
        raw = canonical_bytes(doc)
    monkeypatch.setattr(fixture, 'dispatch', lambda *_: raw)
    with pytest.raises(e.AttemptError):
        e.dispatch_attempt(project['root'], a.attempt_id, cap(project, a))
    assert e.inspect_attempt(project['root'], a.attempt_id).state == 'DELIVERY_UNKNOWN'
    assert not blob(project, a).exists()


@pytest.mark.parametrize('field', ['sha256', 'bytes', 'proposals', 'proposal_manifest_sha256'])
def test_wrong_xrun_commitments_rejected(project, tmp_path, field):
    a, _, run, _ = complete(project, tmp_path)
    old = project['root'] / ('intake/extraction-runs/' + run['run_id'] + '.json')
    run['submission'][field] = '0' * 64 if 'sha256' in field else run['submission'][field] + 1
    run['run_id'] = extraction_run_id(run)
    old.unlink()
    old.with_name(run['run_id'] + '.json').write_bytes(canonical_bytes(run))
    with pytest.raises(module().LinkError):
        create(project, a, run)
    assert not list((project['root'] / 'intake/execution-links').glob('XLINK-*'))


@pytest.mark.parametrize('mode', ['missing_prop', 'wrong_prop', 'wrong_list', 'xcon', 'xinv', 'instruction'])
def test_changed_upstream_receipts_rejected(project, tmp_path, mode):
    a, _, run, props = complete(project, tmp_path)
    root = project['root']
    if mode.endswith('prop'):
        target = root / ('intake/proposals/' + props[0]['proposal_id'] + '.json')
        if mode == 'missing_prop':
            target.unlink()
        else:
            props[0]['payload_sha256'] = '0' * 64
            target.write_bytes(canonical_bytes(props[0]))
    elif mode == 'wrong_list':
        run['proposal_ids'] = ['PROP-' + '0' * 32]
        (root / ('intake/extraction-runs/' + run['run_id'] + '.json')).write_bytes(canonical_bytes(run))
    elif mode == 'instruction':
        _, run, _, _ = seal(project, tmp_path, e.read_response(root, a.attempt_id),
                            instruction_sha='0' * 64, filename='wrong.json')
    elif mode == 'xcon':
        target = root / ('intake/extraction-contracts/' + a.contract['contract_id'] + '.json')
        target.write_bytes(target.read_bytes() + b' ')
    else:
        target = root / 'intake/executions' / a.attempt_id / '03-outcome.json'
        doc = json.loads(target.read_bytes())
        doc['data']['xinv']['response']['sha256'] = '0' * 64
        record = doc['data']['xinv']
        record['invocation_id'] = 'XINV-' + e._digest({k: v for k, v in record.items() if k != 'invocation_id'})[:32]
        doc['checkpoint_sha256'] = e._digest({k: v for k, v in doc.items() if k != 'checkpoint_sha256'})
        target.write_bytes(canonical_bytes(doc))
    with pytest.raises(module().LinkError):
        create(project, a, run)


@pytest.mark.parametrize('mode', ['corrupt', 'substitute', 'directory'])
def test_bad_present_payload_fails_closed(project, tmp_path, mode):
    a, raw, run, _ = complete(project, tmp_path)
    result = create(project, a, run)
    target = blob(project, a)
    if mode == 'directory':
        target.unlink()
        target.mkdir()
    else:
        target.write_bytes(b'x' * (len(raw) if mode == 'substitute' else 3))
    with pytest.raises(module().LinkError):
        module().inspect_execution_link(project['root'], result.link_id)
    assert any(x[0] == 'ERROR' for x in errors(project))


def test_duplicate_conflict_and_unknown_layout_fail_closed(project, tmp_path):
    a, raw, run, _ = complete(project, tmp_path)
    result = create(project, a, run)
    original = path(project, result).read_bytes()
    with pytest.raises(module().LinkError):
        create(project, a, run)
    _, other, _, _ = seal(project, tmp_path, raw, provider='other-declared',
                          instruction_sha=a.contract['instruction_sha256'], filename='other.json')
    with pytest.raises(module().LinkError):
        create(project, a, other)
    assert path(project, result).read_bytes() == original
    doc = json.loads(original)
    doc['run'] = {'id': other['run_id'], 'sha256': sha256((project['root'] /
        ('intake/extraction-runs/' + other['run_id'] + '.json')).read_bytes()).hexdigest()}
    doc['declared_executor'] = other['executor']
    doc['proposals'] = [{'id': ident, 'sha256': sha256((project['root'] /
        ('intake/proposals/' + ident + '.json')).read_bytes()).hexdigest()}
        for ident in other['proposal_ids']]
    doc['link_id'] = module().link_identity(doc)
    path(project, result).with_name(doc['link_id'] + '.json').write_bytes(canonical_bytes(doc))
    assert any(x[0] == 'ERROR' and 'duplicate' in x[2] for x in errors(project))
    with pytest.raises(module().LinkError):
        module().inspect_execution_link(project['root'], result.link_id)
    (path(project, result).parent / 'unknown.json').write_bytes(b'{}')
    assert any(x[0] == 'ERROR' for x in errors(project))


def test_real_concurrent_different_links_have_one_winner(project, tmp_path):
    a, raw, run, _ = complete(project, tmp_path)
    _, other, _, _ = seal(project, tmp_path, raw, provider='other-declared',
                          instruction_sha=a.contract['instruction_sha256'], filename='other.json')
    script = """
import sys
from project_system import execution_link as m
root, attempt, run = sys.argv[1:]
print('READY', flush=True)
assert sys.stdin.readline().strip() == 'GO'
try:
    m.create_execution_link(root, attempt, run)
    print('WON', flush=True)
except m.LinkError:
    print('REJECTED', flush=True)
"""
    children = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        try:
            for run_doc in (run, other):
                child = subprocess.Popen([sys.executable, '-c', script, str(project['root']),
                    a.attempt_id, run_doc['run_id']], stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                children.append(child)
                assert pool.submit(child.stdout.readline).result(timeout=30).strip() == 'READY'
            for child in children:
                child.stdin.write('GO\n')
                child.stdin.flush()
            outputs = [child.communicate(timeout=30) for child in children]
            assert all(c.returncode == 0 for c in children), outputs
            assert sorted(out.strip() for out, _ in outputs) == ['REJECTED', 'WON']
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=10)
    assert len(list((project['root'] / 'intake/execution-links').glob('XLINK-*'))) == 1
    assert not [x for x in errors(project) if x[0] == 'ERROR']
