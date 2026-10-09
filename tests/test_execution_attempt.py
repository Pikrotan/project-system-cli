"""Stage A orchestration/independent persistence tests; no model/network calls."""

from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
from threading import Barrier
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator

from extraction_test_helpers import contracted_project
from project_system import execution_attempt as execution
from project_system.extraction_pack import create_extraction_pack
from project_system.init_project import init_project
from project_system.source_layer import canonical_bytes
from project_system.utils import distribution_root, load_yaml
from project_system.validation import validate


def digest(document):
    return sha256(canonical_bytes(document)).hexdigest()


@pytest.fixture
def project(tmp_path):
    result = contracted_project(tmp_path)
    result['pack'] = create_extraction_pack(result['root'], result['contract']['contract_id'])
    return result


def prepare(project, **kwargs):
    return execution.prepare_attempt(project['root'], project['pack']['pack_id'],
                                     b'test-only trusted instruction', **kwargs)


def authorize(root, attempt):
    return execution.authorize_test_attempt(
        root, attempt.attempt_id, expected_contract_sha256=digest(attempt.contract))


def checkpoint(root, attempt, slot=0):
    return root / 'intake/executions' / attempt.attempt_id / execution.SLOTS[slot]


def rewrite(path, modify):
    document = json.loads(path.read_bytes())
    modify(document)
    document['checkpoint_sha256'] = digest({
        key: value for key, value in document.items() if key != 'checkpoint_sha256'})
    path.write_bytes(canonical_bytes(document))


def test_process_crash_before_prepared_publication_is_diagnosed_orphan(project, tmp_path):
    root = project['root']
    marker = tmp_path / 'unexpected-transport'
    script = '''
import os, sys
from pathlib import Path
from project_system import execution_attempt as e
root, pack_id, marker = sys.argv[1:]
def transport(contract):
    Path(marker).write_bytes(b'called')
    return b'fake'
e.execution_fake.dispatch = transport
def boundary(name):
    if name == 'before_publication:01-prepared.json':
        attempts = list((Path(root) / 'intake/executions').glob('ATTEMPT-*'))
        assert len(attempts) == 1 and not list(attempts[0].iterdir())
        print(attempts[0].name, flush=True)
        os._exit(23)
e._boundary = boundary
e.prepare_attempt(root, pack_id, b'test-only trusted instruction')
'''
    crashed = subprocess.run(
        [sys.executable, '-c', script, str(root), project['pack']['pack_id'], str(marker)],
        capture_output=True, text=True, timeout=30)
    assert crashed.returncode == 23, (crashed.stdout, crashed.stderr)
    ident = crashed.stdout.strip()
    assert execution.ATTEMPT_RE.fullmatch(ident)
    directory = root / 'intake/executions' / ident
    assert directory.is_dir() and not list(directory.iterdir())
    staging = list((root / '.generated/execution-staging').iterdir())
    assert len(staging) == 1
    staged_bytes = staging[0].read_bytes()
    assert json.loads(staged_bytes)['state'] == 'PREPARED'
    assert not marker.exists()
    # Empty cannot prove a benign interruption rather than loss of old receipts.
    diagnostic = 'orphan ATTEMPT directory without PREPARED;.*explicit operator intervention'
    for operation in [
        lambda: execution.inspect_attempt(root, ident),
        lambda: execution.authorize_test_attempt(root, ident, expected_contract_sha256='0' * 64),
        lambda: execution.dispatch_attempt(root, ident, None),
        lambda: execution.close_test_attempt(root, ident, None, outcome='abandoned'),
    ]:
        with pytest.raises(execution.AttemptError, match=diagnostic):
            operation()
    layer = execution.inspect_execution_layer(root, load_yaml(root / 'project.yaml'))
    assert not layer.attempts
    assert len(layer.issues) == 1 and layer.issues[0][0] == 'ERROR'
    assert 'orphan ATTEMPT directory without PREPARED' in layer.issues[0][2]
    assert 'explicit operator intervention' in layer.issues[0][2]
    assert layer.issues[0] in validate(root)
    assert directory.is_dir() and not list(directory.iterdir())
    assert staging[0].read_bytes() == staged_bytes and not marker.exists()


@pytest.mark.parametrize('field', ['pack_bytes', 'instruction_bytes'])
def test_rehashed_integral_float_byte_count_rejected(project, field):
    root = project['root']
    attempt = prepare(project)
    path = checkpoint(root, attempt)
    def tamper(doc):
        contract = doc['data']['contract']
        contract[field] = float(contract[field])
        doc['data']['contract_sha256'] = digest(contract)
    rewrite(path, tamper)
    raw = path.read_bytes()
    # Prove canonical JSON, schema and both hashes still pass before semantic checks.
    document = execution._read_checkpoint(path)
    assert type(document['data']['contract'][field]) is float
    assert document['data']['contract_sha256'] == digest(document['data']['contract'])
    with pytest.raises(execution.AttemptError, match=f'execution {field} must be an integer'):
        execution.inspect_attempt(root, attempt.attempt_id)
    layer = execution.inspect_execution_layer(root, load_yaml(root / 'project.yaml'))
    assert not layer.attempts and layer.issues[0][0] == 'ERROR'
    with pytest.raises(execution.AttemptError):
        execution.authorize_test_attempt(root, attempt.attempt_id,
            expected_contract_sha256=document['data']['contract_sha256'])
    assert path.read_bytes() == raw and not checkpoint(root, attempt, 1).exists()


def test_prepare_unique_exact_commitments_no_source_text_or_canonical_writes(project):
    root = project['root']
    protected = {p: p.read_bytes() for directory in ['knowledge', 'docs', '.github']
                 for p in (root / directory).rglob('*') if p.is_file()}
    protected[root / 'project.yaml'] = (root / 'project.yaml').read_bytes()
    first, second = prepare(project), prepare(project)
    assert first.attempt_id != second.attempt_id
    assert execution.ATTEMPT_RE.fullmatch(first.attempt_id)
    assert first.state == 'PREPARED' and not first.semantic_evidence
    raw = checkpoint(root, first).read_bytes()
    assert b'first' not in raw and b'trusted instruction' not in raw
    assert first.contract['pack_sha256'] == project['pack']['pack_sha256']
    xcon = root / f'intake/extraction-contracts/{project["contract"]["contract_id"]}.json'
    assert first.contract['contract_sha256'] == sha256(xcon.read_bytes()).hexdigest()
    assert all(path.read_bytes() == value for path, value in protected.items())
    assert any(level == 'BLOCKING' and 'PREPARED' in message
               for level, _, message in validate(root))


def test_capability_required_and_bound_to_unique_attempt(project):
    first, second = prepare(project), prepare(project)
    root = project['root']
    cap = authorize(root, first)
    for bad in [None, {'approved': True}, cap]:
        with pytest.raises(execution.AttemptError, match='capability'):
            execution.dispatch_attempt(root, second.attempt_id, bad)
    with pytest.raises(execution.AttemptError, match='binding'):
        execution.authorize_test_attempt(root, first.attempt_id,
                                         expected_contract_sha256='0' * 64)
    assert not checkpoint(root, first, 1).exists()


@pytest.mark.parametrize('field,value', [
    ('instruction_sha256', 'b' * 64), ('instruction_bytes', 1),
    ('options', {'scenario': 'timeout'}), ('retention', 'repository_snapshot'),
    ('adapter', {'id': 'untrusted', 'version': '1'}),
    ('adapter', {'id': 'deterministic-fake', 'version': '2'}), ('model', 'real-model'),
    ('execution_mode', 'remote'), ('allowed_destination', 'https://example.com'),
    ('limits', {'max_pack_bytes': 2097152, 'max_response_bytes': 100, 'timeout_seconds': 1}),
    ('scope', 'canonical_write'), ('project_id', 'other'),
    ('pack_sha256', 'c' * 64), ('contract_sha256', 'd' * 64),
])
def test_modified_security_input_even_rehashed_cannot_reuse_authorization(project, field, value):
    root = project['root']
    attempt = prepare(project)
    cap = authorize(root, attempt)
    def tamper(doc):
        doc['data']['contract'][field] = value
        doc['data']['contract_sha256'] = digest(doc['data']['contract'])
    rewrite(checkpoint(root, attempt), tamper)
    with pytest.raises(execution.AttemptError):
        execution.dispatch_attempt(root, attempt.attempt_id, cap)
    assert not checkpoint(root, attempt, 1).exists()


@pytest.mark.parametrize('kwargs', [
    {'adapter': 'untrusted'}, {'adapter_version': '2'}, {'model': 'real'},
    {'execution_mode': 'remote'}, {'allowed_destination': '../escape'},
    {'retention': 'repository_snapshot'}, {'options': {'approved': True}},
    {'limits': {'max_pack_bytes': 1, 'max_response_bytes': 1, 'timeout_seconds': 1}},
])
def test_unauthorized_parameters_rejected_before_publication(project, kwargs):
    with pytest.raises(execution.AttemptError):
        prepare(project, **kwargs)
    assert not list((project['root'] / 'intake/executions').glob('ATTEMPT-*'))


@pytest.mark.parametrize('field,value', [
    ('max_pack_bytes', 2097152.0),
    ('max_response_bytes', 65536.0),
    ('timeout_seconds', 30.0),
])
def test_integral_float_limit_rejected_without_durable_attempt(project, field, value):
    root = project['root']
    limits = {'max_pack_bytes': 2097152, 'max_response_bytes': 65536, 'timeout_seconds': 30}
    limits[field] = value
    directory = root / 'intake/executions'
    before = {path.relative_to(directory): path.read_bytes()
              for path in directory.rglob('*') if path.is_file()}
    assert not [issue for issue in validate(root) if issue[0] in {'ERROR', 'BLOCKING'}]
    with pytest.raises(execution.AttemptError):
        prepare(project, limits=limits)
    # Check all four obligations together so RED exposes the durable pollution.
    attempts = list(directory.glob('ATTEMPT-*'))
    checkpoints = list(directory.rglob('*.json'))
    errors = [issue for issue in validate(root)
              if issue[0] in {'ERROR', 'BLOCKING'} and issue[1].startswith('intake/executions')]
    assert (len(attempts), len(checkpoints), len(errors)) == (0, 0, 0)
    assert {path.relative_to(directory): path.read_bytes()
            for path in directory.rglob('*') if path.is_file()} == before


def test_first_dispatch_claim_independently_verified_duplicate_never_calls_transport(project, monkeypatch):
    root = project['root']
    attempt = prepare(project)
    cap = authorize(root, attempt)
    calls = []
    def transport(contract):
        independent = execution.inspect_attempt(root, attempt.attempt_id)
        assert independent.state == 'DELIVERY_UNKNOWN'
        calls.append(contract)
        return b'fake'
    monkeypatch.setattr(execution.execution_fake, 'dispatch', transport)
    result = execution.dispatch_attempt(root, attempt.attempt_id, cap)
    assert result['observation'] == 'fake_success'
    assert result['state'] == 'DELIVERY_UNKNOWN' and not result['semantic_evidence']
    marker = checkpoint(root, attempt, 1).read_bytes()
    with pytest.raises(execution.AttemptError, match='no redispatch'):
        execution.dispatch_attempt(root, attempt.attempt_id, cap)
    assert len(calls) == 1 and checkpoint(root, attempt, 1).read_bytes() == marker
    assert not execution.inspect_attempt(root, attempt.attempt_id).response_bytes_available


@pytest.mark.parametrize('scenario,expected', [
    ('success', 'fake_success'), ('after_intent', 'after_intent'), ('timeout', 'timeout'),
    ('unknown_delivery', 'unknown_delivery'), ('malformed_response', 'malformed_response'),
])
def test_fake_scenarios_are_delivery_unknown_not_extraction_success(project, scenario, expected):
    attempt = prepare(project, options={'scenario': scenario})
    result = execution.dispatch_attempt(project['root'], attempt.attempt_id,
                                        authorize(project['root'], attempt))
    assert result['observation'] == expected
    assert result['state'] == 'DELIVERY_UNKNOWN' and not result['semantic_evidence']


def test_fake_pre_dispatch_failure_and_response_interruption(project):
    root = project['root']
    before = prepare(project, options={'scenario': 'before_dispatch'})
    with pytest.raises(execution.AttemptError, match='before dispatch'):
        execution.dispatch_attempt(root, before.attempt_id, authorize(root, before))
    assert execution.inspect_attempt(root, before.attempt_id).state == 'PREPARED'
    after = prepare(project, options={'scenario': 'interrupted_after_response'})
    with pytest.raises(execution.AttemptError, match='after observation'):
        execution.dispatch_attempt(root, after.attempt_id, authorize(root, after))
    assert execution.inspect_attempt(root, after.attempt_id).state == 'DELIVERY_UNKNOWN'


CHILD = '''
import os, sys
from pathlib import Path
from project_system import execution_attempt as e
root, ident, contract_sha, mode, marker = sys.argv[1:]
cap = e.authorize_test_attempt(root, ident, expected_contract_sha256=contract_sha)
def transport(contract):
    Path(marker).write_bytes(b'called')
    return b'fake'
e.execution_fake.dispatch = transport
def boundary(name):
    if name == mode:
        os._exit(23)
e._boundary = boundary
if mode == 'concurrent':
    print('READY', flush=True)
    sys.stdin.readline()
try:
    e.dispatch_attempt(root, ident, cap)
    print('DISPATCHED', flush=True)
except e.AttemptError:
    print('REJECTED', flush=True)
'''


def child_args(root, attempt, mode, marker):
    return [sys.executable, '-c', CHILD, str(root), attempt.attempt_id,
            digest(attempt.contract), mode, str(marker)]


@pytest.mark.parametrize('boundary,state,called', [
    ('before_dispatch_intent', 'PREPARED', False),
    ('after_publication:02-boundary.json', 'DELIVERY_UNKNOWN', False),
    ('after_dispatch_intent', 'DELIVERY_UNKNOWN', False),
    ('after_response_observation', 'DELIVERY_UNKNOWN', True),
])
def test_real_process_crash_recovery_never_auto_dispatches(project, tmp_path, boundary, state, called):
    root = project['root']
    attempt = prepare(project)
    marker = tmp_path / 'transport-called'
    result = subprocess.run(child_args(root, attempt, boundary, marker),
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 23, (result.stdout, result.stderr)
    independent = execution.inspect_attempt(root, attempt.attempt_id)
    assert independent.state == state and marker.exists() == called
    if state == 'DELIVERY_UNKNOWN':
        with pytest.raises(execution.AttemptError, match='no redispatch'):
            execution.dispatch_attempt(root, attempt.attempt_id, authorize(root, independent))
    else:
        # No stale capability from child survives restart; explicit reauthorization.
        with pytest.raises(execution.AttemptError, match='capability'):
            execution.dispatch_attempt(root, attempt.attempt_id, None)


def test_real_concurrent_processes_only_first_claim_dispatches(project, tmp_path):
    root = project['root']
    attempt = prepare(project)
    markers = [tmp_path / f'called-{i}' for i in range(2)]
    children = [subprocess.Popen(child_args(root, attempt, 'concurrent', marker),
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True) for marker in markers]
    try:
        for child in children:
            assert child.stdout.readline().strip() == 'READY'
        for child in children:
            child.stdin.write('go\n')
            child.stdin.flush()
        outputs = [child.communicate(timeout=30) for child in children]
        assert all(child.returncode == 0 for child in children), outputs
        assert sorted(out.strip() for out, _ in outputs) == ['DISPATCHED', 'REJECTED']
        assert sum(marker.exists() for marker in markers) == 1
        assert execution.inspect_attempt(root, attempt.attempt_id).state == 'DELIVERY_UNKNOWN'
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=10)


def test_conflicting_concurrent_claim_preserved_without_dispatch(project, monkeypatch):
    root = project['root']
    attempt = prepare(project)
    cap = authorize(root, attempt)
    destination = checkpoint(root, attempt, 1)
    original_link = os.link
    def race(source, target):
        if Path(target) == destination:
            destination.write_bytes(b'unrelated writer')
        return original_link(source, target)
    monkeypatch.setattr(execution.os, 'link', race)
    monkeypatch.setattr(execution.execution_fake, 'dispatch', lambda _: pytest.fail('transport invoked'))
    with pytest.raises(execution.AttemptError, match='already exists'):
        execution.dispatch_attempt(root, attempt.attempt_id, cap)
    assert destination.read_bytes() == b'unrelated writer'
    with pytest.raises(execution.AttemptError):
        execution.inspect_attempt(root, attempt.attempt_id)
    assert not list((root / '.generated/execution-staging').iterdir())


def test_unsupported_link_no_fallback_no_dispatch_and_temp_cleanup(project, monkeypatch):
    root = project['root']
    attempt = prepare(project)
    cap = authorize(root, attempt)
    def unavailable(*args):
        raise NotImplementedError
    monkeypatch.setattr(execution.os, 'link', unavailable)
    monkeypatch.setattr(execution.os, 'replace', lambda *_: pytest.fail('unsafe fallback'))
    monkeypatch.setattr(execution.execution_fake, 'dispatch', lambda _: pytest.fail('transport'))
    with pytest.raises(execution.AttemptError, match='unsupported'):
        execution.dispatch_attempt(root, attempt.attempt_id, cap)
    assert not checkpoint(root, attempt, 1).exists()
    assert not list((root / '.generated/execution-staging').iterdir())


@pytest.mark.parametrize('mode', ['missing', 'corrupt', 'extra', 'wrong_link', 'wrong_project',
                                 'wrong_attempt', 'auth', 'duplicate_json', 'nonfinite'])
def test_independent_validator_rejects_corrupt_not_interrupted(project, mode):
    root = project['root']
    attempt = prepare(project)
    execution.dispatch_attempt(root, attempt.attempt_id, authorize(root, attempt))
    prepared, intent = checkpoint(root, attempt), checkpoint(root, attempt, 1)
    if mode == 'missing':
        prepared.unlink()
    elif mode == 'corrupt':
        intent.write_bytes(b'{}')
    elif mode == 'extra':
        (intent.parent / 'unexpected.json').write_bytes(b'{}')
    elif mode == 'duplicate_json':
        intent.write_bytes(b'{"state":1,"state":2}')
    elif mode == 'nonfinite':
        intent.write_bytes(b'{"state":NaN}')
    else:
        def tamper(doc):
            if mode == 'wrong_link':
                doc['previous_sha256'] = '0' * 64
            elif mode == 'wrong_project':
                doc['project_id'] = 'other'
            elif mode == 'wrong_attempt':
                doc['attempt_id'] = 'ATTEMPT-' + '0' * 32
            elif mode == 'auth':
                doc['data']['authorization']['contract_sha256'] = '0' * 64
        rewrite(intent, tamper)
    with pytest.raises(execution.AttemptError):
        execution.inspect_attempt(root, attempt.attempt_id)
    layer = execution.inspect_execution_layer(root, load_yaml(root / 'project.yaml'))
    assert layer.issues[0][0] == 'ERROR' and not layer.attempts


def test_terminal_append_only_policy_no_completion_and_cache_disposable(project):
    root = project['root']
    abandoned, unresolved = prepare(project), prepare(project)
    first_raw = checkpoint(root, abandoned).read_bytes()
    cap1, cap2 = authorize(root, abandoned), authorize(root, unresolved)
    with pytest.raises(execution.AttemptError):
        execution.close_test_attempt(root, abandoned.attempt_id, cap1, outcome='unresolved')
    closed = execution.close_test_attempt(root, abandoned.attempt_id, cap1, outcome='abandoned')
    assert closed.state == 'ABANDONED' and not closed.semantic_evidence
    with pytest.raises(execution.AttemptError):
        execution.dispatch_attempt(root, abandoned.attempt_id, cap1)
    execution.dispatch_attempt(root, unresolved.attempt_id, cap2)
    boundary_raw = checkpoint(root, unresolved, 1).read_bytes()
    closed = execution.close_test_attempt(root, unresolved.attempt_id, cap2, outcome='unresolved')
    assert closed.state == 'UNRESOLVED' and not closed.semantic_evidence
    assert checkpoint(root, abandoned).read_bytes() == first_raw
    assert checkpoint(root, unresolved, 1).read_bytes() == boundary_raw
    shutil.rmtree(root / '.generated')
    assert execution.inspect_attempt(root, unresolved.attempt_id).state == 'UNRESOLVED'
    assert not [issue for issue in validate(root) if issue[0] in {'ERROR', 'BLOCKING'}]
    assert sum('no semantic Evidence' in message for _, _, message in validate(root)) == 2


def test_legacy_optional_namespace_init_and_unknown_intake(tmp_path):
    root = init_project('Legacy', tmp_path / 'legacy')
    assert (root / 'intake/executions/.gitkeep').is_file()
    shutil.rmtree(root / 'intake/executions')
    assert not [issue for issue in validate(root) if issue[0] in {'ERROR', 'BLOCKING'}]
    (root / 'intake/unowned').mkdir()
    assert any(level == 'ERROR' and path == 'intake/unowned' for level, path, _ in validate(root))


def test_schema_packaged_and_valid():
    schema = json.loads((distribution_root() / 'schemas' / execution.SCHEMA).read_bytes())
    Draft202012Validator.check_schema(schema)
    assert schema['additionalProperties'] is False


def test_changed_pack_cache_prevents_claim_not_just_hash_trust(project):
    root = project['root']
    attempt = prepare(project)
    cap = authorize(root, attempt)
    cache = root / '.generated/source-extraction-packs' / project['pack']['pack_id'] / 'pack.json'
    cache.write_bytes(b'{}')
    # Durable recovery does not require caches; dispatch always requires exact bytes.
    assert execution.inspect_attempt(root, attempt.attempt_id).state == 'PREPARED'
    with pytest.raises(execution.AttemptError):
        execution.dispatch_attempt(root, attempt.attempt_id, cap)
    assert not checkpoint(root, attempt, 1).exists()


def test_changed_full_xcon_receipt_and_corrupt_checkpoint_hash_fail_closed(project):
    root = project['root']
    attempt = prepare(project)
    cap = authorize(root, attempt)
    xcon = root / f'intake/extraction-contracts/{project["contract"]["contract_id"]}.json'
    original = xcon.read_bytes()
    xcon.write_bytes(original + b'\n')
    with pytest.raises(execution.AttemptError, match='full commitment'):
        execution.inspect_attempt(root, attempt.attempt_id)
    xcon.write_bytes(original)
    execution.dispatch_attempt(root, attempt.attempt_id, cap)
    intent = checkpoint(root, attempt, 1)
    document = json.loads(intent.read_bytes())
    document['checkpoint_sha256'] = '0' * 64
    intent.write_bytes(canonical_bytes(document))
    with pytest.raises(execution.AttemptError, match='integrity'):
        execution.inspect_attempt(root, attempt.attempt_id)


@pytest.mark.parametrize('mode', ['missing_intent', 'double_terminal', 'wrong_terminal'])
def test_contradictory_terminal_checkpoints_fail_closed(project, mode):
    root = project['root']
    attempt = prepare(project)
    cap = authorize(root, attempt)
    execution.dispatch_attempt(root, attempt.attempt_id, cap)
    execution.close_test_attempt(root, attempt.attempt_id, cap, outcome='unresolved')
    if mode == 'missing_intent':
        checkpoint(root, attempt, 1).unlink()
    elif mode == 'double_terminal':
        def tamper(doc):
            doc['state'] = 'DISPOSITION'
            doc['data']['outcome'] = 'abandoned'
        rewrite(checkpoint(root, attempt, 1), tamper)
    else:
        rewrite(checkpoint(root, attempt, 2), lambda doc: doc['data'].update(outcome='abandoned'))
    with pytest.raises(execution.AttemptError):
        execution.inspect_attempt(root, attempt.attempt_id)


def test_concurrent_abandon_and_dispatch_share_one_no_clobber_slot(project, monkeypatch):
    root = project['root']
    attempt = prepare(project)
    cap = authorize(root, attempt)
    barrier, calls = Barrier(2), []
    def boundary(name):
        if name == 'before_publication:02-boundary.json':
            barrier.wait(timeout=10)
    monkeypatch.setattr(execution, '_boundary', boundary)
    monkeypatch.setattr(execution.execution_fake, 'dispatch', lambda _: calls.append(1) or b'fake')
    def action(dispatch):
        try:
            if dispatch:
                execution.dispatch_attempt(root, attempt.attempt_id, cap)
            else:
                execution.close_test_attempt(root, attempt.attempt_id, cap, outcome='abandoned')
            return 'created'
        except execution.AttemptError:
            return 'rejected'
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(action, value) for value in [True, False]]
        assert sorted(f.result(timeout=20) for f in futures) == ['created', 'rejected']
    state = execution.inspect_attempt(root, attempt.attempt_id).state
    assert len(calls) == (1 if state == 'DELIVERY_UNKNOWN' else 0)
    assert state in {'DELIVERY_UNKNOWN', 'ABANDONED'}


def test_checkpoint_directory_and_namespace_file_fail_closed(project):
    root = project['root']
    attempt = prepare(project)
    path = checkpoint(root, attempt, 1)
    path.mkdir()
    with pytest.raises(execution.AttemptError):
        execution.inspect_attempt(root, attempt.attempt_id)
    shutil.rmtree(root / 'intake/executions')
    (root / 'intake/executions').write_bytes(b'not a directory')
    assert execution.inspect_execution_layer(root, load_yaml(root / 'project.yaml')).issues[0][0] == 'ERROR'


@pytest.mark.parametrize('ident', ['../outside', 'ATTEMPT-' + 'a'*32 + '/x', None, []])
def test_path_traversal_and_wrong_types_fail_closed(project, ident):
    with pytest.raises(execution.AttemptError):
        execution.inspect_attempt(project['root'], ident)


def test_symlink_checkpoint_rejected(project, tmp_path):
    attempt = prepare(project)
    path = checkpoint(project['root'], attempt)
    outside = tmp_path / 'external-checkpoint'
    outside.write_bytes(path.read_bytes())
    path.unlink()
    try:
        path.symlink_to(outside)
    except OSError:
        pytest.skip('symlink creation unavailable for this Windows user')
    with pytest.raises(execution.AttemptError):
        execution.inspect_attempt(project['root'], attempt.attempt_id)
    assert outside.read_bytes()


def test_windows_reparse_checkpoint_rejected_without_transport(project, monkeypatch):
    attempt = prepare(project)
    root = project['root']
    cap = authorize(root, attempt)
    target = checkpoint(root, attempt)
    original_lstat = os.lstat
    def reparse(path, *args, **kwargs):
        info = original_lstat(path, *args, **kwargs)
        if Path(path) == target:
            values = {name: getattr(info, name) for name in dir(info) if name.startswith('st_')}
            values['st_file_attributes'] = getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)
            return SimpleNamespace(**values)
        return info
    monkeypatch.setattr(execution.os, 'lstat', reparse)
    monkeypatch.setattr(execution.execution_fake, 'dispatch', lambda _: pytest.fail('transport'))
    with pytest.raises(execution.AttemptError):
        execution.dispatch_attempt(root, attempt.attempt_id, cap)
