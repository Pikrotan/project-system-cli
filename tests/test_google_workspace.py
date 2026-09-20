from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import socket
import sys
from types import SimpleNamespace

import pytest
import yaml

from project_system.cli import main
from project_system.google_api import (
    DOC_MIME, FOLDER_MIME, GOOGLE_HTTP_TIMEOUT_SECONDS, SHEET_MIME,
    GoogleApiError, GoogleApiGateway, GoogleOAuthClient, GooglePermissionError,
    GoogleRateLimitError, GoogleResourceMissingError, GoogleTransientError,
    _translate_api_error,
)
from project_system.google_bindings import (
    GoogleBindingError, design_imports, google_store, load_workspace_binding,
)
from project_system.google_credentials import (
    GOOGLE_AUTH_URI, GOOGLE_SCOPES, GOOGLE_TOKEN_URI, GoogleAuthRequired, GoogleCredentialError,
    GoogleCredentialManager, WindowsDpapiCredentialStore,
    validate_client_configuration,
)
from project_system.google_design import (
    DESIGN_CHANGE_HEADERS, lifecycle_feedback, process_design_changes,
    row_designer_hash,
)
from project_system.google_workspace import (
    GoogleWorkspaceConflictError, google_auto_cycle, initialize_workspace,
    rebind_workspace, sync_workspace, workspace_status,
)
from project_system.google_projection import build_projection
from project_system.init_project import init_project
from project_system.objects import create_object
from project_system.schemas import validate_project_schema
from project_system.sync_pickup import pickup_once
from project_system.sync_auto import SyncAutoError, _project_binding
from project_system.sync_watcher import SyncWatcherError, run_watcher


NOW = datetime(2026, 9, 15, 10, 0, tzinfo=timezone.utc)
REPOSITORY = 'owner/universal-project'
CLIENT = {'installed': {
    'client_id': 'fixture-client.invalid',
    'client_secret': 'fixture-secret-not-real',
    'auth_uri': GOOGLE_AUTH_URI,
    'token_uri': GOOGLE_TOKEN_URI,
    'redirect_uris': ['http://localhost'],
}}
TOKEN = {
    'refresh_token': 'fixture-refresh-not-real',
    'token_uri': GOOGLE_TOKEN_URI,
    'scopes': list(GOOGLE_SCOPES),
}


def git(root, *args):
    result = subprocess.run(
        ['git', *args], cwd=root, text=True, capture_output=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def project_fixture(tmp_path, *, google=True, github=False):
    root = init_project('Universal Project', tmp_path / 'project')
    config_path = root / 'project.yaml'
    config = yaml.safe_load(config_path.read_text(encoding='utf-8'))
    if google:
        config['external_systems']['google_workspace'] = {
            'enabled': True,
            'project_overview': True,
            'design_knowledge': True,
            'design_changes': True,
            'projection_drift': 'restore',
        }
    if github:
        config['external_systems']['github'].update(
            enabled=True, mode='sync',
            sync_pull={'allowed_authors': ['owner'], 'expected_repository': REPOSITORY},
        )
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding='utf-8')
    (root / 'docs/01_VISION.md').write_text(
        '# Vision\n\nCanonical product value.\n', encoding='utf-8',
    )
    git(root, 'init', '-q')
    git(root, 'config', 'user.name', 'Google Tests')
    git(root, 'config', 'user.email', 'google@example.invalid')
    git(root, 'remote', 'add', 'origin', f'https://github.com/{REPOSITORY}.git')
    git(root, 'add', '.')
    git(root, 'commit', '-qm', 'baseline')
    return root


class MemoryStore:
    def __init__(self):
        self.values = {}

    def save(self, kind, value):
        self.values[kind] = deepcopy(value)

    def load(self, kind):
        return deepcopy(self.values.get(kind))

    def delete_all(self):
        present = bool(self.values)
        self.values.clear()
        return present


class FakeOAuth:
    def __init__(self):
        self.authorize_calls = []
        self.credential_calls = []

    def authorize(self, client, scopes):
        self.authorize_calls.append((deepcopy(client), tuple(scopes)))
        return deepcopy(TOKEN)

    def credentials(self, client, token, *, interactive):
        self.credential_calls.append((deepcopy(client), deepcopy(token), interactive))
        return 'credentials'


class FakeGateway:
    def __init__(self):
        self.resources = {}
        self.docs = {}
        self.rows = []
        self.counter = 0
        self.sheet_initialized = None
        self.protected_start = None
        self.replace_calls = []
        self.status_updates = []
        self.claim_hook = None
        self.fail_status_once = False

    def create_resource(self, name, mime_type, role, project_id, repository, *, parent=None):
        self.counter += 1
        resource_id = f'resource_{self.counter:04d}'
        resource = {
            'id': resource_id, 'name': name, 'mimeType': mime_type,
            'parents': [parent] if parent else [], 'createdTime': '2026-09-15T10:00:00Z',
            'trashed': False, 'appProperties': {
                'project_system_project_id': project_id,
                'project_system_repository': repository.lower(),
                'project_system_resource_role': role,
            },
        }
        self.resources[resource_id] = resource
        if mime_type == DOC_MIME:
            self.docs[resource_id] = ''
        return deepcopy(resource)

    def get_resource(self, resource_id):
        if resource_id not in self.resources:
            raise GoogleWorkspaceConflictError('missing fixture resource')
        return deepcopy(self.resources[resource_id])

    def find_project_resources(self, project_id, repository):
        return [
            deepcopy(value) for value in self.resources.values()
            if value['appProperties']['project_system_project_id'] == project_id
            and value['appProperties']['project_system_repository'] == repository.lower()
        ]

    def read_document_text(self, document_id):
        return self.docs[document_id]

    def replace_document_text(self, document_id, text):
        self.replace_calls.append((document_id, text))
        self.docs[document_id] = text

    def initialize_design_sheet(self, spreadsheet_id, headers, system_start):
        self.sheet_initialized = (spreadsheet_id, tuple(headers))
        self.protected_start = system_start

    def read_design_rows(self, spreadsheet_id):
        return deepcopy(self.rows)

    def read_design_row(self, spreadsheet_id, row_number):
        return deepcopy(next(item for item in self.rows if item['row_number'] == row_number))

    def claim_change_id(self, spreadsheet_id, row_number, expected_hash, change_id, hash_row):
        row = next(item for item in self.rows if item['row_number'] == row_number)
        assert hash_row(row['values']) == expected_hash
        values = row['values'] + [''] * (16 - len(row['values']))
        values[9] = change_id
        row['values'] = values
        if self.claim_hook:
            self.claim_hook(row)
        if hash_row(row['values']) != expected_hash:
            raise GoogleWorkspaceConflictError('row changed during fixture claim')
        return deepcopy(row)

    def update_design_status(self, spreadsheet_id, change_id, fields):
        if self.fail_status_once:
            self.fail_status_once = False
            raise GoogleWorkspaceConflictError('simulated status write interruption')
        matches = []
        for row in self.rows:
            values = row['values'] + [''] * (16 - len(row['values']))
            if values[9] == change_id:
                matches.append((row, values))
        if len(matches) != 1:
            raise GoogleWorkspaceConflictError('fixture Change ID missing or duplicated')
        row, values = matches[0]
        values[10:16] = [
            fields.get('status', ''), fields.get('decision_needed', ''),
            fields.get('canonical_objects', ''), fields.get('applied_in', ''),
            fields.get('processing_error', ''), fields.get('notes', ''),
        ]
        row['values'] = values
        self.status_updates.append((change_id, deepcopy(fields)))


def initialized(tmp_path):
    root = project_fixture(tmp_path)
    gateway = FakeGateway()
    report = initialize_workspace(root, gateway=gateway, clock=lambda: NOW)
    return root, gateway, report


def valid_row(*, change_id=''):
    return [
        '2026-09-15', 'Designer', 'Onboarding', 'Welcome', 'Layout',
        'Moved the primary action below the introduction.', 'Clearer hierarchy',
        'https://figma.com/file/example', 'No', change_id,
    ]


def test_old_config_remains_valid_and_google_config_is_exact(tmp_path):
    old = yaml.safe_load((init_project('Old', tmp_path / 'old') / 'project.yaml').read_text())
    assert validate_project_schema(old) == []
    old['external_systems']['google_workspace'] = {'enabled': True}
    assert validate_project_schema(old) == []
    old['external_systems']['google_workspace']['token'] = 'forbidden'
    assert any('Additional properties' in item for item in validate_project_schema(old))


def test_credential_manager_connect_status_disconnect_and_noninteractive_refresh(tmp_path):
    store, oauth = MemoryStore(), FakeOAuth()
    credentials = tmp_path / 'client.json'
    credentials.write_text(json.dumps(CLIENT), encoding='utf-8')
    manager = GoogleCredentialManager(store, oauth)
    assert manager.connect(credentials)['scopes'] == list(GOOGLE_SCOPES)
    assert manager.status()['status'] == 'connected'
    assert manager.credentials(background=True) == 'credentials'
    assert oauth.credential_calls[-1][-1] is False
    assert manager.disconnect()['status'] == 'disconnected'
    with pytest.raises(GoogleAuthRequired):
        manager.credentials(background=True)


def test_client_validation_rejects_web_or_non_local_desktop_configuration():
    with pytest.raises(GoogleCredentialError):
        validate_client_configuration({'web': CLIENT['installed']})
    invalid = deepcopy(CLIENT)
    invalid['installed']['redirect_uris'] = ['https://example.invalid/callback']
    with pytest.raises(GoogleCredentialError, match='localhost'):
        validate_client_configuration(invalid)
    invalid = deepcopy(CLIENT)
    invalid['installed']['token_uri'] = 'https://attacker.example.invalid/token'
    with pytest.raises(GoogleCredentialError, match='official Google endpoints'):
        validate_client_configuration(invalid)


@pytest.mark.parametrize('redirect', [
    'http://localhost.evil.example/callback',
    'http://localhost@evil.example/callback',
    'http://example.invalid/callback',
    'http:///callback',
    'http://[::1',
    'http://localhost:invalid/',
    'https://localhost/',
    'http://localhost\\@evil.example/',
])
def test_client_validation_rejects_lookalike_or_malformed_redirect(redirect):
    invalid = deepcopy(CLIENT)
    invalid['installed']['redirect_uris'] = [redirect]
    with pytest.raises(GoogleCredentialError, match='localhost redirect URI'):
        validate_client_configuration(invalid)


@pytest.mark.parametrize('redirect', ['http://localhost', 'http://localhost:8080/'])
def test_client_validation_accepts_real_desktop_localhost_redirect(redirect):
    client = deepcopy(CLIENT)
    client['installed']['redirect_uris'] = [redirect]
    assert validate_client_configuration(client) == client


def test_connect_rejects_link_like_and_oversized_client_files(tmp_path, monkeypatch):
    credentials = tmp_path / 'client.json'
    credentials.write_text(json.dumps(CLIENT), encoding='utf-8')
    manager = GoogleCredentialManager(MemoryStore(), FakeOAuth())
    monkeypatch.setattr(
        'project_system.google_credentials._is_link',
        lambda path: Path(path) == credentials.absolute(),
    )
    with pytest.raises(GoogleCredentialError, match='regular file'):
        manager.connect(credentials)
    monkeypatch.setattr('project_system.google_credentials._is_link', lambda path: False)
    credentials.write_bytes(b'x' * (256 * 1024 + 1))
    with pytest.raises(GoogleCredentialError, match='exceeds the size limit'):
        manager.connect(credentials)


def test_dpapi_store_separates_and_never_writes_plaintext(tmp_path):
    store = WindowsDpapiCredentialStore(
        tmp_path / 'protected',
        protector=lambda raw: b'PROTECTED:' + raw[::-1],
        unprotector=lambda raw: raw.removeprefix(b'PROTECTED:')[::-1],
    )
    store.save('client', CLIENT)
    store.save('token', TOKEN)
    raw = b''.join(path.read_bytes() for path in store.root.iterdir())
    assert b'fixture-secret' not in raw and b'fixture-refresh' not in raw
    assert store.load('client') == CLIENT and store.load('token') == TOKEN
    assert {path.name for path in store.root.iterdir()} == {
        'default.client.dpapi', 'default.token.dpapi',
    }


@pytest.mark.skipif(os.name != 'nt', reason='Windows DPAPI backend')
def test_real_windows_dpapi_round_trip_uses_no_plaintext(tmp_path):
    store = WindowsDpapiCredentialStore(tmp_path / 'dpapi')
    try:
        store.save('token', TOKEN)
    except GoogleCredentialError:
        pytest.skip('Windows test host has no loaded DPAPI user profile')
    assert b'fixture-refresh' not in next(store.root.iterdir()).read_bytes()
    assert store.load('token') == TOKEN
    assert store.delete_all() is True


@pytest.mark.parametrize(('status', 'expected'), [
    (403, GooglePermissionError),
    (404, GoogleResourceMissingError),
    (429, GoogleRateLimitError),
    (503, GoogleTransientError),
])
def test_google_api_failure_classification_is_sanitized(status, expected):
    error = SimpleNamespace(resp=SimpleNamespace(status=status))
    translated = _translate_api_error(error)
    assert isinstance(translated, expected)
    assert str(status) not in str(translated)


@pytest.mark.parametrize('error', [
    TimeoutError('SECRET timeout detail'),
    ConnectionResetError('SECRET reset detail'),
    socket.gaierror('SECRET DNS detail'),
])
def test_google_network_failure_without_http_status_is_retryable_and_sanitized(error):
    translated = _translate_api_error(error)
    assert isinstance(translated, GoogleTransientError)
    assert 'SECRET' not in str(translated)


def test_google_provider_transport_errors_without_http_status_are_retryable():
    from google.auth.exceptions import TransportError
    from httplib2 import ServerNotFoundError
    from requests.exceptions import ConnectionError as RequestsConnectionError
    from requests.exceptions import InvalidURL
    from requests.exceptions import Timeout as RequestsTimeout

    for error in (
        TransportError(RequestsTimeout('wrapped timeout detail')),
        RequestsConnectionError('connect detail'),
        RequestsTimeout('timeout detail'), ServerNotFoundError('DNS detail'),
    ):
        assert isinstance(_translate_api_error(error), GoogleTransientError)
    assert type(_translate_api_error(TransportError(InvalidURL('bad URL')))) is GoogleApiError
    assert type(_translate_api_error(TransportError('opaque detail'))) is GoogleApiError


@pytest.mark.parametrize('error', [ValueError('bug'), TypeError('bug')])
def test_google_programming_failure_is_not_retryable(error):
    translated = _translate_api_error(error)
    assert type(translated) is GoogleApiError


def test_google_oauth_refresh_uses_30_second_transport_timeout(monkeypatch):
    observed = {}

    class RefreshError(Exception):
        pass

    class Request:
        def __call__(self, url, *, timeout=120):
            observed['timeout'] = timeout

    class Credentials:
        def __init__(self, **kwargs):
            observed['scopes'] = kwargs['scopes']

        def refresh(self, request):
            request('https://oauth2.googleapis.com/token')

    monkeypatch.setitem(sys.modules, 'google', SimpleNamespace())
    monkeypatch.setitem(sys.modules, 'google.auth', SimpleNamespace())
    monkeypatch.setitem(sys.modules, 'google.auth.exceptions', SimpleNamespace(RefreshError=RefreshError))
    monkeypatch.setitem(sys.modules, 'google.auth.transport', SimpleNamespace())
    monkeypatch.setitem(sys.modules, 'google.auth.transport.requests', SimpleNamespace(Request=Request))
    monkeypatch.setitem(sys.modules, 'google.oauth2', SimpleNamespace())
    monkeypatch.setitem(sys.modules, 'google.oauth2.credentials', SimpleNamespace(Credentials=Credentials))
    assert isinstance(GoogleOAuthClient().credentials(CLIENT, TOKEN), Credentials)
    assert observed['timeout'] == GOOGLE_HTTP_TIMEOUT_SECONDS == 30
    assert observed['scopes'] == list(GOOGLE_SCOPES)


def test_google_gateway_uses_bounded_official_http_transport(monkeypatch):
    observed = {'builds': []}

    class Http:
        def __init__(self, *, timeout):
            observed['timeout'] = timeout

    class AuthorizedHttp:
        def __init__(self, credentials, *, http):
            observed['credentials'] = credentials
            observed['http'] = http

    def build(api, version, *, http, cache_discovery):
        observed['builds'].append((api, version, http, cache_discovery))
        return f'{api}-{version}'

    monkeypatch.setitem(sys.modules, 'httplib2', SimpleNamespace(Http=Http))
    monkeypatch.setitem(sys.modules, 'google_auth_httplib2', SimpleNamespace(AuthorizedHttp=AuthorizedHttp))
    monkeypatch.setitem(sys.modules, 'googleapiclient', SimpleNamespace())
    monkeypatch.setitem(sys.modules, 'googleapiclient.discovery', SimpleNamespace(build=build))
    gateway = GoogleApiGateway('credential-boundary')
    assert observed['timeout'] == GOOGLE_HTTP_TIMEOUT_SECONDS == 30
    assert observed['credentials'] == 'credential-boundary'
    assert [(api, version) for api, version, _, _ in observed['builds']] == [
        ('drive', 'v3'), ('docs', 'v1'), ('sheets', 'v4'),
    ]
    assert all(cache is False for _, _, _, cache in observed['builds'])
    assert gateway.drive == 'drive-v3'


def test_sheet_adapter_protects_all_system_columns_and_validates_logic_column():
    calls = []

    class Request:
        def __init__(self, result=None):
            self.result = result or {}

        def execute(self):
            return deepcopy(self.result)

    class Values:
        def update(self, **kwargs):
            calls.append(('values.update', kwargs))
            return Request()

    class Sheets:
        def spreadsheets(self):
            return self

        def values(self):
            return Values()

        def get(self, **kwargs):
            calls.append(('get', kwargs))
            return Request({'sheets': [{'properties': {'sheetId': 7, 'title': 'Sheet1'}}]})

        def batchUpdate(self, **kwargs):
            calls.append(('batchUpdate', kwargs))
            return Request()

    gateway = object.__new__(GoogleApiGateway)
    gateway.sheets = Sheets()
    gateway.initialize_design_sheet('sheet_123', DESIGN_CHANGE_HEADERS, 9)
    batch_requests = [
        request for name, kwargs in calls if name == 'batchUpdate'
        for request in kwargs['body']['requests']
    ]
    validation = next(item['setDataValidation'] for item in batch_requests if 'setDataValidation' in item)
    protection = next(item['addProtectedRange']['protectedRange'] for item in batch_requests if 'addProtectedRange' in item)
    assert validation['range']['startRowIndex'] == 1
    assert validation['range']['startColumnIndex'] == 8
    assert protection['range'] == {
        'sheetId': 7, 'startColumnIndex': 9,
        'endColumnIndex': len(DESIGN_CHANGE_HEADERS),
    }
    assert protection['range']['endColumnIndex'] == 16  # J:P; Q starts at index 16.
    assert protection['warningOnly'] is False


def test_workspace_init_metadata_binding_projection_and_status(tmp_path):
    root, gateway, report = initialized(tmp_path)
    assert report['status'] == 'initialized'
    assert gateway.sheet_initialized[1] == DESIGN_CHANGE_HEADERS
    assert gateway.protected_start == 9
    binding = load_workspace_binding(root)
    assert binding['repository'] == REPOSITORY
    assert set(binding['resources']) == set(('project_overview', 'design_knowledge', 'design_changes'))
    assert set(binding['projections']) == {'project_overview', 'design_knowledge'}
    binding_path, imports = google_store(root)
    assert '.git' in binding_path.parts and not str(binding_path).startswith(str(root / '.generated'))
    assert git(root, 'status', '--short') == ''
    status = workspace_status(root, gateway=gateway)
    assert status['status'] == 'bound' and status['import_count'] == 0
    assert all(value['status'] == 'current' for value in status['projections'].values())


def test_projection_manual_drift_is_restored_and_never_imported(tmp_path):
    root, gateway, report = initialized(tmp_path)
    binding = load_workspace_binding(root)
    document_id = binding['resources']['project_overview']['id']
    canonical = gateway.docs[document_id]
    gateway.docs[document_id] = 'MANUAL DOC TEXT MUST NOT ENTER GIT\n'
    result = sync_workspace(root, gateway=gateway, clock=lambda: NOW)
    assert result['projections']['project_overview']['status'] == 'drift_restored'
    assert gateway.docs[document_id] == canonical
    assert 'MANUAL DOC TEXT' not in (root / 'docs/01_VISION.md').read_text()
    assert git(root, 'status', '--short') == ''


def test_overview_projection_drops_only_embedded_leading_document_h1(tmp_path):
    root = project_fixture(tmp_path)
    (root / 'docs/01_VISION.md').write_text(
        '# Vision\n\n## Problem\n\nBody\n', encoding='utf-8',
    )
    projection = build_projection(root, 'project_overview', projected_at=NOW)
    lines = projection['text'].splitlines()
    assert 'Vision' not in lines
    assert 'Problem' in lines
    assert 'Body' in lines


def test_design_projection_does_not_duplicate_embedded_document_h1(tmp_path):
    root = project_fixture(tmp_path)
    (root / 'docs/design/DESIGN_OVERVIEW.md').write_text(
        '# Design Overview\n\n## Design Source of Truth\n\nBody\n', encoding='utf-8',
    )
    projection = build_projection(root, 'design_knowledge', projected_at=NOW)
    lines = projection['text'].splitlines()
    assert lines.count('Design Overview') == 1
    assert 'Design Source of Truth' in lines
    assert 'Body' in lines


def test_projection_refreshes_after_canonical_commit(tmp_path):
    root, gateway, report = initialized(tmp_path)
    path = root / 'docs/01_VISION.md'
    path.write_text('# Vision\n\nNew approved canonical value.\n', encoding='utf-8')
    git(root, 'add', 'docs/01_VISION.md')
    git(root, 'commit', '-qm', 'approved vision')
    result = sync_workspace(root, gateway=gateway, clock=lambda: NOW)
    assert result['projections']['project_overview']['status'] == 'updated'
    binding = load_workspace_binding(root)
    assert 'New approved canonical value' in gateway.docs[
        binding['resources']['project_overview']['id']
    ]
    assert git(root, 'status', '--short') == ''


def test_status_reports_committed_canonical_source_as_stale_until_sync(tmp_path):
    root, gateway, report = initialized(tmp_path)
    path = root / 'docs/01_VISION.md'
    path.write_text('# Vision\n\nCommitted later value.\n', encoding='utf-8')
    git(root, 'add', 'docs/01_VISION.md')
    git(root, 'commit', '-qm', 'later canonical source')
    status = workspace_status(root, gateway=gateway)
    assert status['projections']['project_overview']['status'] == 'stale_source'
    assert status['projections']['project_overview']['source_stale'] is True


def test_design_projection_marks_open_questions_as_unresolved(tmp_path):
    root = project_fixture(tmp_path)
    path, object_id = create_object(root, 'question', 'Which navigation?', 'design', 'owner')
    git(root, 'add', '.')
    git(root, 'commit', '-qm', 'question')
    gateway = FakeGateway()
    initialize_workspace(root, gateway=gateway, clock=lambda: NOW)
    binding = load_workspace_binding(root)
    text = gateway.docs[binding['resources']['design_knowledge']['id']]
    assert '[UNRESOLVED] Which navigation?' in text
    assert 'Open design questions — unresolved' in text


def test_projection_excludes_proposed_decision_and_finds_frontmatter_figma_url(tmp_path):
    root = project_fixture(tmp_path)
    proposed_path, _ = create_object(root, 'decision', 'Unapproved navigation', 'design', 'owner')
    text = proposed_path.read_text(encoding='utf-8')
    assert 'status: proposed' in text
    proposed_path.write_text(
        text.replace('domain: "design"', 'domain: "design"\nfigma_url: https://figma.com/file/frontmatter'),
        encoding='utf-8',
    )
    active_path, _ = create_object(root, 'feature', 'Approved visual system', 'design', 'owner')
    active_text = active_path.read_text(encoding='utf-8').replace(
        'status: idea', 'status: planned',
    ).replace('domain: "design"', 'domain: "design"\nfigma_url: https://figma.com/file/current')
    active_path.write_text(active_text, encoding='utf-8')
    git(root, 'add', '.')
    git(root, 'commit', '-qm', 'projection facts')
    overview = build_projection(root, 'project_overview', projected_at=NOW)
    design = build_projection(root, 'design_knowledge', projected_at=NOW)
    assert 'Unapproved navigation' not in overview['text']
    assert 'Unapproved navigation' not in design['text']
    assert 'https://figma.com/file/current' in design['text']
    assert 'https://figma.com/file/frontmatter' not in design['text']


def test_rebind_requires_unique_metadata_roles(tmp_path):
    root, gateway, report = initialized(tmp_path)
    binding_path, _ = google_store(root)
    binding_path.unlink()
    rebound = rebind_workspace(root, gateway=gateway, clock=lambda: NOW)
    assert rebound['status'] == 'rebound'
    binding_path.unlink()
    extra = deepcopy(next(
        item for item in gateway.resources.values()
        if item['appProperties']['project_system_resource_role'] == 'project_overview'
    ))
    extra['id'] = 'resource_duplicate'
    gateway.resources[extra['id']] = extra
    with pytest.raises(GoogleWorkspaceConflictError, match='exactly one'):
        rebind_workspace(root, gateway=gateway, clock=lambda: NOW)


def test_status_fails_closed_when_bound_resource_is_deleted(tmp_path):
    root, gateway, report = initialized(tmp_path)
    binding = load_workspace_binding(root)
    del gateway.resources[binding['resources']['design_knowledge']['id']]
    with pytest.raises(GoogleWorkspaceConflictError, match='missing'):
        workspace_status(root, gateway=gateway)


def test_corrupt_local_binding_fails_closed_without_remote_calls(tmp_path):
    root, gateway, report = initialized(tmp_path)
    binding_path, _ = google_store(root)
    binding_path.write_text('{"schema_version": 1}', encoding='utf-8')
    with pytest.raises(GoogleBindingError):
        workspace_status(root, gateway=gateway)


def test_binding_must_match_current_enabled_resource_policy(tmp_path):
    root, gateway, report = initialized(tmp_path)
    config_path = root / 'project.yaml'
    config = yaml.safe_load(config_path.read_text(encoding='utf-8'))
    config['external_systems']['google_workspace']['design_knowledge'] = False
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding='utf-8')
    git(root, 'add', 'project.yaml')
    git(root, 'commit', '-qm', 'change google policy')
    with pytest.raises(GoogleWorkspaceConflictError, match='rebind required'):
        workspace_status(root, gateway=gateway)


def test_design_change_import_is_stable_idempotent_and_reorder_safe(tmp_path, monkeypatch):
    root, gateway, report = initialized(tmp_path)
    binding = load_workspace_binding(root)
    gateway.rows = [{'row_number': 2, 'values': valid_row()}]
    monkeypatch.setattr('project_system.google_design.secrets.token_hex', lambda count: '1234abcd')
    first = process_design_changes(root, binding, gateway, clock=lambda: NOW)
    assert first['imported'] == 1
    change_id = gateway.rows[0]['values'][9]
    assert change_id == 'GDES-20260915-1234abcd'
    gateway.rows[0]['row_number'] = 8
    second = process_design_changes(root, binding, gateway, clock=lambda: NOW)
    assert second['reused'] == 1 and len(design_imports(root)) == 1
    assert gateway.rows[0]['values'][10] == 'IMPORTED — OWNER REVIEW'
    assert gateway.rows[0]['values'][11] == 'PENDING'
    assert git(root, 'status', '--short') == ''


def test_designer_logic_no_cannot_decide_canonical_need_before_owner_review(tmp_path):
    root, gateway, _ = initialized(tmp_path)
    binding = load_workspace_binding(root)
    row = valid_row()
    assert row[8] == 'No'
    gateway.rows = [{'row_number': 2, 'values': row}]
    result = process_design_changes(root, binding, gateway, clock=lambda: NOW)
    assert result['imported'] == 1
    assert gateway.rows[0]['values'][10] == 'IMPORTED — OWNER REVIEW'
    assert gateway.rows[0]['values'][11] == 'PENDING'


def test_row_reorder_during_identity_claim_uses_claimed_row_identity(tmp_path, monkeypatch):
    root, gateway, report = initialized(tmp_path)
    binding = load_workspace_binding(root)
    gateway.rows = [{'row_number': 2, 'values': valid_row()}]
    gateway.claim_hook = lambda row: row.__setitem__('row_number', 8)
    monkeypatch.setattr('project_system.google_design.secrets.token_hex', lambda count: 'bb22cc33')
    result = process_design_changes(root, binding, gateway, clock=lambda: NOW)
    assert result['imported'] == 1
    assert design_imports(root)[0]['initial_row_number'] == 8


def test_malformed_row_is_actionable_and_can_be_fixed(tmp_path, monkeypatch):
    root, gateway, report = initialized(tmp_path)
    binding = load_workspace_binding(root)
    row = valid_row()
    row[5] = ''
    gateway.rows = [{'row_number': 2, 'values': row}]
    monkeypatch.setattr('project_system.google_design.secrets.token_hex', lambda count: '2345bcde')
    first = process_design_changes(root, binding, gateway, clock=lambda: NOW)
    assert first['invalid'] == 1 and len(design_imports(root)) == 0
    assert 'What Changed is required' in gateway.rows[0]['values'][14]
    gateway.rows[0]['values'][5] = 'Added a visible empty state.'
    second = process_design_changes(root, binding, gateway, clock=lambda: NOW)
    assert second['imported'] == 1 and len(design_imports(root)) == 1


def test_edited_after_import_is_conflict_and_immutable_original_survives(tmp_path, monkeypatch):
    root, gateway, report = initialized(tmp_path)
    binding = load_workspace_binding(root)
    gateway.rows = [{'row_number': 2, 'values': valid_row()}]
    monkeypatch.setattr('project_system.google_design.secrets.token_hex', lambda count: '3456cdef')
    process_design_changes(root, binding, gateway, clock=lambda: NOW)
    original = deepcopy(design_imports(root)[0])
    gateway.rows[0]['values'][5] = 'Different edit after import.'
    result = process_design_changes(root, binding, gateway, clock=lambda: NOW)
    assert result['conflicts'] == 1
    assert design_imports(root)[0] == original
    assert 'immutable original preserved' in gateway.rows[0]['values'][14]


def test_workspace_cycle_fails_closed_on_edited_immutable_import(tmp_path, monkeypatch):
    root, gateway, report = initialized(tmp_path)
    gateway.rows = [{'row_number': 2, 'values': valid_row()}]
    monkeypatch.setattr('project_system.google_design.secrets.token_hex', lambda count: 'aa11bb22')
    sync_workspace(root, gateway=gateway, clock=lambda: NOW)
    gateway.rows[0]['values'][5] = 'Changed after immutable import.'
    with pytest.raises(GoogleWorkspaceConflictError, match='immutable import conflict'):
        sync_workspace(root, gateway=gateway, clock=lambda: NOW)
    assert len(design_imports(root)) == 1


def test_status_write_interruption_reuses_immutable_import_on_retry(tmp_path, monkeypatch):
    root, gateway, report = initialized(tmp_path)
    binding = load_workspace_binding(root)
    gateway.rows = [{'row_number': 2, 'values': valid_row()}]
    gateway.fail_status_once = True
    monkeypatch.setattr('project_system.google_design.secrets.token_hex', lambda count: '9abc0123')
    with pytest.raises(GoogleWorkspaceConflictError):
        process_design_changes(root, binding, gateway, clock=lambda: NOW)
    assert len(design_imports(root)) == 1
    retried = process_design_changes(root, binding, gateway, clock=lambda: NOW)
    assert retried['reused'] == 1
    assert gateway.rows[0]['values'][10] == 'IMPORTED — OWNER REVIEW'


def test_row_changed_during_identity_claim_creates_no_import(tmp_path, monkeypatch):
    root, gateway, report = initialized(tmp_path)
    binding = load_workspace_binding(root)
    gateway.rows = [{'row_number': 2, 'values': valid_row()}]
    gateway.claim_hook = lambda row: row['values'].__setitem__(5, 'Concurrent edit')
    monkeypatch.setattr('project_system.google_design.secrets.token_hex', lambda count: '4567def0')
    with pytest.raises(GoogleWorkspaceConflictError):
        process_design_changes(root, binding, gateway, clock=lambda: NOW)
    assert len(design_imports(root)) == 0


def test_duplicate_change_id_fails_closed(tmp_path):
    root, gateway, report = initialized(tmp_path)
    binding = load_workspace_binding(root)
    change_id = 'GDES-20260915-5678ef01'
    gateway.rows = [
        {'row_number': 2, 'values': valid_row(change_id=change_id)},
        {'row_number': 3, 'values': valid_row(change_id=change_id)},
    ]
    with pytest.raises(Exception, match='duplicate Change ID'):
        process_design_changes(root, binding, gateway, clock=lambda: NOW)
    assert len(design_imports(root)) == 0


def test_lifecycle_feedback_uses_human_controlled_pack_evidence(monkeypatch):
    pack = {
        'pack_id': 'SYNC-20260915-1234abcd',
        'source': {'type': 'google_design_change', 'ref': 'GDES-20260915-1234abcd'},
        'expected_targets': ['DEC-20260915-1234abcd'], 'changes': [],
    }
    terminal = {'terminal': {
        'outcome': 'pushed', 'commit_sha': 'a' * 40,
    }}
    monkeypatch.setattr(
        'project_system.google_design.intake_bindings',
        lambda root: [SimpleNamespace(state='completed', pack=pack, terminal=terminal)],
    )
    feedback = lifecycle_feedback(Path('.'))['GDES-20260915-1234abcd']
    assert feedback['status'] == 'APPLIED'
    assert feedback['canonical_objects'] == 'DEC-20260915-1234abcd'
    assert 'commit:' + 'a' * 40 in feedback['applied_in']


def test_google_sync_fails_closed_on_dirty_repository(tmp_path):
    root, gateway, report = initialized(tmp_path)
    (root / 'docs/01_VISION.md').write_text('dirty', encoding='utf-8')
    with pytest.raises(GoogleWorkspaceConflictError, match='clean'):
        sync_workspace(root, gateway=gateway, clock=lambda: NOW)


def test_workspace_init_fails_before_remote_creation_on_dirty_repository(tmp_path):
    root = project_fixture(tmp_path)
    (root / 'docs/01_VISION.md').write_text('dirty', encoding='utf-8')
    gateway = FakeGateway()
    with pytest.raises(GoogleWorkspaceConflictError, match='clean'):
        initialize_workspace(root, gateway=gateway, clock=lambda: NOW)
    assert gateway.resources == {}


def test_generated_output_refuses_non_file_target(tmp_path):
    root, gateway, report = initialized(tmp_path)
    target = root / '.generated/google/state.json'
    target.unlink()
    target.mkdir()
    with pytest.raises(GoogleWorkspaceConflictError, match='output target is unsafe'):
        sync_workspace(root, gateway=gateway, clock=lambda: NOW)


def test_pickup_google_disabled_is_legacy_and_enabled_composes_without_github(tmp_path, monkeypatch):
    root = project_fixture(tmp_path, google=False)
    legacy = pickup_once(root)
    assert legacy['status'] == 'blocked_config'
    config_path = root / 'project.yaml'
    config = yaml.safe_load(config_path.read_text())
    config['external_systems']['google_workspace'] = {'enabled': True}
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding='utf-8')
    git(root, 'add', 'project.yaml')
    git(root, 'commit', '-qm', 'enable google')
    composed = pickup_once(root, google_cycle=lambda value: {'status': 'processed', 'imported': 1})
    assert composed['status'] == 'processed'
    assert composed['google']['imported'] == 1


def test_google_does_not_mask_invalid_enabled_github_sync_policy(tmp_path):
    root = project_fixture(tmp_path)
    config_path = root / 'project.yaml'
    config = yaml.safe_load(config_path.read_text(encoding='utf-8'))
    config['external_systems']['github'].update(enabled=True, mode='sync')
    config['external_systems']['github'].pop('sync_pull', None)
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding='utf-8')
    git(root, 'add', 'project.yaml')
    git(root, 'commit', '-qm', 'invalid github sync policy')
    called = []
    result = pickup_once(root, google_cycle=lambda value: called.append(value))
    assert result['status'] == 'blocked_config'
    assert called == []


def test_existing_watcher_runs_google_branch_without_second_runtime(tmp_path, monkeypatch):
    root = project_fixture(tmp_path)
    monkeypatch.setattr(
        'project_system.google_workspace.google_auto_cycle',
        lambda value: {'status': 'processed', 'design_changes': {'imported': 1}},
    )
    result = run_watcher(
        root, interval=60, max_cycles=1,
        clock=lambda: NOW, sleeper=lambda seconds: None,
    )
    assert result['cycles'] == 1
    state = json.loads((root / '.generated/sync/auto/state.json').read_text())
    assert state['last_cycle_status'] == 'processed'
    assert git(root, 'status', '--short') == ''


@pytest.mark.parametrize('yaml_document', ['- item\n', 'plain-string\n', '42\n'])
def test_non_mapping_project_yaml_fails_closed_in_pickup_watcher_and_auto(tmp_path, yaml_document):
    root = project_fixture(tmp_path)
    (root / 'project.yaml').write_text(yaml_document, encoding='utf-8')
    result = pickup_once(root, google_cycle=lambda value: pytest.fail('Google cycle must not run'))
    assert result['status'] == 'blocked_config'
    assert 'top-level must be a mapping' in result['reason']
    with pytest.raises(SyncWatcherError, match='top-level must be a mapping'):
        run_watcher(root, interval=60, max_cycles=1)
    with pytest.raises(SyncAutoError, match='top-level must be a mapping'):
        _project_binding(root)


def test_background_auth_required_is_noninteractive_and_sanitized(tmp_path):
    root, gateway, report = initialized(tmp_path)

    class MissingManager:
        def credentials(self, *, background):
            assert background is True
            raise GoogleAuthRequired('SECRET REFRESH TOKEN')

    result = google_auto_cycle(root, manager=MissingManager(), clock=lambda: NOW)
    assert result == {
        'status': 'google_auth_required', 'error_category': 'google_auth_required',
    }
    state = (root / '.generated/google/state.json').read_text(encoding='utf-8')
    assert 'SECRET REFRESH TOKEN' not in state
    assert 'google_auth_required' in state


def test_cli_google_surface_uses_injected_boundaries(monkeypatch, capsys):
    monkeypatch.setattr('project_system.cli.GoogleCredentialManager', lambda: SimpleNamespace(
        connect=lambda path: {'status': 'connected', 'path_used': bool(path)},
        status=lambda: {'status': 'connected'},
        disconnect=lambda: {'status': 'disconnected'},
    ))
    main(['google', 'connect', '--credentials', 'client.json'])
    assert json.loads(capsys.readouterr().out)['status'] == 'connected'
    main(['google', 'status'])
    assert json.loads(capsys.readouterr().out)['status'] == 'connected'
    main(['google', 'disconnect'])
    assert json.loads(capsys.readouterr().out)['status'] == 'disconnected'
