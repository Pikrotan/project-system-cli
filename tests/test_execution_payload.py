"""B2 sealed fake responses; no providers, canonical writes or production approval."""

from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from extraction_test_helpers import contracted_project
from project_system import execution_attempt as e
from project_system.extraction_pack import create_extraction_pack
from project_system.source_layer import canonical_bytes
from project_system.validation import validate


RESPONSE = b'{"fake_orchestration_only":true}'


@pytest.fixture
def project(tmp_path):
    result = contracted_project(tmp_path)
    result['pack'] = create_extraction_pack(result['root'], result['contract']['contract_id'])
    return result


def digest(doc):
    return sha256(canonical_bytes(doc)).hexdigest()


def prepare(project, retention='sealed_local', **kwargs):
    return e.prepare_attempt(project['root'], project['pack']['pack_id'], b'trusted fake instruction',
                             schema_version=3, retention=retention, **kwargs)


def cap(root, attempt):
    return e.authorize_test_attempt(root, attempt.attempt_id,
                                    expected_contract_sha256=digest(attempt.contract))


def outcome(root, attempt):
    return root / 'intake/executions' / attempt.attempt_id / '03-outcome.json'


def blob(root, attempt):
    return root / '.project-local/storage/executions' / attempt.attempt_id / 'response.bin'


def complete(project, retention='sealed_local'):
    attempt = prepare(project, retention)
    e.dispatch_attempt(project['root'], attempt.attempt_id, cap(project['root'], attempt))
    return attempt


def execution_issues(root):
    return [issue for issue in validate(root)
            if issue[1].startswith(('intake/executions', '.project-local/'))]


def git(root, *args):
    return subprocess.run(['git', *args], cwd=root, capture_output=True, text=True, check=True, timeout=30).stdout


def test_init_excludes_local_storage_from_git_and_ai_context(project):
    root = project['root']
    assert '/.project-local/' in (root / '.gitignore').read_text().splitlines()
    assert '.project-local/**' in (root / '.llmignore').read_text().splitlines()
    git(root, 'init', '-q')
    probe = root / '.project-local/storage/probe'
    probe.parent.mkdir(parents=True)
    probe.write_bytes(b'private')
    assert git(root, 'check-ignore', '--', '.project-local/storage/probe').strip() == '.project-local/storage/probe'


@pytest.mark.parametrize('version', [1, 2])
def test_v1_v2_remain_metadata_only_without_retroactive_sealing(project, version):
    root = project['root']
    attempt = e.prepare_attempt(root, project['pack']['pack_id'], b'legacy', schema_version=version)
    original = (outcome(root, attempt).parent / '01-prepared.json').read_bytes()
    report = e.dispatch_attempt(root, attempt.attempt_id, cap(root, attempt))
    assert report['state'] == ('DELIVERY_UNKNOWN' if version == 1 else 'RESPONSE_RECORDED')
    assert not blob(root, attempt).exists()
    assert not e.inspect_attempt(root, attempt.attempt_id).response_bytes_available
    assert (outcome(root, attempt).parent / '01-prepared.json').read_bytes() == original
    with pytest.raises(e.AttemptError):
        e.prepare_attempt(root, project['pack']['pack_id'], b'legacy', schema_version=version,
                          retention='sealed_local')


def test_v3_exact_sealed_bytes_history_and_current_availability(project):
    root = project['root']
    protected = {p: p.read_bytes() for prefix in ('knowledge', 'docs', '.github')
                 for p in (root / prefix).rglob('*') if p.is_file()}
    protected[root / 'project.yaml'] = (root / 'project.yaml').read_bytes()
    attempt = complete(project)
    assert blob(root, attempt).read_bytes() == RESPONSE
    result = e.inspect_attempt(root, attempt.attempt_id)
    assert result.state == 'RESPONSE_RECORDED' and result.payload_status == 'AVAILABLE'
    assert result.response_bytes_available and not result.semantic_evidence
    assert e.read_response(root, attempt.attempt_id) == RESPONSE
    doc = json.loads(outcome(root, attempt).read_bytes())
    assert doc['schema_version'] == 3 and doc['profile'] == 'project-system-execution-attempt-test-v3'
    record = doc['data']['xinv']
    assert record['schema_version'] == 2 and record['profile'] == 'project-system-execution-invocation-test-v2'
    assert record['retention'] == 'sealed_local'
    assert 'response_bytes_available' not in record
    assert record['sealed_payload'] == {'path': blob(root, attempt).relative_to(root).as_posix(),
                                        'sha256': sha256(RESPONSE).hexdigest(), 'bytes': len(RESPONSE)}
    assert record['response'] == {'sha256': sha256(RESPONSE).hexdigest(), 'bytes': len(RESPONSE)}
    assert all(RESPONSE not in p.read_bytes() for p in (root / 'intake').rglob('*') if p.is_file())
    assert not [issue for issue in execution_issues(root) if issue[0] in {'ERROR', 'BLOCKING'}]
    assert all(path.read_bytes() == value for path, value in protected.items())
    assert not list((root / '.project-local/execution-staging').iterdir())
    git(root, 'init', '-q')
    assert '.project-local' not in git(root, 'status', '--short', '--untracked-files=all')
    assert not git(root, 'ls-files', '--', '.project-local')


def test_v3_metadata_only_has_no_payload_and_cannot_read(project):
    root = project['root']
    attempt = complete(project, 'metadata_only')
    record = json.loads(outcome(root, attempt).read_bytes())['data']['xinv']
    assert record['retention'] == 'metadata_only' and record['sealed_payload'] is None
    assert not (root / '.project-local').exists()
    assert e.inspect_attempt(root, attempt.attempt_id).payload_status == 'NOT_RETAINED'
    with pytest.raises(e.AttemptError):
        e.read_response(root, attempt.attempt_id)


def test_missing_after_transfer_preserves_immutable_history_as_warning(project):
    root = project['root']
    attempt = complete(project)
    immutable = outcome(root, attempt).read_bytes()
    blob(root, attempt).unlink()
    # Simulates transferred canonical Git data with no machine-local tree.
    import shutil
    shutil.rmtree(root / '.project-local')
    result = e.inspect_attempt(root, attempt.attempt_id)
    assert result.state == 'RESPONSE_RECORDED' and result.terminal
    assert result.payload_status == 'MISSING' and not result.response_bytes_available
    issues = execution_issues(root)
    assert any(level == 'WARNING' and 'PAYLOAD_MISSING' in message for level, _, message in issues)
    assert not [issue for issue in issues if issue[0] in {'ERROR', 'BLOCKING'}]
    with pytest.raises(e.AttemptError):
        e.read_response(root, attempt.attempt_id)
    assert outcome(root, attempt).read_bytes() == immutable


@pytest.mark.parametrize('replacement', [b'corrupt', b'x' * len(RESPONSE), RESPONSE + b'!'])
def test_corrupt_payload_is_error_without_changing_terminal_history(project, replacement):
    root = project['root']
    attempt = complete(project)
    immutable = outcome(root, attempt).read_bytes()
    blob(root, attempt).write_bytes(replacement)
    result = e.inspect_attempt(root, attempt.attempt_id)
    assert result.state == 'RESPONSE_RECORDED' and result.payload_status == 'CORRUPT'
    assert not result.response_bytes_available
    assert any(level == 'ERROR' and 'PAYLOAD_CORRUPT' in message
               for level, _, message in execution_issues(root))
    with pytest.raises(e.AttemptError):
        e.read_response(root, attempt.attempt_id)
    assert outcome(root, attempt).read_bytes() == immutable


@pytest.mark.parametrize('file', ['.gitignore', '.llmignore'])
@pytest.mark.parametrize('mode', ['missing', 'no_rule', 'negation'])
def test_existing_project_protection_is_required_before_publication(project, file, mode):
    root = project['root']
    path = root / file
    if mode == 'missing':
        path.unlink()
    elif mode == 'no_rule':
        path.write_text('unrelated/**\n')
    else:
        path.write_text(path.read_text() + '!.project-local/**\n')
    before = path.read_bytes() if path.exists() else None
    with pytest.raises(e.AttemptError, match='protect'):
        prepare(project)
    assert not list((root / 'intake/executions').glob('ATTEMPT-*'))
    assert not (root / '.project-local').exists()
    assert (path.read_bytes() if path.exists() else None) == before


def test_already_tracked_local_storage_rejected(project):
    root = project['root']
    git(root, 'init', '-q')
    probe = root / '.project-local/storage/private.bin'
    probe.parent.mkdir(parents=True)
    probe.write_bytes(b'old unrelated bytes')
    git(root, 'add', '-f', '--', '.project-local/storage/private.bin')
    with pytest.raises(e.AttemptError, match='tracked'):
        prepare(project)
    assert probe.read_bytes() == b'old unrelated bytes'


def test_changed_protection_after_prepare_fails_before_intent_and_transport(project, monkeypatch):
    root = project['root']
    attempt = prepare(project)
    authorization = cap(root, attempt)
    (root / '.llmignore').write_text('unrelated/**\n')
    monkeypatch.setattr(e.execution_fake, 'dispatch', lambda _: pytest.fail('transport invoked'))
    with pytest.raises(e.AttemptError, match='protect'):
        e.dispatch_attempt(root, attempt.attempt_id, authorization)
    assert e.inspect_attempt(root, attempt.attempt_id).state == 'PREPARED'
    assert not blob(root, attempt).exists()


@pytest.mark.parametrize('boundary,has_blob,terminal', [
    ('before_blob_publication', False, False), ('after_blob_publication', True, False),
    ('before_publication:03-outcome.json', True, False),
    ('after_publication:03-outcome.json', True, True),
])
def test_real_crash_blob_and_terminal_boundaries(project, boundary, has_blob, terminal):
    root = project['root']
    attempt = prepare(project)
    script = '''
import os, sys
from project_system import execution_attempt as e
root, ident, point = sys.argv[1:]
attempt = e.inspect_attempt(root, ident)
cap = e.authorize_test_attempt(root, ident, expected_contract_sha256=e._digest(attempt.contract))
def crash(name):
    if name == point:
        os._exit(23)
e._boundary = crash
e.dispatch_attempt(root, ident, cap)
'''
    crashed = subprocess.run([sys.executable, '-c', script, str(root), attempt.attempt_id, boundary],
                             capture_output=True, text=True, timeout=30)
    assert crashed.returncode == 23, (crashed.stdout, crashed.stderr)
    assert blob(root, attempt).exists() == has_blob
    assert outcome(root, attempt).exists() == terminal
    result = e.inspect_attempt(root, attempt.attempt_id)
    assert result.state == ('RESPONSE_RECORDED' if terminal else 'DELIVERY_UNKNOWN')
    assert result.response_bytes_available == terminal
    if not terminal:
        assert result.payload_status == ('ORPHAN' if has_blob else 'UNRESOLVED')
        with pytest.raises(e.AttemptError, match='no redispatch'):
            e.dispatch_attempt(root, attempt.attempt_id, cap(root, attempt))
        closed = e.close_test_attempt(root, attempt.attempt_id, cap(root, attempt), outcome='unresolved')
        assert closed.state == 'UNRESOLVED' and not closed.response_bytes_available
        assert blob(root, attempt).exists() == has_blob
        with pytest.raises(e.AttemptError):
            e.read_response(root, attempt.attempt_id)


def test_real_response_vs_unresolved_terminal_race_keeps_orphan_non_authoritative(project):
    root = project['root']
    attempt = prepare(project)
    script = '''
import sys
from project_system import execution_attempt as e
root, ident, action = sys.argv[1:]
attempt = e.inspect_attempt(root, ident)
cap = e.authorize_test_attempt(root, ident, expected_contract_sha256=e._digest(attempt.contract))
def boundary(name):
    if name == 'before_publication:03-outcome.json':
        print('READY', flush=True)
        assert sys.stdin.readline().strip() == 'GO'
e._boundary = boundary
try:
    if action == 'response':
        e.dispatch_attempt(root, ident, cap)
    else:
        e.close_test_attempt(root, ident, cap, outcome='unresolved')
    print('WON', flush=True)
except e.AttemptError:
    print('REJECTED', flush=True)
'''
    children, pool = [], ThreadPoolExecutor(max_workers=2)
    try:
        for action in ('response', 'unresolved'):
            child = subprocess.Popen([sys.executable, '-c', script, str(root), attempt.attempt_id, action],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            children.append(child)
            assert pool.submit(child.stdout.readline).result(timeout=30).strip() == 'READY'
        assert blob(root, attempt).read_bytes() == RESPONSE
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
    result = e.inspect_attempt(root, attempt.attempt_id)
    assert result.state in {'RESPONSE_RECORDED', 'UNRESOLVED'}
    assert result.payload_status == ('AVAILABLE' if result.state == 'RESPONSE_RECORDED' else 'ORPHAN')


@pytest.mark.parametrize('existing', [RESPONSE, b'unrelated writer'])
def test_existing_or_concurrently_created_blob_is_never_overwritten_or_accepted(project, monkeypatch, existing):
    root = project['root']
    attempt = prepare(project)
    def boundary(name):
        if name == 'before_blob_publication':
            blob(root, attempt).write_bytes(existing)
    monkeypatch.setattr(e, '_boundary', boundary)
    with pytest.raises(e.AttemptError, match='already exists'):
        e.dispatch_attempt(root, attempt.attempt_id, cap(root, attempt))
    assert blob(root, attempt).read_bytes() == existing
    assert not outcome(root, attempt).exists()
    assert e.inspect_attempt(root, attempt.attempt_id).state == 'DELIVERY_UNKNOWN'
    assert not list((root / '.project-local/execution-staging').iterdir())


def test_unsupported_blob_link_is_fail_closed_and_cleans_only_own_temp(project, monkeypatch):
    root = project['root']
    attempt = prepare(project)
    original = os.link
    def unsupported(source, destination, *args, **kwargs):
        if Path(destination).name == 'response.bin':
            raise NotImplementedError
        return original(source, destination, *args, **kwargs)
    monkeypatch.setattr(os, 'link', unsupported)
    monkeypatch.setattr(os, 'replace', lambda *_: pytest.fail('unsafe fallback'))
    with pytest.raises(e.AttemptError, match='unsupported'):
        e.dispatch_attempt(root, attempt.attempt_id, cap(root, attempt))
    assert not blob(root, attempt).exists() and not outcome(root, attempt).exists()
    assert not list((root / '.project-local/execution-staging').iterdir())


@pytest.mark.parametrize('mode', ['directory', 'reparse', 'symlink'])
def test_payload_filesystem_safety(project, tmp_path, monkeypatch, mode):
    root = project['root']
    attempt = complete(project)
    target = blob(root, attempt)
    if mode == 'directory':
        target.unlink()
        target.mkdir()
    elif mode == 'symlink':
        outside = tmp_path / 'outside'
        outside.write_bytes(RESPONSE)
        target.unlink()
        try:
            target.symlink_to(outside)
        except OSError:
            pytest.skip('symlink creation unavailable for this Windows user')
    else:
        original = os.lstat
        def reparse(path, *args, **kwargs):
            info = original(path, *args, **kwargs)
            if Path(path) == target:
                fields = {key: getattr(info, key) for key in dir(info) if key.startswith('st_')}
                fields['st_file_attributes'] = 0x400
                return SimpleNamespace(**fields)
            return info
        monkeypatch.setattr(os, 'lstat', reparse)
    assert e.inspect_attempt(root, attempt.attempt_id).payload_status == 'UNSAFE'
    assert any(issue[0] == 'ERROR' and 'PAYLOAD_UNSAFE' in issue[2] for issue in execution_issues(root))
    with pytest.raises(e.AttemptError):
        e.read_response(root, attempt.attempt_id)


@pytest.mark.parametrize('field,value', [
    ('path', '../outside.bin'), ('path', 'D:/secret.bin'),
    ('path', '.project-local/storage/executions/ATTEMPT-' + '0' * 32 + '/response.bin'),
    ('sha256', '0' * 64), ('bytes', float(len(RESPONSE))),
])
def test_rehashed_sealed_payload_forgery_and_attempt_substitution_rejected(project, field, value):
    root = project['root']
    attempt = complete(project)
    target = outcome(root, attempt)
    doc = json.loads(target.read_bytes())
    record = doc['data']['xinv']
    record['sealed_payload'][field] = value
    record['invocation_id'] = 'XINV-' + digest({k: v for k, v in record.items() if k != 'invocation_id'})[:32]
    doc['checkpoint_sha256'] = digest({k: v for k, v in doc.items() if k != 'checkpoint_sha256'})
    target.write_bytes(canonical_bytes(doc))
    with pytest.raises(e.AttemptError):
        e.inspect_attempt(root, attempt.attempt_id)
    with pytest.raises(e.AttemptError):
        e.read_response(root, attempt.attempt_id)


@pytest.mark.parametrize('field', ['max_pack_bytes', 'max_response_bytes', 'timeout_seconds'])
def test_v3_strict_limit_types_before_publication(project, field):
    limits = {'max_pack_bytes': 2097152, 'max_response_bytes': 65536, 'timeout_seconds': 30}
    limits[field] = float(limits[field])
    with pytest.raises(e.AttemptError, match='integers'):
        prepare(project, limits=limits)
    assert not list((project['root'] / 'intake/executions').glob('ATTEMPT-*'))


def test_v3_explicit_abandonment_never_seals_payload(project):
    root = project['root']
    attempt = prepare(project)
    result = e.close_test_attempt(root, attempt.attempt_id, cap(root, attempt), outcome='abandoned')
    assert result.state == 'ABANDONED' and not result.response_bytes_available
    assert not blob(root, attempt).exists() and not outcome(root, attempt).exists()


def test_unexpected_metadata_only_blob_is_orphan_not_available(project):
    root = project['root']
    attempt = complete(project, 'metadata_only')
    target = blob(root, attempt)
    target.parent.mkdir(parents=True)
    target.write_bytes(RESPONSE)
    result = e.inspect_attempt(root, attempt.attempt_id)
    assert result.state == 'RESPONSE_RECORDED' and result.payload_status == 'ORPHAN'
    assert not result.response_bytes_available
    assert any('PAYLOAD_ORPHAN' in issue[2] for issue in execution_issues(root))
    with pytest.raises(e.AttemptError):
        e.read_response(root, attempt.attempt_id)


def test_unreferenced_local_blob_without_attempt_is_diagnosed_not_promoted(project):
    root = project['root']
    ident = 'ATTEMPT-' + '0' * 32
    target = root / '.project-local/storage/executions' / ident / 'response.bin'
    target.parent.mkdir(parents=True)
    target.write_bytes(RESPONSE)
    assert any('PAYLOAD_ORPHAN' in issue[2] for issue in execution_issues(root))
    with pytest.raises(e.AttemptError):
        e.read_response(root, ident)
    assert target.read_bytes() == RESPONSE
    assert not list((root / 'intake/executions').glob('ATTEMPT-*'))


def test_reader_revalidates_bytes_after_initial_provenance_inspection(project, monkeypatch):
    root = project['root']
    attempt = complete(project)
    original = e.inspect_attempt
    def changed(*args, **kwargs):
        result = original(*args, **kwargs)
        blob(root, attempt).write_bytes(b'changed after inspection')
        return result
    monkeypatch.setattr(e, 'inspect_attempt', changed)
    with pytest.raises(e.AttemptError):
        e.read_response(root, attempt.attempt_id)


def test_protective_gate_rechecked_after_transient_response_before_blob_write(project, monkeypatch):
    root = project['root']
    attempt = prepare(project)
    def boundary(name):
        if name == 'after_response_observation':
            (root / '.llmignore').write_text('unrelated/**\n')
    monkeypatch.setattr(e, '_boundary', boundary)
    with pytest.raises(e.AttemptError, match='protect'):
        e.dispatch_attempt(root, attempt.attempt_id, cap(root, attempt))
    assert e.inspect_attempt(root, attempt.attempt_id).state == 'DELIVERY_UNKNOWN'
    assert not blob(root, attempt).exists() and not outcome(root, attempt).exists()


def test_forged_fake_result_never_creates_blob_or_outcome(project, monkeypatch):
    root = project['root']
    attempt = prepare(project)
    monkeypatch.setattr(e.execution_fake, 'dispatch', lambda _: b'caller-supplied response')
    with pytest.raises(e.AttemptError, match='unrecognized fake response'):
        e.dispatch_attempt(root, attempt.attempt_id, cap(root, attempt))
    assert e.inspect_attempt(root, attempt.attempt_id).state == 'DELIVERY_UNKNOWN'
    assert not blob(root, attempt).exists() and not outcome(root, attempt).exists()


def test_blob_is_complete_and_fsynced_before_hard_link_and_outcome(project, monkeypatch):
    root = project['root']
    attempt = prepare(project)
    synced, events = set(), []
    original_sync, original_link = os.fsync, os.link
    def sync(descriptor):
        info = os.fstat(descriptor)
        synced.add((info.st_dev, info.st_ino))
        return original_sync(descriptor)
    def link(source, destination, *args, **kwargs):
        if Path(destination).name == 'response.bin':
            info = os.lstat(source)
            assert (info.st_dev, info.st_ino) in synced
            assert Path(source).read_bytes() == RESPONSE
            assert not outcome(root, attempt).exists()
            events.append('blob_link')
        return original_link(source, destination, *args, **kwargs)
    monkeypatch.setattr(os, 'fsync', sync)
    monkeypatch.setattr(os, 'link', link)
    monkeypatch.setattr(e, '_boundary', events.append)
    e.dispatch_attempt(root, attempt.attempt_id, cap(root, attempt))
    assert events.index('after_dispatch_intent') < events.index('before_blob_publication')
    assert events.index('before_blob_publication') < events.index('blob_link') < events.index('after_blob_publication')
    assert events.index('after_blob_publication') < events.index('before_publication:03-outcome.json')


def test_repeat_dispatch_does_not_overwrite_blob_or_terminal(project):
    root = project['root']
    attempt = complete(project)
    before = (blob(root, attempt).read_bytes(), outcome(root, attempt).read_bytes())
    with pytest.raises(e.AttemptError):
        e.dispatch_attempt(root, attempt.attempt_id, None)
    assert (blob(root, attempt).read_bytes(), outcome(root, attempt).read_bytes()) == before


def test_blob_changed_before_independent_reread_never_gets_success_outcome(project, monkeypatch):
    root = project['root']
    attempt = prepare(project)
    def changed(name):
        if name == 'after_blob_publication':
            blob(root, attempt).write_bytes(b'changed before verification')
    monkeypatch.setattr(e, '_boundary', changed)
    with pytest.raises(e.AttemptError):
        e.dispatch_attempt(root, attempt.attempt_id, cap(root, attempt))
    assert not outcome(root, attempt).exists()
    assert e.inspect_attempt(root, attempt.attempt_id).state == 'DELIVERY_UNKNOWN'
    assert blob(root, attempt).read_bytes() == b'changed before verification'


def test_incomplete_staging_is_not_response_evidence_and_is_not_collected(project):
    root = project['root']
    attempt = prepare(project, options={'scenario': 'interrupted_after_response'})
    with pytest.raises(e.AttemptError, match='after observation'):
        e.dispatch_attempt(root, attempt.attempt_id, cap(root, attempt))
    stage = root / '.project-local/execution-staging/old-incomplete.tmp'
    stage.parent.mkdir(parents=True)
    stage.write_bytes(RESPONSE[:5])
    assert e.inspect_attempt(root, attempt.attempt_id).state == 'DELIVERY_UNKNOWN'
    assert not e.inspect_attempt(root, attempt.attempt_id).response_bytes_available
    assert not blob(root, attempt).exists() and not outcome(root, attempt).exists()
    closed = e.close_test_attempt(root, attempt.attempt_id, cap(root, attempt), outcome='unresolved')
    assert closed.state == 'UNRESOLVED' and not closed.response_bytes_available
    assert stage.read_bytes() == RESPONSE[:5]


@pytest.mark.parametrize('scenario', ['after_intent', 'timeout', 'unknown_delivery', 'malformed_response'])
def test_v3_fixed_unresolved_scenarios_never_persist_payload(project, scenario):
    root = project['root']
    attempt = prepare(project, options={'scenario': scenario})
    assert e.dispatch_attempt(root, attempt.attempt_id, cap(root, attempt))['state'] == 'UNRESOLVED'
    assert not blob(root, attempt).exists()
    record = json.loads(outcome(root, attempt).read_bytes())['data']['xinv']
    assert record['response'] is None and record['sealed_payload'] is None


def test_sealed_retention_change_cannot_reuse_metadata_only_authorization(project):
    root = project['root']
    attempt = prepare(project, 'metadata_only')
    authorization = cap(root, attempt)
    path = outcome(root, attempt).parent / '01-prepared.json'
    doc = json.loads(path.read_bytes())
    doc['data']['contract']['retention'] = 'sealed_local'
    doc['data']['contract_sha256'] = digest(doc['data']['contract'])
    doc['checkpoint_sha256'] = digest({k: v for k, v in doc.items() if k != 'checkpoint_sha256'})
    path.write_bytes(canonical_bytes(doc))
    with pytest.raises(e.AttemptError, match='capability'):
        e.dispatch_attempt(root, attempt.attempt_id, authorization)
    assert not blob(root, attempt).exists()
    assert not (path.parent / '02-boundary.json').exists()


def test_stable_file_read_detects_replacement_while_opening(project, monkeypatch):
    root = project['root']
    attempt = complete(project)
    target = blob(root, attempt)
    original = Path.open
    replaced = []
    def swap(path, *args, **kwargs):
        if path == target and not replaced and args and args[0] == 'rb':
            replaced.append(True)
            target.unlink()
            with original(target, 'wb') as stream:
                stream.write(b'replaced inode')
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', swap)
    result = e.inspect_attempt(root, attempt.attempt_id)
    assert replaced and result.payload_status == 'CORRUPT'
    assert result.state == 'RESPONSE_RECORDED' and not result.response_bytes_available
    with pytest.raises(e.AttemptError):
        e.read_response(root, attempt.attempt_id)
