"""Google OAuth bootstrap and protected local credential storage.

The repository never receives OAuth client configuration or user tokens.  The
Windows backend stores the two concepts as separately DPAPI-protected blobs.
Other operating-system backends can implement the same small store interface.
"""
from ctypes import POINTER, Structure, byref, c_char, c_void_p, cast, create_string_buffer, string_at
from ctypes import wintypes
import ctypes
import json
import os
from pathlib import Path
import tempfile
from urllib.parse import urlsplit


GOOGLE_SCOPES = ('https://www.googleapis.com/auth/drive.file',)
GOOGLE_AUTH_URI = 'https://accounts.google.com/o/oauth2/auth'
GOOGLE_TOKEN_URI = 'https://oauth2.googleapis.com/token'
MAX_CREDENTIAL_BYTES = 256 * 1024
STORE_KINDS = frozenset({'client', 'token'})


class GoogleError(RuntimeError):
    exit_code = 8
    category = 'google_error'


class GoogleConfigurationError(GoogleError):
    category = 'google_config'


class GoogleCredentialError(GoogleError):
    category = 'google_credential_error'


class GoogleAuthRequired(GoogleError):
    category = 'google_auth_required'


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise GoogleCredentialError(f'duplicate credential JSON key: {key}')
        result[key] = value
    return result


def _json_loads(raw, label):
    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_CREDENTIAL_BYTES:
        raise GoogleCredentialError(f'{label} is empty or exceeds the size limit')
    try:
        value = json.loads(raw.decode('utf-8-sig'), object_pairs_hook=_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GoogleCredentialError(f'{label} is not valid UTF-8 JSON') from exc
    if not isinstance(value, dict):
        raise GoogleCredentialError(f'{label} must contain a JSON object')
    return value


def validate_client_configuration(document):
    if not isinstance(document, dict) or set(document) != {'installed'}:
        raise GoogleCredentialError('OAuth client configuration must contain one installed app')
    installed = document.get('installed')
    required = {'client_id', 'client_secret', 'auth_uri', 'token_uri', 'redirect_uris'}
    if not isinstance(installed, dict) or not required <= set(installed):
        raise GoogleCredentialError('OAuth installed app configuration is incomplete')
    for field in ('client_id', 'client_secret', 'auth_uri', 'token_uri'):
        if not isinstance(installed.get(field), str) or not installed[field].strip():
            raise GoogleCredentialError(f'OAuth client field {field} is invalid')
    if installed['auth_uri'] != GOOGLE_AUTH_URI or installed['token_uri'] != GOOGLE_TOKEN_URI:
        raise GoogleCredentialError('OAuth Desktop App must use the official Google endpoints')
    redirects = installed.get('redirect_uris')
    if (not isinstance(redirects, list) or not redirects
            or any(not _valid_localhost_redirect(item) for item in redirects)):
        raise GoogleCredentialError('OAuth Desktop App must allow a localhost redirect URI')
    # Preserve official optional installed-app fields, but never another client type.
    return {'installed': dict(installed)}


def _valid_localhost_redirect(value):
    if not isinstance(value, str) or not value or any(
        char.isspace() or char == '\\' for char in value
    ):
        return False
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == 'http' and parsed.hostname == 'localhost'
        and parsed.username is None and parsed.password is None
        and parsed.path in {'', '/'} and not parsed.query and not parsed.fragment
        and (port is None or 1 <= port <= 65535)
    )


def validate_token_material(document):
    required = {'refresh_token', 'token_uri', 'scopes'}
    if not isinstance(document, dict) or set(document) != required:
        raise GoogleCredentialError('stored Google token material has an invalid structure')
    if not isinstance(document['refresh_token'], str) or not document['refresh_token']:
        raise GoogleCredentialError('Google OAuth did not provide long-lived refresh capability')
    if not isinstance(document['token_uri'], str) or not document['token_uri']:
        raise GoogleCredentialError('stored Google token endpoint is invalid')
    if document['token_uri'] != GOOGLE_TOKEN_URI:
        raise GoogleCredentialError('stored Google token endpoint differs from the Google policy')
    if document['scopes'] != list(GOOGLE_SCOPES):
        raise GoogleCredentialError('stored Google scopes differ from the least-privilege policy')
    return dict(document)


class _DataBlob(Structure):
    _fields_ = [('cbData', wintypes.DWORD), ('pbData', POINTER(c_char))]


def _input_blob(raw):
    buffer = create_string_buffer(raw)
    return _DataBlob(len(raw), cast(buffer, POINTER(c_char))), buffer


def _windows_crypto():
    crypt32 = ctypes.WinDLL('crypt32.dll', use_last_error=True)
    kernel32 = ctypes.WinDLL('kernel32.dll', use_last_error=True)
    crypt32.CryptProtectData.argtypes = [
        POINTER(_DataBlob), wintypes.LPCWSTR, POINTER(_DataBlob), c_void_p,
        c_void_p, wintypes.DWORD, POINTER(_DataBlob),
    ]
    crypt32.CryptProtectData.restype = wintypes.BOOL
    crypt32.CryptUnprotectData.argtypes = [
        POINTER(_DataBlob), POINTER(wintypes.LPWSTR), POINTER(_DataBlob),
        c_void_p, c_void_p, wintypes.DWORD, POINTER(_DataBlob),
    ]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [c_void_p]
    kernel32.LocalFree.restype = c_void_p
    return crypt32, kernel32


def _dpapi_protect(raw):
    if os.name != 'nt':
        raise GoogleCredentialError('protected Google credential storage is unavailable on this OS')
    source, keepalive = _input_blob(raw)
    output = _DataBlob()
    crypt32, kernel32 = _windows_crypto()
    if not crypt32.CryptProtectData(
        byref(source), wintypes.LPCWSTR('Project System Google Workspace'), None, None, None,
        0x1, byref(output),
    ):
        raise GoogleCredentialError('Windows could not protect Google credential material')
    try:
        return string_at(output.pbData, output.cbData)
    finally:
        kernel32.LocalFree(cast(output.pbData, c_void_p))


def _dpapi_unprotect(raw):
    if os.name != 'nt':
        raise GoogleCredentialError('protected Google credential storage is unavailable on this OS')
    source, keepalive = _input_blob(raw)
    output = _DataBlob()
    crypt32, kernel32 = _windows_crypto()
    if not crypt32.CryptUnprotectData(
        byref(source), None, None, None, None, 0x1, byref(output),
    ):
        raise GoogleCredentialError('stored Google credential material cannot be decrypted')
    try:
        return string_at(output.pbData, output.cbData)
    finally:
        kernel32.LocalFree(cast(output.pbData, c_void_p))


def _is_link(path):
    return path.is_symlink() or getattr(path, 'is_junction', lambda: False)()


class WindowsDpapiCredentialStore:
    """Separate DPAPI-protected client and user-token records."""

    def __init__(self, root=None, *, protector=None, unprotector=None):
        if root is None:
            local = os.environ.get('LOCALAPPDATA')
            if not local:
                raise GoogleCredentialError('LOCALAPPDATA is unavailable')
            self.anchor = Path(local).absolute().resolve()
            root = self.anchor / 'ProjectSystem' / 'google' / 'credentials'
        else:
            self.anchor = Path(root).absolute().parent.resolve()
        self.root = Path(root).absolute()
        self._protect = protector or _dpapi_protect
        self._unprotect = unprotector or _dpapi_unprotect

    def _path(self, kind, *, create=False):
        if kind not in STORE_KINDS:
            raise GoogleCredentialError('unknown credential record kind')
        current = self.root
        while current != self.anchor and current != current.parent:
            if current.exists() and _is_link(current):
                raise GoogleCredentialError('credential store must not use symlinks or junctions')
            current = current.parent
        try:
            self.root.resolve(strict=False).relative_to(self.anchor)
        except ValueError as exc:
            raise GoogleCredentialError('credential store escapes its protected local root') from exc
        if create:
            self.root.mkdir(parents=True, exist_ok=True)
        if not self.root.exists():
            return self.root / f'default.{kind}.dpapi'
        path = self.root / f'default.{kind}.dpapi'
        if path.exists() and (_is_link(path) or not path.is_file()):
            raise GoogleCredentialError('credential record is not a safe regular file')
        return path

    def save(self, kind, document):
        raw = json.dumps(
            document, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
        ).encode('utf-8')
        if len(raw) > MAX_CREDENTIAL_BYTES:
            raise GoogleCredentialError('credential material exceeds the size limit')
        encrypted = self._protect(raw)
        path = self._path(kind, create=True)
        handle, temporary = tempfile.mkstemp(
            prefix=f'.{path.name}.', suffix='.tmp', dir=str(path.parent),
        )
        try:
            with os.fdopen(handle, 'wb') as stream:
                stream.write(encrypted)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        except Exception:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise

    def load(self, kind):
        path = self._path(kind)
        if not path.is_file():
            return None
        try:
            with path.open('rb') as stream:
                encrypted = stream.read(MAX_CREDENTIAL_BYTES * 4 + 1)
            if not encrypted or len(encrypted) > MAX_CREDENTIAL_BYTES * 4:
                raise GoogleCredentialError('credential record is empty or oversized')
            return _json_loads(self._unprotect(encrypted), f'stored Google {kind}')
        except GoogleError:
            raise
        except OSError as exc:
            raise GoogleCredentialError('cannot read protected Google credentials') from exc

    def delete_all(self):
        removed = False
        for kind in sorted(STORE_KINDS):
            path = self._path(kind)
            if path.exists():
                path.unlink()
                removed = True
        return removed


def default_credential_store():
    if os.name != 'nt':
        raise GoogleCredentialError(
            'no protected credential backend is available for this operating system'
        )
    return WindowsDpapiCredentialStore()


class GoogleCredentialManager:
    def __init__(self, store=None, oauth=None):
        self.store = store or default_credential_store()
        self.oauth = oauth

    def _oauth(self):
        if self.oauth is None:
            from .google_api import GoogleOAuthClient
            self.oauth = GoogleOAuthClient()
        return self.oauth

    def connect(self, credentials_path=None):
        if credentials_path is None:
            client = self.store.load('client')
            if client is None:
                raise GoogleAuthRequired(
                    'first connection requires --credentials <OAuth Desktop App JSON>'
                )
            client = validate_client_configuration(client)
        else:
            candidate = Path(credentials_path).absolute()
            if _is_link(candidate) or not candidate.is_file():
                raise GoogleCredentialError('OAuth client configuration must be a regular file')
            path = candidate.resolve()
            try:
                with path.open('rb') as stream:
                    raw = stream.read(MAX_CREDENTIAL_BYTES + 1)
                client = validate_client_configuration(
                    _json_loads(raw, 'OAuth client configuration')
                )
            except OSError as exc:
                raise GoogleCredentialError('cannot read OAuth client configuration') from exc
        token = validate_token_material(
            self._oauth().authorize(client, GOOGLE_SCOPES)
        )
        self.store.save('client', client)
        self.store.save('token', token)
        return {'status': 'connected', 'scopes': list(GOOGLE_SCOPES), 'storage': 'protected_os'}

    def status(self):
        client = self.store.load('client')
        token = self.store.load('token')
        connected = client is not None and token is not None
        if client is not None:
            validate_client_configuration(client)
        if token is not None:
            validate_token_material(token)
        return {
            'status': 'connected' if connected else 'not_connected',
            'client_configured': client is not None,
            'token_present': token is not None,
            'scopes': list(GOOGLE_SCOPES) if connected else [],
            'storage': 'protected_os',
        }

    def disconnect(self):
        return {'status': 'disconnected' if self.store.delete_all() else 'not_connected'}

    def credentials(self, *, background=False):
        client = self.store.load('client')
        token = self.store.load('token')
        if client is None or token is None:
            raise GoogleAuthRequired('Google authorization is required')
        client = validate_client_configuration(client)
        token = validate_token_material(token)
        # This API is deliberately non-interactive.  In particular, background
        # execution never calls authorize() and can never open a browser.
        return self._oauth().credentials(client, token, interactive=False)
