from copy import deepcopy
from datetime import datetime
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys

import pytest
import yaml

from project_system.cli import main
from project_system.init_project import init_project
from project_system.objects import create_object
from project_system.sync_intake import SyncIntakeError, intake_sync
from project_system.sync_planning import SyncPlanError, plan_sync
from project_system.sync_verification import verify_sync
from project_system.sync_finalization import finalize_sync


def git(root, *args):
    return subprocess.run(['git', *args], cwd=root, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def project(tmp_path):
    root = init_project('Bridge Demo', tmp_path / 'project')
    obj, oid = create_object(root, 'feature', 'Existing feature', 'general', 'owner')
    obj = obj.rename(obj.with_name(f'{oid}-existing-feature.md'))
    git(root, 'init', '-q')
    git(root, 'config', 'user.email', 'tests@example.invalid')
    git(root, 'config', 'user.name', 'Bridge Tests')
    git(root, 'add', '.')
    git(root, 'commit', '-qm', 'test baseline')
    request = {
        'schema_version': 1,
        'request_id': 'REQUEST-20260906-aabbccdd',
        'source': {'type': 'external_discussion', 'ref': 'approved-thread', 'note': 'Исходный контекст'},
        'approval': {'approved_by': 'project-owner', 'approved_at': '2026-09-06T10:00:00+03:00'},
        'change_class': 'C',
        'changes': [{
            'change_id': 'approved-update', 'kind': 'update_object',
            'summary': 'Test-only approved clarification.', 'target_id': oid,
            'patch': {'body': 'Approved text from the external executor.'},
        }],
        'expected_targets': [oid], 'notes': 'Transport, not canonical truth.',
    }
    return root, obj, request


def encode(request):
    return yaml.safe_dump(request, sort_keys=False, allow_unicode=True).encode('utf-8')


def accept(project, request=None, **kwargs):
    root, _, original = project
    return intake_sync(root, '-', stdin=BytesIO(encode(request or original)), **kwargs)


def canonical(root):
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob('*') if path.is_file()
        and path.relative_to(root).parts[0] not in {'.git', '.generated', 'inbox'}
    }


def no_packs(root):
    assert list((root / 'inbox/sync').glob('*.yaml')) == []


def test_file_binding_provenance_and_no_canonical_or_git_writes(project, tmp_path, monkeypatch):
    root, _, request = project
    raw = encode(request)
    transport = tmp_path / 'request.yaml'
    transport.write_bytes(raw)
    before = canonical(root)
    head = git(root, 'rev-parse', 'HEAD')
    index = (root / '.git/index').read_bytes()
    calls = []
    original_run = subprocess.run

    def record(args, **kwargs):
        calls.append(args)
        return original_run(args, **kwargs)

    monkeypatch.setattr(subprocess, 'run', record)
    path, report = intake_sync(root, transport)
    pack = yaml.safe_load(path.read_text(encoding='utf-8'))
    assert pack['project_id'] == 'bridge-demo'
    assert pack['base_commit'] == head
    assert re.fullmatch(r'SYNC-\d{8}-[0-9a-f]{8}', pack['pack_id'])
    assert path == root / 'inbox/sync' / f'{pack["pack_id"]}.yaml'
    assert datetime.fromisoformat(pack['created_at']).tzinfo is not None
    assert pack['approval'] == request['approval']
    assert pack['changes'] == request['changes']
    assert pack['source'] == request['source']
    assert pack['provenance'] == {
        'request_id': request['request_id'], 'request_sha256': sha256(raw).hexdigest(),
        'source': request['source'], 'intake_at': pack['created_at'],
    }
    output = root / '.generated/sync' / pack['pack_id']
    assert (output / 'intake.md').is_file()
    assert json.loads((output / 'intake.json').read_text(encoding='utf-8')) == report
    assert not (output / 'plan.json').exists()
    assert report['change_kinds'] == {'update_object': 1}
    assert report['expected_targets'] == request['expected_targets']
    assert report['intake_result'] == 'created'
    assert canonical(root) == before
    assert git(root, 'rev-parse', 'HEAD') == head
    assert (root / '.git/index').read_bytes() == index
    assert not any(args[0] == 'git' and args[1] in {'add', 'commit', 'push', 'reset'} for args in calls)


def test_stdin_cli_and_plan_then_phase2_phase3(project, monkeypatch, capsys):
    root, obj, request = project
    monkeypatch.chdir(root)
    monkeypatch.setattr(sys, 'stdin', BytesIO(encode(request)))
    main(['sync', 'intake', '-', '--plan'])
    text = capsys.readouterr().out
    assert 'Status: created' in text and 'Plan: ready' in text and 'Allowed writes: 1' in text
    path = root / next(line[6:] for line in text.splitlines() if line.startswith('Path: '))
    pack = yaml.safe_load(path.read_text(encoding='utf-8'))
    obj.write_text(obj.read_text(encoding='utf-8') + '\nApproved edit.\n', encoding='utf-8')
    _, verification = verify_sync(root, path)
    _, finalization = finalize_sync(root, pack['pack_id'])
    assert verification['verification_result'] == 'passed'
    assert finalization['state'] == 'prepared'


def test_all_change_kinds_use_shared_resolution(project):
    root, obj, request = project
    oid = request['expected_targets'][0]
    new_id = 'FEAT-20260906-deadbeef'
    request['changes'] += [
        {'change_id': 'create', 'kind': 'create_object', 'summary': 'Create fixture.',
         'object': {'id': new_id, 'type': 'feature', 'title': 'Fixture', 'domain': 'general',
                    'slug': 'fixture', 'status': 'planned', 'body': 'Approved content.'}},
        {'change_id': 'retire', 'kind': 'retire_object', 'summary': 'Retire fixture.',
         'target_id': oid, 'new_status': 'deprecated', 'replacement_id': new_id},
        {'change_id': 'narrative', 'kind': 'narrative_impact', 'summary': 'Affected doc.',
         'narrative_paths': ['docs/03_PRODUCT.md']},
        {'change_id': 'proposal', 'kind': 'proposal', 'summary': 'Proposal only.',
         'proposal': 'Not an active decision.', 'related_ids': [oid]},
        {'change_id': 'unresolved', 'kind': 'unresolved', 'summary': 'Still open.',
         'proposal': 'Leave undecided.', 'related_ids': [oid]},
    ]
    request['expected_targets'] += [new_id, 'docs/03_PRODUCT.md']
    path, report = accept(project, plan=True)
    output = root / '.generated/sync' / path.stem
    manifest = json.loads((output / 'manifest.json').read_text(encoding='utf-8'))
    assert report['change_count'] == 6
    assert set(report['change_kinds']) == {'update_object', 'create_object', 'retire_object', 'narrative_impact', 'proposal', 'unresolved'}
    assert set(manifest['allowed_write_set']) == {
        obj.relative_to(root).as_posix(), 'docs/03_PRODUCT.md', f'knowledge/features/{new_id}-fixture.md',
    }
    assert len(manifest['unresolved_proposal_items']) == 2


@pytest.mark.parametrize('field,value', [
    ('schema_version', 2), ('request_id', '../escape'), ('request_id', '/absolute'),
    ('request_id', 'D:\\escape'), ('request_id', ''), ('request_id', 'bad\n'), ('change_class', 'Z'),
    ('approval', {}), ('approval', {'approved_by': 'owner', 'approved_at': 'not-a-date'}),
    ('approval', {'approved_by': '   ', 'approved_at': '2026-09-06T10:00:00Z'}),
    ('project_id', 'attacker'), ('base_commit', '0' * 40), ('pack_id', 'SYNC-owned'),
    ('output_path', 'D:/escape.yaml'), ('created_at', '2026-09-06T10:00:00Z'),
    ('provenance', {'request_sha256': '0' * 64}), ('changes', []),
])
def test_invalid_contract_never_writes_pack(project, field, value):
    root, _, request = project
    request[field] = value
    with pytest.raises(SyncIntakeError):
        accept(project)
    no_packs(root)


@pytest.mark.parametrize('raw', [
    b'[]', b'changes: [', b'schema_version: 1\nschema_version: 1\n',
    b'value: &x []\nother: *x', b'!!python/object/apply:os.system ["echo unsafe"]',
    b'{"request_id":"a", "request_id":"b"}', b'\xff\xfe', b'x' * (2 * 1024 * 1024 + 1),
], ids=['list', 'syntax', 'duplicate-yaml', 'alias', 'unsafe-yaml', 'duplicate-json', 'encoding', 'oversized'])
def test_strict_transport_parser(project, raw):
    root, _, _ = project
    with pytest.raises(SyncIntakeError):
        intake_sync(root, '-', stdin=BytesIO(raw))
    no_packs(root)


@pytest.mark.parametrize('timestamp', ['2026-09-06', '2026-09-06T10:00:00',
                                     '2026-02-30T10:00:00Z', '2026-09-06T10:00:00+01:99'])
def test_timestamp_validation_without_optional_format_dependencies(project, timestamp):
    from project_system.sync_planning import _validate_pack_schema
    root, _, request = project
    request['approval']['approved_at'] = timestamp
    with pytest.raises(SyncIntakeError, match='date-time'):
        accept(project)
    no_packs(root)
    request['approval']['approved_at'] = '2026-09-06T10:00:00Z'
    path, _ = accept(project)
    pack = yaml.safe_load(path.read_text(encoding='utf-8'))
    pack['approval']['approved_at'] = timestamp
    with pytest.raises(SyncPlanError, match='date-time'):
        _validate_pack_schema(pack)


def test_utf8_bom_crlf_hashes_original_transport_bytes(project):
    root, _, request = project
    raw = b'\xef\xbb\xbf' + encode(request).replace(b'\n', b'\r\n')
    path, report = intake_sync(root, '-', stdin=BytesIO(raw), plan=True)
    assert report['request_hash'] == sha256(raw).hexdigest()
    pack = yaml.safe_load(path.read_text(encoding='utf-8'))
    assert pack['source'] == request['source']
    assert pack['changes'] == request['changes']


def test_duplicate_pack_identity_rejected(project):
    path, _ = accept(project)
    path.with_name('duplicate.yaml').write_bytes(path.read_bytes())
    with pytest.raises(SyncIntakeError, match='duplicate pack_id'):
        accept(project)


@pytest.mark.parametrize('case', ['duplicate_change', 'missing_id', 'missing_ref', 'wrong_expected',
                                 'traversal', 'absolute', 'missing_narrative', 'create_collision', 'bad_create_id'])
def test_target_validation_before_write(project, case):
    root, _, request = project
    oid = request['expected_targets'][0]
    if case == 'duplicate_change':
        request['changes'].append(deepcopy(request['changes'][0]))
    elif case == 'missing_id':
        request['changes'][0]['target_id'] = 'FEAT-20260906-00000000'
    elif case == 'missing_ref':
        request['changes'][0]['patch'] = {'frontmatter': {'depends_on': ['REQ-20260906-00000000']}}
    elif case == 'wrong_expected':
        request['expected_targets'] = []
    elif case in {'traversal', 'absolute', 'missing_narrative'}:
        path = {'traversal': 'docs/../README.md', 'absolute': 'D:/owned.md',
                'missing_narrative': 'docs/missing.md'}[case]
        request['changes'] = [{'change_id': 'doc', 'kind': 'narrative_impact',
                               'summary': 'Bad target.', 'narrative_paths': [path]}]
        request['expected_targets'] = [path]
    else:
        new_id = oid if case == 'create_collision' else 'FEAT-20260906-UPPERBAD'
        request['changes'] = [{'change_id': 'new', 'kind': 'create_object', 'summary': 'Bad create.',
                              'object': {'id': new_id, 'type': 'feature', 'title': 'Test', 'body': 'Text'}}]
        request['expected_targets'] = [new_id]
    with pytest.raises(SyncIntakeError):
        accept(project)
    no_packs(root)


def test_reuse_same_bytes_and_head_preserves_pack_bytes_and_timestamp(project):
    path, first = accept(project)
    content, mtime = path.read_bytes(), path.stat().st_mtime_ns
    same_path, second = accept(project, plan=True)
    assert same_path == path and second['intake_result'] == 'reused' and second['reused']
    assert second['intake_at'] == first['intake_at']
    assert path.read_bytes() == content and path.stat().st_mtime_ns == mtime
    assert len(list(path.parent.glob('*.yaml'))) == 1


def test_same_request_new_head_creates_new_binding(project):
    root, _, _ = project
    first_path, first = accept(project)
    git(root, 'commit', '--allow-empty', '-qm', 'new test head')
    second_path, second = accept(project)
    assert first_path != second_path
    assert first['base_commit'] != second['base_commit'] == git(root, 'rev-parse', 'HEAD')
    assert first['request_hash'] == second['request_hash']


def test_changed_bytes_reusing_request_id_rejected(project):
    root, _, request = project
    path, _ = accept(project)
    before = path.read_bytes()
    request['notes'] += 'Changed.'
    with pytest.raises(SyncIntakeError, match='request_id already used'):
        accept(project)
    assert path.read_bytes() == before
    assert len(list(path.parent.glob('*.yaml'))) == 1


def test_duplicate_request_binding_rejected(project):
    root, _, _ = project
    path, _ = accept(project)
    pack = yaml.safe_load(path.read_text(encoding='utf-8'))
    pack['pack_id'] = 'SYNC-20260906-00000000'
    (path.parent / (pack['pack_id'] + '.yaml')).write_bytes(encode(pack))
    with pytest.raises(SyncIntakeError, match='duplicate request binding'):
        accept(project)


def test_pack_modification_is_not_silently_reused(project):
    path, _ = accept(project)
    pack = yaml.safe_load(path.read_text(encoding='utf-8'))
    pack['changes'][0]['summary'] = 'tampered'
    path.write_bytes(encode(pack))
    with pytest.raises(SyncIntakeError, match='modified'):
        accept(project)


def test_collision_retry_and_overwrite_protection(project, monkeypatch):
    from project_system import sync_intake
    root, _, request = project
    path, _ = accept(project)
    content = path.read_bytes()
    request['request_id'] = 'next-request'
    monkeypatch.setattr(sync_intake, '_new_pack_id', lambda now: path.stem)
    with pytest.raises(SyncIntakeError, match='collision-free'):
        accept(project)
    assert path.read_bytes() == content
    ids = iter([path.stem, 'SYNC-20260906-11223344'])
    monkeypatch.setattr(sync_intake, '_new_pack_id', lambda now: next(ids))
    other_path, _ = accept(project)
    assert other_path.stem == 'SYNC-20260906-11223344'
    assert path.read_bytes() == content


def test_exclusive_creation_race_never_overwrites(project, monkeypatch):
    from project_system import sync_intake
    root, _, _ = project
    race_path = root / 'inbox/sync/SYNC-20260906-11223344.yaml'
    ids = iter(['SYNC-20260906-11223344', 'SYNC-20260906-11223345'])
    monkeypatch.setattr(sync_intake, '_new_pack_id', lambda now: next(ids))
    original_open = Path.open

    def racing_open(path, *args, **kwargs):
        if path == race_path and args and args[0] == 'xb':
            with original_open(path, 'wb') as stream:
                stream.write(b'other process won')
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'open', racing_open)
    # Malformed file created by another process makes the next validation fail closed.
    with pytest.raises(SyncIntakeError):
        accept(project)
    assert race_path.read_bytes() == b'other process won'


def test_intake_lock_rejects_concurrent_writer(project):
    root, _, _ = project
    lock = root / '.generated/sync/.intake.lock'
    lock.parent.mkdir(parents=True)
    lock.write_bytes(b'other process')
    with pytest.raises(SyncIntakeError, match='already running'):
        accept(project)
    assert lock.read_bytes() == b'other process'


@pytest.mark.parametrize('which', ['inbox', 'inbox/sync', '.generated', '.generated/sync'])
def test_output_symlink_escape(project, tmp_path, which):
    root, _, _ = project
    target = root / which
    outside = tmp_path / 'outside'
    outside.mkdir()
    if target.exists():
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        target.symlink_to(outside, target_is_directory=True)
    except OSError:
        # Same path checks cover Windows junctions without symlink privileges.
        result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(target), str(outside)], capture_output=True)
        if result.returncode:
            pytest.skip('cannot create a symlink or junction')
    with pytest.raises(SyncIntakeError, match='symlink/junction'):
        accept(project)
    assert list(outside.iterdir()) == []


def test_inbox_selected_pack_plans_but_unrelated_inbox_remains_dirty(project):
    root, _, _ = project
    path, report = accept(project)
    plan_sync(root, path)
    (root / 'inbox/general/unrelated.txt').write_text('do not ignore', encoding='utf-8')
    with pytest.raises(SyncPlanError, match='clean working tree'):
        plan_sync(root, path)
    with pytest.raises(SyncIntakeError, match='clean working tree'):
        accept(project, plan=True)
    assert path.exists()


def test_intake_without_plan_allows_dirty_valid_canonical_state(project):
    root, obj, _ = project
    obj.write_text(obj.read_text(encoding='utf-8') + '\nExisting edit.\n', encoding='utf-8')
    before = canonical(root)
    accept(project)
    assert canonical(root) == before


def test_intake_with_plan_requires_clean_baseline_before_pack_write(project):
    root, obj, _ = project
    obj.write_text(obj.read_text(encoding='utf-8') + '\nExisting edit.\n', encoding='utf-8')
    with pytest.raises(SyncIntakeError, match='clean working tree'):
        accept(project, plan=True)
    no_packs(root)


def test_plan_failure_after_intake_keeps_reusable_pack_and_report(project, monkeypatch):
    from project_system import sync_intake
    root, _, _ = project
    original_plan = sync_intake.plan_sync

    def fail(*args):
        raise SyncPlanError('simulated subsequent plan failure')

    monkeypatch.setattr(sync_intake, 'plan_sync', fail)
    with pytest.raises(SyncIntakeError, match='pack retained'):
        accept(project, plan=True)
    path = next((root / 'inbox/sync').glob('*.yaml'))
    report = json.loads((root / '.generated/sync' / path.stem / 'intake.json').read_text(encoding='utf-8'))
    assert report['intake_result'] == 'created' and report['plan_result'] == 'failed'
    monkeypatch.setattr(sync_intake, 'plan_sync', original_plan)
    _, retried = accept(project, plan=True)
    assert retried['reused'] and retried['plan_result'] == 'ready'


def test_bridge_options_do_not_change_legacy_cli(project, monkeypatch):
    root, _, _ = project
    monkeypatch.chdir(root)
    with pytest.raises(SystemExit) as exc:
        main(['sync', 'plan', 'test.yaml', '--plan'])
    assert exc.value.code == 2
    with pytest.raises(SystemExit) as exc:
        main(['sync', 'intake', '-', '--commit'])
    assert exc.value.code == 2
