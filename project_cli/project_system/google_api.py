"""Official Google OAuth/API adapters behind deterministic internal boundaries."""
from copy import deepcopy
import errno
from functools import partial
from importlib import import_module
import socket

from .google_credentials import (
    GOOGLE_SCOPES, GoogleAuthRequired, GoogleError,
)


FOLDER_MIME = 'application/vnd.google-apps.folder'
DOC_MIME = 'application/vnd.google-apps.document'
SHEET_MIME = 'application/vnd.google-apps.spreadsheet'
GOOGLE_HTTP_TIMEOUT_SECONDS = 30


class GoogleApiError(GoogleError):
    category = 'google_api_error'


class GooglePermissionError(GoogleApiError):
    category = 'google_permission_denied'


class GoogleResourceMissingError(GoogleApiError):
    category = 'google_resource_missing'


class GoogleTransientError(GoogleApiError):
    category = 'google_transient'


class GoogleRateLimitError(GoogleTransientError):
    category = 'google_rate_limit'


def _translate_api_error(exc):
    status = getattr(getattr(exc, 'resp', None), 'status', None)
    if status in {401}:
        return GoogleAuthRequired('Google authorization is invalid or revoked')
    if status in {403}:
        return GooglePermissionError('Google API permission denied')
    if status in {404, 410}:
        return GoogleResourceMissingError('bound Google resource is missing')
    if status in {429}:
        return GoogleRateLimitError('Google API rate limit reached')
    if type(status) is int and (status >= 500 or status == 408):
        return GoogleTransientError('temporary Google API failure')
    if status is None and _is_transient_transport_error(exc):
        return GoogleTransientError('temporary Google transport failure')
    return GoogleApiError('Google API operation failed')


def _is_transient_transport_error(exc):
    if isinstance(exc, (TimeoutError, ConnectionError, socket.gaierror)):
        return True
    if isinstance(exc, OSError) and exc.errno in {
        errno.ETIMEDOUT, errno.ECONNRESET, errno.ECONNREFUSED,
        errno.ENETUNREACH, errno.EHOSTUNREACH, errno.EPIPE,
    }:
        return True
    try:
        from google.auth.exceptions import TransportError
    except ImportError:
        TransportError = ()
    if isinstance(exc, TransportError):
        cause = exc.__cause__ or (exc.args[0] if exc.args else None)
        return (
            isinstance(cause, BaseException) and cause is not exc
            and _is_transient_transport_error(cause)
        )
    for module, names in (
        ('requests.exceptions', ('ConnectionError', 'Timeout')),
        ('httplib2', ('ServerNotFoundError',)),
    ):
        try:
            transport_types = tuple(getattr(import_module(module), name) for name in names)
        except (ImportError, AttributeError):
            continue
        if isinstance(exc, transport_types):
            return True
    return False


def _execute(request):
    try:
        return request.execute()
    except Exception as exc:
        raise _translate_api_error(exc) from exc


class GoogleOAuthClient:
    """Interactive connect plus non-interactive refresh for normal operations."""

    def authorize(self, client, scopes):
        try:
            from google_auth_oauthlib.flow import InstalledAppFlow
            flow = InstalledAppFlow.from_client_config(client, scopes=list(scopes))
            credentials = flow.run_local_server(
                port=0, open_browser=True, access_type='offline', prompt='consent',
                authorization_prompt_message='Open this URL to authorize Project System: {url}',
                success_message='Project System authorization completed. You may close this window.',
            )
        except ImportError as exc:
            raise GoogleApiError('Google OAuth dependencies are not installed') from exc
        except Exception as exc:
            raise GoogleAuthRequired('interactive Google authorization failed') from exc
        return {
            'refresh_token': credentials.refresh_token,
            'token_uri': credentials.token_uri,
            'scopes': list(GOOGLE_SCOPES),
        }

    def credentials(self, client, token, *, interactive=False):
        if interactive:
            raise GoogleApiError('interactive refresh is not supported by this boundary')
        installed = client['installed']
        try:
            from google.auth.exceptions import RefreshError
            from google.auth.transport.requests import Request
            from google.oauth2.credentials import Credentials
        except ImportError as exc:
            raise GoogleApiError('Google authentication dependencies are not installed') from exc
        try:
            credentials = Credentials(
                token=None,
                refresh_token=token['refresh_token'],
                token_uri=token['token_uri'],
                client_id=installed['client_id'],
                client_secret=installed['client_secret'],
                scopes=list(GOOGLE_SCOPES),
            )
            credentials.refresh(partial(Request(), timeout=GOOGLE_HTTP_TIMEOUT_SECONDS))
            return credentials
        except RefreshError as exc:
            if _is_transient_transport_error(exc.__cause__):
                raise GoogleTransientError('temporary Google transport failure') from exc
            raise GoogleAuthRequired('Google authorization is invalid or revoked') from exc
        except Exception as exc:
            raise _translate_api_error(exc) from exc


class GoogleApiGateway:
    """Small Drive/Docs/Sheets surface used by the workspace engine."""

    def __init__(self, credentials):
        try:
            from google_auth_httplib2 import AuthorizedHttp
            from googleapiclient.discovery import build
            from httplib2 import Http
            transport = AuthorizedHttp(
                credentials, http=Http(timeout=GOOGLE_HTTP_TIMEOUT_SECONDS),
            )
            self.drive = build('drive', 'v3', http=transport, cache_discovery=False)
            self.docs = build('docs', 'v1', http=transport, cache_discovery=False)
            self.sheets = build('sheets', 'v4', http=transport, cache_discovery=False)
        except ImportError as exc:
            raise GoogleApiError('Google API dependencies are not installed') from exc
        except Exception as exc:
            raise _translate_api_error(exc) from exc

    @staticmethod
    def _properties(project_id, repository, role):
        return {
            'project_system_project_id': project_id,
            'project_system_repository': repository.lower(),
            'project_system_resource_role': role,
        }

    def create_resource(self, name, mime_type, role, project_id, repository, *, parent=None):
        body = {
            'name': name,
            'mimeType': mime_type,
            'appProperties': self._properties(project_id, repository, role),
        }
        if parent:
            body['parents'] = [parent]
        return _execute(self.drive.files().create(
            body=body, fields='id,name,mimeType,parents,appProperties,createdTime',
        ))

    def get_resource(self, resource_id):
        return _execute(self.drive.files().get(
            fileId=resource_id,
            fields='id,name,mimeType,trashed,parents,appProperties,createdTime,modifiedTime',
        ))

    def find_project_resources(self, project_id, repository):
        escaped_project = project_id.replace("'", "\\'")
        escaped_repo = repository.lower().replace("'", "\\'")
        query = (
            "trashed = false and "
            f"appProperties has {{ key='project_system_project_id' and value='{escaped_project}' }} and "
            f"appProperties has {{ key='project_system_repository' and value='{escaped_repo}' }}"
        )
        result = _execute(self.drive.files().list(
            q=query, spaces='drive', pageSize=100,
            fields='files(id,name,mimeType,parents,appProperties,createdTime),nextPageToken',
        ))
        if result.get('nextPageToken'):
            raise GoogleApiError('Google resource discovery exceeded the bounded result limit')
        return result.get('files', [])

    def read_document_text(self, document_id):
        document = _execute(self.docs.documents().get(documentId=document_id))
        chunks = []
        for item in document.get('body', {}).get('content', []):
            paragraph = item.get('paragraph') or {}
            for element in paragraph.get('elements', []):
                value = (element.get('textRun') or {}).get('content')
                if isinstance(value, str):
                    chunks.append(value)
        return ''.join(chunks)

    def replace_document_text(self, document_id, text):
        document = _execute(self.docs.documents().get(documentId=document_id))
        content = document.get('body', {}).get('content', [])
        end_index = max(
            [item.get('endIndex', 1) for item in content if isinstance(item.get('endIndex'), int)]
            or [1]
        )
        requests = []
        if end_index > 2:
            requests.append({'deleteContentRange': {'range': {'startIndex': 1, 'endIndex': end_index - 1}}})
        if text:
            requests.append({'insertText': {'location': {'index': 1}, 'text': text}})
        if requests:
            _execute(self.docs.documents().batchUpdate(
                documentId=document_id, body={'requests': requests},
            ))

    def initialize_design_sheet(self, spreadsheet_id, headers, system_start):
        spreadsheet = _execute(self.sheets.spreadsheets().get(
            spreadsheetId=spreadsheet_id, fields='sheets.properties',
        ))
        sheets = spreadsheet.get('sheets') or []
        if len(sheets) != 1:
            raise GoogleApiError('new Design Changes spreadsheet has an unexpected sheet layout')
        sheet_id = sheets[0]['properties']['sheetId']
        old_title = sheets[0]['properties']['title']
        if old_title != 'Design Changes':
            _execute(self.sheets.spreadsheets().batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={'requests': [{'updateSheetProperties': {
                    'properties': {'sheetId': sheet_id, 'title': 'Design Changes', 'gridProperties': {'frozenRowCount': 1}},
                    'fields': 'title,gridProperties.frozenRowCount',
                }}]},
            ))
        _execute(self.sheets.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id, range="'Design Changes'!A1:P1",
            valueInputOption='RAW', body={'values': [list(headers)]},
        ))
        _execute(self.sheets.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={'requests': [
                {'setDataValidation': {'range': {
                    'sheetId': sheet_id, 'startRowIndex': 1,
                    'startColumnIndex': 8, 'endColumnIndex': 9,
                }, 'rule': {'condition': {'type': 'ONE_OF_LIST', 'values': [
                    {'userEnteredValue': 'Yes'}, {'userEnteredValue': 'No'},
                    {'userEnteredValue': 'Unsure'},
                ]}, 'strict': True, 'showCustomUi': True}}},
                {'addProtectedRange': {'protectedRange': {
                    'range': {
                        'sheetId': sheet_id,
                        'startColumnIndex': system_start,
                        'endColumnIndex': len(headers),
                    },
                    'description': 'Project System managed columns',
                    'warningOnly': False,
                }}},
            ]},
        ))

    def read_design_rows(self, spreadsheet_id):
        result = _execute(self.sheets.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id, range="'Design Changes'!A2:P",
        ))
        return [
            {'row_number': number, 'values': list(values)}
            for number, values in enumerate(result.get('values', []), start=2)
        ]

    def read_design_row(self, spreadsheet_id, row_number):
        result = _execute(self.sheets.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range=f"'Design Changes'!A{row_number}:P{row_number}",
        ))
        values = result.get('values') or [[]]
        return {'row_number': row_number, 'values': list(values[0])}

    def claim_change_id(self, spreadsheet_id, row_number, expected_hash, change_id, hash_row):
        # Locate by exact designer payload immediately before the write.  This
        # tolerates a row reorder between the initial scan and identity claim;
        # duplicate blank payloads are intentionally ambiguous and fail closed.
        candidates = []
        for current in self.read_design_rows(spreadsheet_id):
            values = list(current['values']) + [''] * (16 - len(current['values']))
            if not values[9] and hash_row(values) == expected_hash:
                candidates.append(current)
        if len(candidates) != 1:
            raise GoogleApiError('Design Changes row changed or became ambiguous before identity assignment')
        row_number = candidates[0]['row_number']
        _execute(self.sheets.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range=f"'Design Changes'!J{row_number}", valueInputOption='RAW',
            body={'values': [[change_id]]},
        ))
        claimed = self.read_design_row(spreadsheet_id, row_number)
        values = list(claimed['values']) + [''] * (16 - len(claimed['values']))
        if values[9] != change_id or hash_row(values) != expected_hash:
            raise GoogleApiError('Design Changes row changed during identity assignment')
        return claimed

    def update_design_status(self, spreadsheet_id, change_id, fields):
        matches = []
        for row in self.read_design_rows(spreadsheet_id):
            values = row['values'] + [''] * (16 - len(row['values']))
            if values[9] == change_id:
                matches.append(row['row_number'])
        if len(matches) != 1:
            raise GoogleApiError('Design Change identity is missing or duplicated in the sheet')
        row = matches[0]
        values = [[
            fields.get('status', ''), fields.get('decision_needed', ''),
            fields.get('canonical_objects', ''), fields.get('applied_in', ''),
            fields.get('processing_error', ''), fields.get('notes', ''),
        ]]
        _execute(self.sheets.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range=f"'Design Changes'!K{row}:P{row}", valueInputOption='RAW',
            body={'values': values},
        ))
