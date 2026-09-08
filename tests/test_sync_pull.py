from copy import deepcopy
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
import shutil
import subprocess

import pytest
import yaml

from project_system.cli import main
from project_system.init_project import init_project
from project_system.objects import create_object
from project_system import sync_pull
from project_system.sync_pull import SyncPullError, pull_sync, inspect_pull
from project_system.sync_intake import intake_sync
from project_system.sync_verification import verify_sync
from project_system.sync_finalization import finalize_sync

REPO = 'example-owner/example-project'


def git(root, *args):
    return subprocess.run(['git', *args], cwd=root, capture_output=True, text=True, check=True).stdout.strip()


def canonical(root):
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob('*') if p.is_file()
            and p.relative_to(root).parts[0] not in {'.git', '.generated', 'inbox'}}


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    root = init_project('Pull fixture', tmp_path / 'project')
    obj, oid = create_object(root, 'feature', 'Fixture', 'general', 'owner')
    obj = obj.rename(obj.with_name(f'{oid}-real-slug.md'))
    config_path = root / 'project.yaml'
    config = yaml.safe_load(config_path.read_text(encoding='utf-8'))
    config['external_systems']['github'].update(enabled=True, sync_pull={
        'allowed_authors': ['owner'], 'expected_repository': REPO,
    })
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding='utf-8')
    git(root, 'init', '-q')
    git(root, 'config', 'user.name', 'Fixture')
    git(root, 'config', 'user.email', 'fixture@example.invalid')
    git(root, 'remote', 'add', 'origin', f'git@github.com:{REPO}.git')
    git(root, 'add', '.')
    git(root, 'commit', '-qm', 'fixture baseline')
    request = {'schema_version': 1, 'request_id': 'REQUEST-test-pull',
               'source': {'type': 'external_discussion', 'ref': 'test', 'note': 'UTF-8 проверка'},
               'approval': {'approved_by': 'owner', 'approved_at': '2026-09-07T10:00:00Z'},
               'change_class': 'C',
               'changes': [{'change_id': 'update', 'kind': 'update_object', 'target_id': oid,
                            'summary': 'Fixture update.', 'patch': {'body': 'Fixture text.'}}],
               'expected_targets': [oid], 'notes': 'Not product truth.'}
    issue = {'number': 1, 'html_url': f'https://github.com/{REPO}/issues/1',
             'state': 'open', 'title': '[SYNC REQUEST][TEST] fixture', 'user': {'login': 'owner'},
             'updated_at': '2026-09-07T10:00:00Z',
             'body': yaml.safe_dump(request, sort_keys=False, allow_unicode=True)}
    state = {'issues': {1: issue}, 'calls': []}

    def gh_get(root, endpoint, fields=()):
        state['calls'].append((endpoint, fields))
        if endpoint == 'search/issues':
            issues = [i for i in state['issues'].values() if i['state'] == 'open']
            return {'total_count': len(issues), 'incomplete_results': False, 'items': deepcopy(issues)}
        assert endpoint.startswith(f'repos/{REPO}/issues/')
        return deepcopy(state['issues'][int(endpoint.rsplit('/', 1)[1])])

    monkeypatch.setattr(sync_pull, 'gh_get', gh_get)
    return root, obj, request, issue, state


def no_pack(root):
    assert list((root / 'inbox/sync').glob('*.yaml')) == []
    assert not list((root / '.generated').rglob('pull.json'))


def test_valid_pull_provenance_no_canonical_or_git_writes(fixture, monkeypatch):
    root, obj, request, issue, state = fixture
    before, head, index = canonical(root), git(root, 'rev-parse', 'HEAD'), (root / '.git/index').read_bytes()
    calls, original_run = [], subprocess.run

    def record(args, **kwargs):
        calls.append(args)
        return original_run(args, **kwargs)

    monkeypatch.setattr(subprocess, 'run', record)
    report = pull_sync(root)
    path = root / report['pack_path']
    pack = yaml.safe_load(path.read_text(encoding='utf-8'))
    transport = pack['provenance']['transport']
    assert pack['project_id'] == 'pull-fixture' and pack['base_commit'] == head
    assert pack['changes'] == request['changes'] and pack['source'] == request['source']
    assert transport == {'kind': 'github_issue', 'repository': REPO, 'issue_number': 1,
                         'issue_url': issue['html_url'], 'issue_author': 'owner',
                         'issue_updated_at': issue['updated_at'],
                         'body_sha256': sha256(issue['body'].encode()).hexdigest(),
                         'request_sha256': pack['provenance']['request_sha256'],
                         'title_sha256': sha256(issue['title'].encode()).hexdigest()}
    output = root / '.generated/sync' / pack['pack_id']
    assert all((output / name).exists() for name in ['intake.json', 'intake.md', 'pull.json', 'pull.md'])
    assert json.loads((output / 'intake.json').read_text())['transport'] == transport
    assert not (output / 'plan.json').exists()
    assert report['remote_action'] == 'none' and report['acknowledgement'] == 'local_only'
    assert canonical(root) == before and git(root, 'rev-parse', 'HEAD') == head
    assert (root / '.git/index').read_bytes() == index
    assert all(args[1] not in {'add', 'commit', 'push', 'reset'} for args in calls if args[0] == 'git')
    assert all(endpoint == 'search/issues' or endpoint == f'repos/{REPO}/issues/1' for endpoint, _ in state['calls'])


def test_pull_plan_cli_and_phase_2_3(fixture, monkeypatch, capsys):
    root, obj, _, _, _ = fixture
    monkeypatch.chdir(root)
    main(['sync', 'pull', '--plan'])
    assert 'Plan: ready' in capsys.readouterr().out
    path = next((root / 'inbox/sync').glob('*.yaml'))
    manifest = json.loads((root / '.generated/sync' / path.stem / 'manifest.json').read_text())
    assert manifest['allowed_write_set'] == [obj.relative_to(root).as_posix()]
    obj.write_text(obj.read_text(encoding='utf-8') + '\nFixture edit.\n', encoding='utf-8')
    assert verify_sync(root, path)[1]['verification_result'] == 'passed'
    assert finalize_sync(root, path)[1]['state'] == 'prepared'


def test_read_only_inspection_does_not_write(fixture):
    root, _, _, _, _ = fixture
    before = {p.relative_to(root): p.read_bytes() for p in root.rglob('*') if p.is_file()}
    assert inspect_pull(root)['selected']['request']['request_id'] == 'REQUEST-test-pull'
    after = {p.relative_to(root): p.read_bytes() for p in root.rglob('*') if p.is_file()}
    assert after == before


def test_same_issue_reused_across_heads_and_missing_derived_receipt(fixture):
    root, _, _, _, _ = fixture
    first = pull_sync(root, plan=True)
    path = root / first['pack_path']
    before, mtime = path.read_bytes(), path.stat().st_mtime_ns
    receipt = root / '.generated/sync' / first['pack_id'] / 'pull.json'
    original_receipt = receipt.read_bytes()
    assert pull_sync(root, plan=True)['status'] == 'already_processed'
    assert receipt.read_bytes() == original_receipt
    receipt.unlink()  # Generated receipt is not the idempotency source of truth.
    git(root, 'commit', '--allow-empty', '-qm', 'fixture next head')
    repeated = pull_sync(root)
    assert repeated['pack_id'] == first['pack_id']
    assert path.read_bytes() == before and path.stat().st_mtime_ns == mtime
    assert len(list(path.parent.glob('*.yaml'))) == 1
    assert receipt.read_bytes() == original_receipt
    with pytest.raises(SyncPullError, match='stale base_commit'):
        pull_sync(root, plan=True)


@pytest.mark.parametrize('change', ['body', 'title', 'updated_at', 'author', 'marker', 'url', 'closed'])
def test_changed_processed_issue_is_drift(fixture, change):
    root, _, _, issue, _ = fixture
    report = pull_sync(root)
    before = (root / report['pack_path']).read_bytes()
    if change == 'body': issue['body'] += '\n# edited'
    elif change == 'title': issue['title'] += ' edited'
    elif change == 'updated_at': issue['updated_at'] = '2026-09-07T11:00:00Z'
    elif change == 'author': issue['user']['login'] = 'intruder'
    elif change == 'marker': issue['title'] = 'ordinary issue now'
    elif change == 'url': issue['html_url'] = 'https://github.com/other/repo/issues/1'
    else:
        issue['state'] = 'closed'
        issue['updated_at'] = '2026-09-07T11:00:00Z'
    with pytest.raises(SyncPullError, match='drift/conflict'):
        pull_sync(root, issue_number=1)
    assert (root / report['pack_path']).read_bytes() == before


@pytest.mark.parametrize('explicit', [False, True], ids=['discovery', 'explicit'])
def test_unrelated_drifted_binding_does_not_block_new_issue(fixture, explicit):
    root, _, request, issue, state = fixture
    first = pull_sync(root)
    first_path = root / first['pack_path']
    first_bytes = first_path.read_bytes()
    issue['body'] += '\n# drifted after processing'
    state['issues'][2] = second_issue(request, issue)

    report = pull_sync(root, issue_number=2 if explicit else None)

    assert report['transport']['issue_number'] == 2
    assert any('issue #1: reconciliation needed' in warning for warning in report['warnings'])
    assert first_path.read_bytes() == first_bytes
    assert len(list((root / 'inbox/sync').glob('*.yaml'))) == 2


def test_wrong_marker_is_ignored_but_explicit_selection_errors(fixture):
    root, _, _, issue, _ = fixture
    issue['title'] = 'Ordinary issue mentioning [SYNC REQUEST] later'
    assert pull_sync(root)['status'] == 'no_pending'
    with pytest.raises(SyncPullError, match='marker'):
        pull_sync(root, issue_number=1)
    no_pack(root)


@pytest.mark.parametrize('mutation,match', [
    ('author', 'unauthorized'), ('yaml', 'parse'), ('duplicate-key', 'duplicate'),
    ('traversal', 'unsafe'), ('unknown-target', 'missing'), ('extra-project', 'Additional'),
    ('multiple-documents', 'parse'), ('multiple-fences', 'exactly one'),
    ('date', 'date-time'), ('pull-request', 'pull request'),
    ('wrong-url', 'wrong repository'), ('duplicate-change', 'duplicate change'),
    ('missing-proposal', 'proposal'),
])
def test_invalid_issue_never_creates_pack_or_ack(fixture, mutation, match):
    root, _, request, issue, _ = fixture
    before = canonical(root)
    if mutation == 'author': issue['user']['login'] = 'untrusted'
    elif mutation == 'yaml': issue['body'] = 'schema_version: ['
    elif mutation == 'duplicate-key': issue['body'] += '\nschema_version: 1\n'
    elif mutation == 'multiple-documents': issue['body'] += '\n---\n' + issue['body']
    elif mutation == 'multiple-fences': issue['body'] = '```yaml\n' + issue['body'] + '\n```\n```yaml\na: b\n```'
    elif mutation == 'date': issue['updated_at'] = 'not-a-date'
    elif mutation == 'pull-request': issue['pull_request'] = {}
    elif mutation == 'wrong-url': issue['html_url'] = 'https://github.com/wrong/repo/issues/1'
    else:
        if mutation == 'traversal':
            request['changes'] = [{'change_id': 'escape', 'kind': 'narrative_impact', 'summary': 'test',
                                   'narrative_paths': ['docs/../escape.md']}]
            request['expected_targets'] = ['docs/../escape.md']
        elif mutation == 'unknown-target': request['changes'][0]['target_id'] = 'FEAT-20260907-00000000'
        elif mutation == 'duplicate-change': request['changes'].append(deepcopy(request['changes'][0]))
        elif mutation == 'missing-proposal':
            request['changes'] = [{'change_id': 'open', 'kind': 'unresolved', 'summary': 'Still undecided.'}]
            request['expected_targets'] = []
        else: request['project_id'] = 'injected'
        issue['body'] = yaml.safe_dump(request)
    with pytest.raises(SyncPullError, match=match): pull_sync(root)
    no_pack(root)
    assert canonical(root) == before


def test_single_fence_and_author_case(fixture):
    root, _, _, issue, _ = fixture
    issue['body'] = '```yaml\n' + issue['body'] + '```\n'
    issue['user']['login'] = 'OWNER'
    assert pull_sync(root)['status'] == 'created'


@pytest.mark.parametrize('encoding,fenced', [
    ('yaml', False), ('json', False), ('yaml', True), ('json', True),
], ids=['raw-yaml', 'raw-json', 'fenced-yaml', 'fenced-json'])
def test_request_transport_modes(fixture, encoding, fenced):
    root, _, request, issue, _ = fixture
    payload = (yaml.safe_dump(request, sort_keys=False, allow_unicode=True)
               if encoding == 'yaml' else json.dumps(request, ensure_ascii=False))
    issue['body'] = f'```{encoding}\n{payload}\n```\n' if fenced else f'\n{payload}\n'
    assert pull_sync(root)['status'] == 'created'


def test_malformed_fenced_request_is_rejected(fixture):
    root, _, _, issue, _ = fixture
    issue['body'] = '```yaml\nschema_version: [\n```\n'
    with pytest.raises(SyncPullError, match='parse'):
        pull_sync(root)
    no_pack(root)


def test_one_fence_with_inert_prose_and_full_body_hash(fixture):
    root, _, _, issue, _ = fixture
    issue['body'] = 'Transport test only; do not execute this prose.\n\n```yaml\n' + issue['body'] + '```\n\nFooter.'
    report = pull_sync(root)
    assert report['transport']['body_sha256'] == sha256(issue['body'].encode()).hexdigest()
    assert report['transport']['body_sha256'] != report['transport']['request_sha256']
    issue['body'] = issue['body'].replace('Footer.', 'Changed footer.')
    with pytest.raises(SyncPullError, match='drift/conflict'): pull_sync(root, issue_number=1)


def second_issue(request, issue, *, duplicate=False):
    other = deepcopy(issue)
    other['number'] = 2
    other['html_url'] = f'https://github.com/{REPO}/issues/2'
    request = deepcopy(request)
    if not duplicate: request['request_id'] += '-two'
    other['body'] = yaml.safe_dump(request, sort_keys=False, allow_unicode=True)
    return other


def test_multiple_candidates_require_selection_and_no_pending(fixture):
    root, _, request, issue, state = fixture
    state['issues'][2] = second_issue(request, issue)
    with pytest.raises(SyncPullError, match='multiple pending'): pull_sync(root)
    no_pack(root)
    assert pull_sync(root, issue_number=1)['transport']['issue_number'] == 1
    assert pull_sync(root)['transport']['issue_number'] == 2
    assert pull_sync(root)['status'] == 'no_pending'
    assert len(list((root / 'inbox/sync').glob('*.yaml'))) == 2


@pytest.mark.parametrize('already_processed', [False, True])
def test_duplicate_request_across_issues(fixture, already_processed):
    root, _, request, issue, state = fixture
    if already_processed: pull_sync(root)
    state['issues'][2] = second_issue(request, issue, duplicate=True)
    with pytest.raises(SyncPullError, match='duplicate request_id'): pull_sync(root)
    assert len(list((root / 'inbox/sync').glob('*.yaml'))) == int(already_processed)


def test_explicit_issue_rejects_duplicate_request_across_open_candidates(fixture):
    root, _, request, issue, state = fixture
    state['issues'][2] = second_issue(request, issue, duplicate=True)
    with pytest.raises(SyncPullError, match='duplicate request_id'):
        pull_sync(root, issue_number=2)
    no_pack(root)


def test_prior_direct_intake_cannot_be_silently_overwritten_with_transport(fixture):
    root, _, _, issue, _ = fixture
    path, _ = intake_sync(root, '-', stdin=BytesIO(issue['body'].encode()))
    before = path.read_bytes()
    with pytest.raises(SyncPullError, match='different or changed transport'): pull_sync(root)
    assert path.read_bytes() == before


def test_intake_of_pulled_bytes_remains_compatible(fixture):
    root, _, _, issue, _ = fixture
    first = pull_sync(root)
    path, report = intake_sync(root, '-', stdin=BytesIO(issue['body'].encode()))
    assert path == root / first['pack_path'] and report['reused']
    assert report['transport'] == first['transport']


def test_no_pending_requests(fixture):
    root, _, _, _, state = fixture
    state['issues'].clear()
    assert pull_sync(root, plan=True)['status'] == 'no_pending'
    no_pack(root)


@pytest.mark.parametrize('value', [None, {'allowed_authors': []}, {'allowed_authors': ['owner', 'OWNER']},
                                 {'allowed_authors': ['owner'], 'expected_repository': 'other/project'}])
def test_fail_closed_local_policy(fixture, value):
    root, _, _, _, state = fixture
    path = root / 'project.yaml'
    config = yaml.safe_load(path.read_text())
    config['external_systems']['github']['sync_pull'] = value
    path.write_text(yaml.safe_dump(config), encoding='utf-8')
    with pytest.raises(SyncPullError): pull_sync(root)
    assert state['calls'] == []
    no_pack(root)


@pytest.mark.parametrize('remote', ['https://github.com/example-owner/example-project.git',
                                   'ssh://git@github.com/example-owner/example-project.git'])
def test_github_remote_formats(fixture, remote):
    root, *_ = fixture
    git(root, 'remote', 'set-url', 'origin', remote)
    assert pull_sync(root)['status'] == 'created'


@pytest.mark.parametrize('remote', ['D:/some/repo', 'https://github.com.evil/owner/repo',
                                   'https://github.com/owner/../repo', 'https://token@github.com/owner/repo',
                                   'https://github.com/owner/repo?x=1', 'git@github.com:owner/..'])
def test_invalid_remote_never_invokes_gh(fixture, remote):
    root, _, _, _, state = fixture
    git(root, 'remote', 'set-url', 'origin', remote)
    with pytest.raises(SyncPullError): pull_sync(root)
    assert state['calls'] == []


def test_plan_dirty_baseline_never_writes_pack(fixture):
    root, obj, *_ = fixture
    obj.write_text(obj.read_text(encoding='utf-8') + '\nUncommitted.\n', encoding='utf-8')
    with pytest.raises(SyncPullError, match='clean working tree'): pull_sync(root, plan=True)
    no_pack(root)


@pytest.mark.parametrize('which', ['inbox/sync', '.generated/sync'])
def test_output_escape_protection(fixture, tmp_path, which):
    root, *_ = fixture
    target, outside = root / which, tmp_path / 'outside'
    outside.mkdir()
    if target.exists(): shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    try: target.symlink_to(outside, target_is_directory=True)
    except OSError:
        result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(target), str(outside)], capture_output=True)
        if result.returncode: pytest.skip('No symlink/junction permission')
    with pytest.raises(SyncPullError, match='symlink|junction|escapes'): pull_sync(root)
    assert list(outside.iterdir()) == []


def test_failure_after_intake_does_not_ack_and_retry_recovers(fixture, monkeypatch):
    root, _, _, _, _ = fixture
    original = sync_pull.gh_get
    count = 0

    def fail_last(root, endpoint, fields=()):
        nonlocal count
        if endpoint.startswith('repos/'):
            count += 1
            if count == 3: raise SyncPullError('simulated gh outage after intake')
        return original(root, endpoint, fields)

    monkeypatch.setattr(sync_pull, 'gh_get', fail_last)
    with pytest.raises(SyncPullError, match='outage'): pull_sync(root)
    assert len(list((root / 'inbox/sync').glob('*.yaml'))) == 1
    assert not list((root / '.generated').rglob('pull.json'))
    monkeypatch.setattr(sync_pull, 'gh_get', original)
    assert pull_sync(root)['status'] == 'already_processed'


@pytest.mark.parametrize('failure', ['missing', 'auth', 'timeout', 'bad-json'])
def test_gh_transport_failures_are_safe(tmp_path, monkeypatch, failure):
    monkeypatch.setattr(sync_pull, '_gh_executable', lambda: 'fixture-gh')

    def run(args, **kwargs):
        assert args[:7] == ['fixture-gh', 'api', '--hostname', 'github.com', '--method', 'GET', 'search/issues']
        if failure == 'missing': raise FileNotFoundError()
        if failure == 'timeout': raise subprocess.TimeoutExpired(args, 30)
        return subprocess.CompletedProcess(args, 1 if failure == 'auth' else 0, b'not json', b'SECRET_TOKEN_NEVER_ECHO')

    monkeypatch.setattr(subprocess, 'run', run)
    with pytest.raises((SyncPullError, sync_pull.SyncPlanError)) as exc: sync_pull.gh_get(tmp_path, 'search/issues')
    assert 'SECRET_TOKEN' not in str(exc.value)


def test_incomplete_search_is_not_reported_as_empty(fixture, monkeypatch):
    root, *_ = fixture
    monkeypatch.setattr(sync_pull, 'gh_get', lambda *args: {'items': [], 'total_count': 0, 'incomplete_results': True})
    with pytest.raises(SyncPullError, match='incomplete'): pull_sync(root)
    no_pack(root)


@pytest.mark.parametrize('number', ['../../other/repo', 2])
def test_explicit_issue_identity_cannot_inject_endpoint(fixture, monkeypatch, number):
    root, _, _, issue, _ = fixture
    calls = []

    def wrong_identity(root, endpoint, fields=()):
        calls.append(endpoint)
        if endpoint == 'search/issues':
            return {'total_count': 0, 'incomplete_results': False, 'items': []}
        return {**issue, 'number': number}

    monkeypatch.setattr(sync_pull, 'gh_get', wrong_identity)
    with pytest.raises(SyncPullError, match='issue number'): pull_sync(root, issue_number=1)
    assert calls == ['search/issues', f'repos/{REPO}/issues/1']
    no_pack(root)


@pytest.mark.parametrize('at_read', [2, 3])
def test_issue_change_during_pull_never_acknowledged(fixture, monkeypatch, at_read):
    root, _, _, issue, _ = fixture
    original, count = sync_pull.gh_get, 0

    def changing_issue(root, endpoint, fields=()):
        nonlocal count
        if endpoint.startswith('repos/'):
            count += 1
            if count == at_read: issue['body'] += '\n# transport changed'
        return original(root, endpoint, fields)

    monkeypatch.setattr(sync_pull, 'gh_get', changing_issue)
    with pytest.raises(SyncPullError, match='drift/conflict'): pull_sync(root)
    assert len(list((root / 'inbox/sync').glob('*.yaml'))) == (1 if at_read == 3 else 0)
    assert not list((root / '.generated').rglob('pull.json'))


@pytest.mark.parametrize('args', [['sync', 'pull', '--commit'], ['sync', 'intake', '-', '--issue', '1'],
                                 ['sync', 'pull', 'injected-path'], ['sync', 'pull', '--issue', '-1']])
def test_cli_option_boundaries(fixture, monkeypatch, args):
    root, *_ = fixture
    monkeypatch.chdir(root)
    with pytest.raises(SystemExit) as exc: main(args)
    assert exc.value.code == 2
