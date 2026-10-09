"""B1 test-only observations: real publication/crash tests, never real providers."""

from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from extraction_test_helpers import contracted_project
from project_system import execution_attempt as e
from project_system.extraction_pack import create_extraction_pack
from project_system.source_layer import canonical_bytes
from project_system.utils import load_yaml
from project_system.validation import validate


RESPONSE = b'{"fake_orchestration_only":true}'
V2_SLOTS = ('01-prepared.json', '02-boundary.json', '03-outcome.json')


def digest(value):
    return sha256(canonical_bytes(value)).hexdigest()


@pytest.fixture
def project(tmp_path):
    result = contracted_project(tmp_path)
    result['pack'] = create_extraction_pack(result['root'], result['contract']['contract_id'])
    return result


def prepare(project, **kwargs):
    return e.prepare_attempt(project['root'], project['pack']['pack_id'], b'trusted fake instruction',
                             schema_version=2, **kwargs)


def authorize(root, attempt):
    return e.authorize_test_attempt(root, attempt.attempt_id,
                                    expected_contract_sha256=digest(attempt.contract))


def path(root, attempt, index):
    return root / 'intake/executions' / attempt.attempt_id / V2_SLOTS[index]


def rewrite(checkpoint, modify):
    doc = json.loads(checkpoint.read_bytes())
    modify(doc)
    if 'xinv' in doc['data']:
        record = doc['data']['xinv']
        record['invocation_id'] = 'XINV-' + digest({k: v for k, v in record.items()
                                                   if k != 'invocation_id'})[:32]
    doc['checkpoint_sha256'] = digest({k: v for k, v in doc.items()
                                       if k != 'checkpoint_sha256'})
    checkpoint.write_bytes(canonical_bytes(doc))


def test_published_v1_interpretation_and_layout_unchanged(project):
    root = project['root']
    attempt = e.prepare_attempt(root, project['pack']['pack_id'], b'legacy')
    prepared = path(root, attempt, 0).read_bytes()
    assert json.loads(prepared)['profile'] == 'project-system-execution-attempt-test-v1'
    cap = authorize(root, attempt)
    assert e.dispatch_attempt(root, attempt.attempt_id, cap)['state'] == 'DELIVERY_UNKNOWN'
    assert not path(root, attempt, 2).exists()
    result = e.close_test_attempt(root, attempt.attempt_id, cap, outcome='unresolved')
    assert result.state == 'UNRESOLVED'
    assert (path(root, attempt, 0).parent / '03-disposition.json').is_file()
    assert path(root, attempt, 0).read_bytes() == prepared
    assert e.inspect_attempt(root, attempt.attempt_id) == result


def test_v2_prepared_independent_validation_and_abandonment(project):
    root = project['root']
    attempt = prepare(project)
    doc = json.loads(path(root, attempt, 0).read_bytes())
    assert doc['schema_version'] == 2
    assert doc['profile'] == 'project-system-execution-attempt-test-v2'
    assert e.inspect_attempt(root, attempt.attempt_id).state == 'PREPARED'
    cap = authorize(root, attempt)
    result = e.close_test_attempt(root, attempt.attempt_id, cap, outcome='abandoned')
    assert result.state == 'ABANDONED' and result.terminal
    auth = json.loads(path(root, attempt, 1).read_bytes())['data']['authorization']
    assert auth['prepared_sha256'] == doc['checkpoint_sha256']
    assert not path(root, attempt, 2).exists()


def test_response_exact_digest_metadata_only_and_no_semantic_claims(project):
    root = project['root']
    before = {p.relative_to(root): p.read_bytes() for prefix in ('knowledge', 'docs')
              for p in (root / prefix).rglob('*') if p.is_file()}
    attempt = prepare(project)
    report = e.dispatch_attempt(root, attempt.attempt_id, authorize(root, attempt))
    assert report['state'] == 'RESPONSE_RECORDED'
    result = e.inspect_attempt(root, attempt.attempt_id)
    assert result.terminal and not result.semantic_evidence and not result.response_bytes_available
    doc = json.loads(path(root, attempt, 2).read_bytes())
    xinv = doc['data']['xinv']
    assert xinv['response'] == {'sha256': sha256(RESPONSE).hexdigest(), 'bytes': len(RESPONSE)}
    assert xinv['prepared_sha256'] == json.loads(path(root, attempt, 0).read_bytes())['checkpoint_sha256']
    assert xinv['dispatch_intent_sha256'] == json.loads(path(root, attempt, 1).read_bytes())['checkpoint_sha256']
    for field in ('semantic_evidence', 'task_completion_claimed', 'human_approval_verified',
                  'model_authenticity_verified', 'canonical_authority', 'response_bytes_available'):
        assert xinv[field] is False
    assert sorted(p.name for p in path(root, attempt, 0).parent.iterdir()) == list(V2_SLOTS)
    assert all(RESPONSE not in p.read_bytes() for p in root.rglob('*') if p.is_file())
    assert all((root / p).read_bytes() == data for p, data in before.items())
    layer = e.inspect_execution_layer(root, load_yaml(root / 'project.yaml'))
    assert layer.issues[0][0] == 'WARNING' and 'no semantic Evidence' in layer.issues[0][2]
    assert not [issue for issue in validate(root) if issue[0] in {'ERROR', 'BLOCKING'}
                and issue[1].startswith('intake/executions')]


def test_no_caller_response_authority_and_forged_fake_response_rejected(project, monkeypatch):
    root = project['root']
    attempt = prepare(project)
    cap = authorize(root, attempt)
    with pytest.raises(e.AttemptError):
        e.dispatch_attempt(root, attempt.attempt_id, cap, response=RESPONSE)
    assert not path(root, attempt, 1).exists()
    monkeypatch.setattr(e.execution_fake, 'dispatch', lambda _: b'caller forged bytes')
    with pytest.raises(e.AttemptError):
        e.dispatch_attempt(root, attempt.attempt_id, cap)
    assert e.inspect_attempt(root, attempt.attempt_id).state == 'DELIVERY_UNKNOWN'
    assert not path(root, attempt, 2).exists()


def test_no_terminal_overwrite_or_duplicate_dispatch(project):
    root = project['root']
    attempt = prepare(project)
    cap = authorize(root, attempt)
    e.dispatch_attempt(root, attempt.attempt_id, cap)
    original = path(root, attempt, 2).read_bytes()
    for action in (lambda: e.dispatch_attempt(root, attempt.attempt_id, cap),
                   lambda: e.close_test_attempt(root, attempt.attempt_id, cap, outcome='unresolved'),
                   lambda: e._publish(root, attempt.attempt_id, V2_SLOTS[2], json.loads(original))):
        with pytest.raises(e.AttemptError):
            action()
        assert path(root, attempt, 2).read_bytes() == original


@pytest.mark.parametrize('scenario', ['after_intent', 'timeout', 'unknown_delivery', 'malformed_response'])
def test_fixed_fake_unresolved_outcomes(project, scenario):
    root = project['root']
    attempt = prepare(project, options={'scenario': scenario})
    report = e.dispatch_attempt(root, attempt.attempt_id, authorize(root, attempt))
    assert report['state'] == 'UNRESOLVED'
    xinv = json.loads(path(root, attempt, 2).read_bytes())['data']['xinv']
    assert xinv['observation'] == scenario and xinv['response'] is None


@pytest.mark.parametrize('boundary,expected', [
    ('before_publication:03-outcome.json', 'DELIVERY_UNKNOWN'),
    ('after_publication:03-outcome.json', 'RESPONSE_RECORDED'),
    ('after_response_observation', 'DELIVERY_UNKNOWN'),
])
def test_real_process_crash_around_transient_observation_and_terminal(project, tmp_path, boundary, expected):
    root = project['root']
    attempt = prepare(project)
    marker = tmp_path / 'observed'
    script = '''
import os, sys
from pathlib import Path
from project_system import execution_attempt as e
root, ident, boundary, marker = sys.argv[1:]
attempt = e.inspect_attempt(root, ident)
cap = e.authorize_test_attempt(root, ident, expected_contract_sha256=e._digest(attempt.contract))
original = e.execution_fake.dispatch
def fake(contract):
    response = original(contract)
    Path(marker).write_bytes(b'observed without payload')
    return response
e.execution_fake.dispatch = fake
def crash(name):
    if name == boundary:
        os._exit(23)
e._boundary = crash
e.dispatch_attempt(root, ident, cap)
'''
    run = subprocess.run([sys.executable, '-c', script, str(root), attempt.attempt_id,
                          boundary, str(marker)], capture_output=True, text=True, timeout=30)
    assert run.returncode == 23, (run.stdout, run.stderr)
    assert marker.read_bytes() == b'observed without payload'
    result = e.inspect_attempt(root, attempt.attempt_id)
    assert result.state == expected
    assert path(root, attempt, 2).exists() == result.terminal
    with pytest.raises(e.AttemptError):
        e.dispatch_attempt(root, attempt.attempt_id, authorize(root, attempt))
    if not result.terminal:
        cap = authorize(root, result)
        closed = e.close_test_attempt(root, attempt.attempt_id, cap, outcome='unresolved')
        assert closed.state == 'UNRESOLVED'
        assert json.loads(path(root, attempt, 2).read_bytes())['data']['xinv']['response'] is None


def test_two_process_response_vs_unresolved_exactly_one_terminal_winner(project):
    root = project['root']
    attempt = prepare(project)
    script = '''
import sys
from project_system import execution_attempt as e
root, ident, action = sys.argv[1:]
attempt = e.inspect_attempt(root, ident)
cap = e.authorize_test_attempt(root, ident, expected_contract_sha256=e._digest(attempt.contract))
def barrier(name):
    if name == 'before_publication:03-outcome.json':
        print('READY', flush=True)
        assert sys.stdin.readline().strip() == 'GO'
e._boundary = barrier
try:
    if action == 'response':
        e.dispatch_attempt(root, ident, cap)
    else:
        e.close_test_attempt(root, ident, cap, outcome='unresolved')
    print('WON', flush=True)
except e.AttemptError:
    print('REJECTED', flush=True)
'''
    children = []
    pool = ThreadPoolExecutor(max_workers=2)
    try:
        for action in ('response', 'unresolved'):
            child = subprocess.Popen([sys.executable, '-c', script, str(root), attempt.attempt_id, action],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     text=True)
            children.append(child)
            assert pool.submit(child.stdout.readline).result(timeout=30).strip() == 'READY'
        for child in children:
            child.stdin.write('GO\n')
            child.stdin.flush()
        results = [child.communicate(timeout=30) for child in children]
        assert all(child.returncode == 0 for child in children), results
        assert sorted(out.strip() for out, _ in results) == ['REJECTED', 'WON']
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=10)
        pool.shutdown(wait=True)
    assert e.inspect_attempt(root, attempt.attempt_id).state in {'RESPONSE_RECORDED', 'UNRESOLVED'}
    assert sorted(p.name for p in path(root, attempt, 0).parent.iterdir()) == list(V2_SLOTS)


@pytest.mark.parametrize('field,value', [
    ('prepared_sha256', '0' * 64), ('contract_sha256', '0' * 64),
    ('dispatch_intent_sha256', '0' * 64), ('authorization_sha256', '0' * 64),
    ('attempt_id', 'ATTEMPT-' + '0' * 32), ('project_id', 'other'),
    ('response', {'sha256': '0' * 64, 'bytes': len(RESPONSE)}),
    ('response', {'sha256': sha256(RESPONSE).hexdigest(), 'bytes': len(RESPONSE) + 1}),
    ('response', {'sha256': sha256(RESPONSE).hexdigest(), 'bytes': float(len(RESPONSE))}),
    ('observation', 'timeout'), ('schema_version', 1.0),
    ('semantic_evidence', True), ('task_completion_claimed', True),
    ('unknown', 'extra'),
])
def test_rehashed_xinv_bindings_and_protocol_forgery_rejected(project, field, value):
    root = project['root']
    attempt = prepare(project)
    e.dispatch_attempt(root, attempt.attempt_id, authorize(root, attempt))
    rewrite(path(root, attempt, 2), lambda doc: doc['data']['xinv'].update({field: value}))
    with pytest.raises(e.AttemptError):
        e.inspect_attempt(root, attempt.attempt_id)


@pytest.mark.parametrize('index,field', [(0, 'instruction_sha256'), (1, 'authorization_sha256')])
def test_recomputed_prepared_and_boundary_commitments_rejected(project, index, field):
    root = project['root']
    attempt = prepare(project)
    e.dispatch_attempt(root, attempt.attempt_id, authorize(root, attempt))
    def change(doc):
        if index == 0:
            doc['data']['contract'][field] = '0' * 64
            doc['data']['contract_sha256'] = digest(doc['data']['contract'])
        else:
            doc['data']['authorization'][field] = '0' * 64
    rewrite(path(root, attempt, index), change)
    with pytest.raises(e.AttemptError):
        e.inspect_attempt(root, attempt.attempt_id)


@pytest.mark.parametrize('field', ['max_pack_bytes', 'max_response_bytes', 'timeout_seconds'])
def test_v2_float_limits_rejected_before_publication(project, field):
    limits = {'max_pack_bytes': 2097152, 'max_response_bytes': 65536, 'timeout_seconds': 30}
    limits[field] = float(limits[field])
    with pytest.raises(e.AttemptError, match='limits must be integers'):
        prepare(project, limits=limits)
    assert not list((project['root'] / 'intake/executions').glob('ATTEMPT-*'))


@pytest.mark.parametrize('field', ['pack_bytes', 'instruction_bytes'])
def test_v2_rehashed_float_contract_counts_rejected(project, field):
    attempt = prepare(project)
    def change(doc):
        contract = doc['data']['contract']
        contract[field] = float(contract[field])
        doc['data']['contract_sha256'] = digest(contract)
    rewrite(path(project['root'], attempt, 0), change)
    with pytest.raises(e.AttemptError, match='integer'):
        e.inspect_attempt(project['root'], attempt.attempt_id)


@pytest.mark.parametrize('mode', ['wrong_slot', 'mixed_profile', 'missing_intent', 'duplicate_key'])
def test_v2_mixed_or_malformed_layouts_independently_rejected(project, mode):
    root = project['root']
    attempt = prepare(project)
    e.dispatch_attempt(root, attempt.attempt_id, authorize(root, attempt))
    terminal = path(root, attempt, 2)
    if mode == 'wrong_slot':
        terminal.rename(terminal.with_name('03-disposition.json'))
    elif mode == 'mixed_profile':
        rewrite(terminal, lambda doc: doc.update(schema_version=1,
                                               profile='project-system-execution-attempt-test-v1'))
    elif mode == 'missing_intent':
        path(root, attempt, 1).unlink()
    else:
        raw = terminal.read_bytes()
        terminal.write_bytes(b'{"schema_version":2,' + raw[1:])
    with pytest.raises(e.AttemptError):
        e.inspect_attempt(root, attempt.attempt_id)
    assert e.inspect_execution_layer(root, load_yaml(root / 'project.yaml')).issues[0][0] == 'ERROR'


@pytest.mark.parametrize('value', [None, [], '2', 2.0, True, 3])
def test_invalid_version_rejected_before_directory_creation(project, value):
    with pytest.raises(e.AttemptError):
        e.prepare_attempt(project['root'], project['pack']['pack_id'], b'fake', schema_version=value)
    assert not list((project['root'] / 'intake/executions').glob('ATTEMPT-*'))


@pytest.mark.parametrize('field,value', [
    ('retention', 'repository_snapshot'), ('allowed_destination', 'remote'),
    ('adapter', 'real-model'), ('model', 'real-model'), ('options', {'unknown': True}),
])
def test_v2_authority_retention_and_options_fail_closed(project, field, value):
    with pytest.raises(e.AttemptError):
        prepare(project, **{field: value})
    assert not list((project['root'] / 'intake/executions').glob('ATTEMPT-*'))


@pytest.mark.parametrize('mode', ['other_attempt', 'dict', 'wrong_prepared_hash'])
def test_v2_capability_cannot_be_substituted(project, mode):
    root = project['root']
    attempt = prepare(project)
    cap = authorize(root, attempt)
    if mode == 'other_attempt':
        cap = authorize(root, prepare(project))
    elif mode == 'dict':
        cap = {'approved': True, 'contract_sha256': digest(attempt.contract)}
    else:
        cap = e._TestAuthorization(cap.contract_sha256, cap.signature, '0' * 64)
    with pytest.raises(e.AttemptError):
        e.dispatch_attempt(root, attempt.attempt_id, cap)
    assert e.inspect_attempt(root, attempt.attempt_id).state == 'PREPARED'
    assert not path(root, attempt, 1).exists()


def test_v2_response_over_limit_has_no_success_commitment(project):
    root = project['root']
    attempt = prepare(project, limits={'max_pack_bytes': 2097152,
                                       'max_response_bytes': 1, 'timeout_seconds': 30})
    assert e.dispatch_attempt(root, attempt.attempt_id, authorize(root, attempt))['state'] == 'UNRESOLVED'
    xinv = json.loads(path(root, attempt, 2).read_bytes())['data']['xinv']
    assert xinv['response'] is None and xinv['observation'] == 'malformed_response'


def test_v2_unsupported_terminal_link_fails_without_fallback_or_retry(project, monkeypatch):
    root = project['root']
    attempt = prepare(project)
    cap = authorize(root, attempt)
    original = os.link
    def unsupported(source, destination, *args, **kwargs):
        if Path(destination).name == V2_SLOTS[2]:
            raise OSError('unsupported hard link')
        return original(source, destination, *args, **kwargs)
    monkeypatch.setattr(e.os, 'link', unsupported)
    with pytest.raises(e.AttemptError, match='no-clobber publication unsupported'):
        e.dispatch_attempt(root, attempt.attempt_id, cap)
    assert not path(root, attempt, 2).exists()
    assert not list((root / '.generated/execution-staging').iterdir())
    assert e.inspect_attempt(root, attempt.attempt_id).state == 'DELIVERY_UNKNOWN'
    with pytest.raises(e.AttemptError, match='no redispatch'):
        e.dispatch_attempt(root, attempt.attempt_id, cap)


def test_v2_independent_validation_does_not_require_disposable_xpack(project):
    root = project['root']
    attempt = prepare(project)
    e.dispatch_attempt(root, attempt.attempt_id, authorize(root, attempt))
    cache = root / '.generated/source-extraction-packs' / project['pack']['pack_id'] / 'pack.json'
    cache.unlink()
    assert e.inspect_attempt(root, attempt.attempt_id).state == 'RESPONSE_RECORDED'


@pytest.mark.parametrize('field,value', [
    ('schema_version', 2.0), ('data', []), ('data', {'xinv': 'forged'}),
    ('previous_sha256', None), ('project_id', 'wrong'),
])
def test_rehashed_invalid_terminal_envelope_is_controlled(project, field, value):
    root = project['root']
    attempt = prepare(project)
    e.dispatch_attempt(root, attempt.attempt_id, authorize(root, attempt))
    terminal = path(root, attempt, 2)
    doc = json.loads(terminal.read_bytes())
    doc[field] = value
    doc['checkpoint_sha256'] = digest({k: v for k, v in doc.items() if k != 'checkpoint_sha256'})
    terminal.write_bytes(canonical_bytes(doc))
    with pytest.raises(e.AttemptError):
        e.inspect_attempt(root, attempt.attempt_id)
