"""Optional Stage 13B2b2-A test-only execution contracts and immutable boundaries.

No production approval, model dispatch, response sealing or successful Evidence.
Root/ancestor directories and published receipts must not be maliciously removed
or replaced. Atomic hard-link claims protect cooperating writers and process
crashes, not hostile writers or universal directory-entry power-loss durability.
"""

from dataclasses import dataclass
from functools import wraps
from hashlib import sha256
import hmac
import os
from pathlib import Path
import re
import secrets
import stat
import tempfile

from . import execution_fake
from .extraction_layer import _schema, _strict_json, validate_contract
from .extraction_pack import (
    MAX_PACK_CANONICAL_BYTES, _pack_path, _read_exact, _strict_pack_json,
    _verify_document,
)
from .intake_layer import inspect_intake_layer, intake_path
from .source_layer import SourceError, canonical_bytes, checked_path, project_identity
from .utils import load_yaml


PROFILE = 'project-system-execution-attempt-test-v1'
SCHEMA = 'execution-attempt.schema.json'
ATTEMPT_RE = re.compile(r'ATTEMPT-[0-9a-f]{32}')
MAX_CHECKPOINT_BYTES = 64 * 1024
MAX_INSTRUCTION_BYTES = 64 * 1024
SLOTS = ('01-prepared.json', '02-boundary.json', '03-disposition.json')
_TEST_ISSUER_KEY = secrets.token_bytes(32)


class AttemptError(SourceError):
    """Execution metadata or a dispatch boundary is not trustworthy."""


class OrphanAttemptError(AttemptError):
    """An empty durable directory cannot establish the attempt's execution history."""


def _controlled(function):
    @wraps(function)
    def call(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except AttemptError:
            raise
        except (SourceError, OSError, ValueError, TypeError, KeyError,
                AttributeError, RecursionError, NotImplementedError) as exc:
            raise AttemptError('execution attempt cannot complete safely') from exc
    return call


def _digest(value):
    return sha256(canonical_bytes(value)).hexdigest()


def _path(root, attempt_id, slot=None):
    if not isinstance(attempt_id, str) or not ATTEMPT_RE.fullmatch(attempt_id):
        raise AttemptError('invalid ATTEMPT ID')
    if slot is not None and slot not in SLOTS:
        raise AttemptError('invalid execution checkpoint slot')
    relative = f'intake/executions/{attempt_id}'
    return intake_path(root, relative + ('/' + slot if slot else ''))


def _project(root):
    if inspect_intake_layer(root).issues:
        raise AttemptError('unsafe or unknown intake namespace')
    return project_identity(load_yaml(checked_path(Path(root).absolute() / 'project.yaml')))


def _commitments(root, pack_id):
    raw, digest, size = _read_exact(
        _pack_path(root, pack_id), MAX_PACK_CANONICAL_BYTES, 'execution XPACK')
    pack = _strict_pack_json(raw)
    _verify_document(root, raw, pack, pack_id)
    contract_raw, contract_digest, _ = _read_exact(
        intake_path(root, f'intake/extraction-contracts/{pack["contract_id"]}.json'),
        4 * 1024 * 1024, 'execution XCON')
    # The complete immutable receipt bytes are committed, not just the XCON ID.
    validate_contract(_strict_json(contract_raw, schema='extraction-contract.schema.json',
                                  label='execution XCON'))
    return {
        'contract_id': pack['contract_id'], 'contract_sha256': contract_digest,
        'pack_id': pack_id, 'pack_sha256': digest, 'pack_bytes': size,
    }


def _checkpoint(contract, state, previous, data):
    document = {
        'schema_version': 1, 'profile': PROFILE,
        'project_id': contract['project_id'], 'attempt_id': contract['attempt_id'],
        'state': state, 'previous_sha256': previous, 'data': data,
    }
    document['checkpoint_sha256'] = _digest(document)
    _schema(document, SCHEMA, 'execution checkpoint')
    return document


def _read_checkpoint(path):
    raw, _, _ = _read_exact(path, MAX_CHECKPOINT_BYTES, 'execution checkpoint')
    document = _strict_json(raw, schema=SCHEMA, label='execution checkpoint')
    if raw != canonical_bytes(document):
        raise AttemptError('execution checkpoint is not canonical JSON')
    unsigned = {key: value for key, value in document.items()
                if key != 'checkpoint_sha256'}
    if document['checkpoint_sha256'] != _digest(unsigned):
        raise AttemptError('execution checkpoint integrity mismatch')
    if type(document['schema_version']) is not int:
        raise AttemptError('invalid execution schema version')
    return document


def _boundary(name):
    """Controlled crash injection seam; never an authorization or transport hook."""


def _publish(root, attempt_id, slot, document):
    destination = _path(root, attempt_id, slot)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination = _path(root, attempt_id, slot)
    raw = canonical_bytes(document)
    if len(raw) > MAX_CHECKPOINT_BYTES:
        raise AttemptError('execution checkpoint exceeds byte limit')
    # Orphaned staging is disposable, not an unrecognized durable checkpoint.
    staging = checked_path(Path(root).absolute() / '.generated/execution-staging')
    staging.mkdir(parents=True, exist_ok=True)
    staging = checked_path(staging)
    descriptor, name = tempfile.mkstemp(prefix='.attempt-', suffix='.tmp', dir=staging)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        _path(root, attempt_id, slot)
        _boundary('before_publication:' + slot)
        try:
            os.link(temporary, destination)
        except FileExistsError as exc:
            raise AttemptError('immutable checkpoint already exists; no redispatch') from exc
        except (OSError, NotImplementedError) as exc:
            raise AttemptError('atomic no-clobber publication unsupported; fail closed') from exc
        _boundary('after_publication:' + slot)
        if _read_checkpoint(_path(root, attempt_id, slot)) != document:
            raise AttemptError('published execution checkpoint mismatch')
    finally:
        temporary.unlink(missing_ok=True)


@dataclass(frozen=True)
class AttemptState:
    attempt_id: str
    state: str
    contract: dict
    last_sha256: str
    terminal: bool
    semantic_evidence: bool = False
    response_bytes_available: bool = False


@_controlled
def inspect_attempt(root, attempt_id):
    """Independent read-only validation; never trust a dispatch return value.

XPACK caches need not survive recovery. Their full commitment is checked against
the XPACK ID here; exact bytes and published verifier are required before dispatch.
"""
    project_id = _project(root)
    directory = _path(root, attempt_id)
    names = set()
    for child in directory.iterdir():
        path = _path(root, attempt_id, child.name)
        if not stat.S_ISREG(os.lstat(path).st_mode):
            raise AttemptError('execution checkpoint is not a regular file')
        names.add(child.name)
    if not names:
        raise OrphanAttemptError(
            'orphan ATTEMPT directory without PREPARED; execution history unverified; '
            'explicit operator intervention required; no automatic recovery')
    if SLOTS[0] not in names:
        raise AttemptError('missing PREPARED checkpoint')
    documents = {name: _read_checkpoint(_path(root, attempt_id, name))
                 for name in SLOTS if name in names}
    prepared = documents[SLOTS[0]]
    if prepared['state'] != 'PREPARED' or prepared['previous_sha256'] is not None:
        raise AttemptError('invalid initial execution checkpoint')
    contract = prepared['data']['contract']
    contract_sha = _digest(contract)
    if (contract['project_id'] != project_id or contract['attempt_id'] != attempt_id
            or prepared['data']['contract_sha256'] != contract_sha):
        raise AttemptError('execution contract project/identity/commitment mismatch')
    for field in ('pack_bytes', 'instruction_bytes'):
        if type(contract[field]) is not int:
            raise AttemptError(f'execution {field} must be an integer')
    if (contract['pack_id'] != 'XPACK-' + contract['pack_sha256'][:32]
            or contract['pack_bytes'] > contract['limits']['max_pack_bytes']):
        raise AttemptError('execution pack identity/limit mismatch')
    for value in contract['limits'].values():
        if type(value) is not int:
            raise AttemptError('execution limits must be integers')
    contract_raw, contract_digest, _ = _read_exact(
        intake_path(root, f'intake/extraction-contracts/{contract["contract_id"]}.json'),
        4 * 1024 * 1024, 'execution XCON')
    if contract_digest != contract['contract_sha256']:
        raise AttemptError('execution XCON full commitment mismatch')
    xcon = validate_contract(_strict_json(
        contract_raw, schema='extraction-contract.schema.json', label='execution XCON'))
    if xcon['project_id'] != project_id or xcon['contract_id'] != contract['contract_id']:
        raise AttemptError('execution XCON project/identity mismatch')
    if SLOTS[2] in documents and SLOTS[1] not in documents:
        raise AttemptError('disposition has no dispatch boundary')
    previous = None
    state = 'PREPARED'
    for name in SLOTS:
        doc = documents.get(name)
        if doc is None:
            continue
        if (doc['project_id'] != project_id or doc['attempt_id'] != attempt_id
                or doc['previous_sha256'] != previous):
            raise AttemptError('checkpoint project/attempt/link mismatch')
        if name != SLOTS[0]:
            authorization = doc['data']['authorization']
            expected = {'kind': 'test_only', 'contract_sha256': contract_sha}
            expected['authorization_sha256'] = _digest(expected)
            if authorization != expected:
                raise AttemptError('test authorization commitment mismatch')
        if name == SLOTS[1]:
            if doc['state'] not in {'DISPATCH_INTENT', 'DISPOSITION'}:
                raise AttemptError('illegal dispatch boundary')
            if doc['state'] == 'DISPOSITION' and doc['data']['outcome'] != 'abandoned':
                raise AttemptError('PREPARED can only be explicitly abandoned')
        if name == SLOTS[2]:
            if state != 'DISPATCH_INTENT' or doc['state'] != 'DISPOSITION':
                raise AttemptError('illegal terminal transition')
            if doc['data']['outcome'] != 'unresolved':
                raise AttemptError('unknown delivery can only be closed unresolved')
        state = doc['state']
        previous = doc['checkpoint_sha256']
    if state == 'DISPOSITION':
        last = documents[SLOTS[2]] if SLOTS[2] in documents else documents[SLOTS[1]]
        state = last['data']['outcome'].upper()
    elif state == 'DISPATCH_INTENT':
        state = 'DELIVERY_UNKNOWN'
    return AttemptState(attempt_id, state, contract, previous,
                        state in {'ABANDONED', 'UNRESOLVED'})


@dataclass(frozen=True)
class ExecutionLayer:
    attempts: dict
    issues: tuple


def inspect_execution_layer(root, config):
    """Optional namespace. Corrupt=ERROR; valid incomplete=BLOCKING; terminal=WARNING."""
    attempts, issues = {}, []
    try:
        directory = intake_path(root, 'intake/executions')
        try:
            info = os.lstat(directory)
        except FileNotFoundError:
            return ExecutionLayer({}, ())
        if not stat.S_ISDIR(info.st_mode):
            raise AttemptError('execution namespace is not a directory')
        project_identity(config)
        for child in sorted(directory.iterdir()):
            relative = 'intake/executions/' + child.name
            try:
                safe = intake_path(root, relative)
                if child.name == '.gitkeep':
                    if not stat.S_ISREG(os.lstat(safe).st_mode) or os.lstat(safe).st_size:
                        raise AttemptError('invalid execution placeholder')
                    continue
                result = inspect_attempt(root, child.name)
                attempts[child.name] = result
                issues.append(('WARNING' if result.terminal else 'BLOCKING', relative,
                               'test-only attempt: ' + result.state + '; no semantic Evidence'))
            except OrphanAttemptError as exc:
                issues.append(('ERROR', relative, str(exc)))
            except (SourceError, OSError, ValueError, TypeError):
                issues.append(('ERROR', relative, 'invalid execution attempt'))
    except (SourceError, OSError, ValueError, TypeError):
        issues.append(('ERROR', 'intake/executions', 'unsafe execution namespace'))
    return ExecutionLayer(attempts, tuple(issues))


@_controlled
def prepare_attempt(root, pack_id, instruction, *, options=None,
                    adapter='deterministic-fake', adapter_version='1', model='fake',
                    execution_mode='local_fake', allowed_destination='none',
                    retention='metadata_only', limits=None):
    if not isinstance(instruction, bytes) or not 0 < len(instruction) <= MAX_INSTRUCTION_BYTES:
        raise AttemptError('test instruction must be bounded nonempty bytes')
    contract = {
        'project_id': _project(root), 'attempt_id': 'ATTEMPT-' + secrets.token_hex(16),
        **_commitments(root, pack_id),
        'instruction_sha256': sha256(instruction).hexdigest(),
        'instruction_bytes': len(instruction),
        'adapter': {'id': adapter, 'version': adapter_version}, 'model': model,
        'options': {'scenario': 'success'} if options is None else options,
        'execution_mode': execution_mode, 'allowed_destination': allowed_destination,
        'limits': ({'max_pack_bytes': MAX_PACK_CANONICAL_BYTES,
                    'max_response_bytes': 65536, 'timeout_seconds': 30}
                   if limits is None else limits),
        'retention': retention, 'scope': 'extraction_only',
    }
    doc = _checkpoint(contract, 'PREPARED', None,
                      {'contract': contract, 'contract_sha256': _digest(contract)})
    for value in contract['limits'].values():
        if type(value) is not int:
            raise AttemptError('execution limits must be integers')
    if contract['pack_bytes'] > contract['limits']['max_pack_bytes']:
        raise AttemptError('execution pack exceeds authorized byte limit')
    _publish(root, contract['attempt_id'], SLOTS[0], doc)
    return inspect_attempt(root, contract['attempt_id'])


@dataclass(frozen=True)
class _TestAuthorization:
    contract_sha256: str
    signature: str


def _signature(contract_sha):
    return hmac.new(_TEST_ISSUER_KEY, contract_sha.encode('ascii'), 'sha256').hexdigest()


@_controlled
def authorize_test_attempt(root, attempt_id, *, expected_contract_sha256):
    """Explicit test harness capability, NOT human approval. Not persistable/resumable."""
    result = inspect_attempt(root, attempt_id)
    digest = _digest(result.contract)
    if result.terminal or not isinstance(expected_contract_sha256, str) or digest != expected_contract_sha256:
        raise AttemptError('explicit test authorization binding mismatch')
    return _TestAuthorization(digest, _signature(digest))


def _authorization(result, capability):
    digest = _digest(result.contract)
    if (type(capability) is not _TestAuthorization
            or not isinstance(capability.signature, str)
            or capability.contract_sha256 != digest
            or not hmac.compare_digest(capability.signature, _signature(digest))):
        raise AttemptError('fresh test-only authorization capability required')
    auth = {'kind': 'test_only', 'contract_sha256': digest}
    auth['authorization_sha256'] = _digest(auth)
    return auth


@_controlled
def dispatch_attempt(root, attempt_id, capability):
    result = inspect_attempt(root, attempt_id)
    authorization = _authorization(result, capability)
    if result.state != 'PREPARED':
        raise AttemptError('dispatch boundary already exists; no redispatch')
    actual = _commitments(root, result.contract['pack_id'])
    if any(result.contract[key] != value for key, value in actual.items()):
        raise AttemptError('execution input commitment changed')
    if result.contract['options']['scenario'] == 'before_dispatch':
        raise AttemptError('fake failure before dispatch; PREPARED remains')
    _boundary('before_dispatch_intent')
    intent = _checkpoint(result.contract, 'DISPATCH_INTENT', result.last_sha256,
                         {'authorization': authorization})
    _publish(root, attempt_id, SLOTS[1], intent)
    _boundary('after_dispatch_intent')
    # No caller-supplied executor. This module is the sole fake-only dispatch route.
    observation = 'fake_success'
    try:
        response = execution_fake.dispatch(result.contract)
        if not isinstance(response, bytes) or len(response) > result.contract['limits']['max_response_bytes']:
            observation = 'malformed_response'
        _boundary('after_response_observation')
        if result.contract['options']['scenario'] == 'interrupted_after_response':
            raise AttemptError('fake interruption after observation; delivery unknown')
    except execution_fake.FakeTransportError as exc:
        observation = str(exc)
    # Response state/sealing intentionally deferred. Observation is not durable truth.
    return {'attempt_id': attempt_id, 'observation': observation,
            'state': inspect_attempt(root, attempt_id).state,
            'semantic_evidence': False, 'response_bytes_available': False}


@_controlled
def close_test_attempt(root, attempt_id, capability, *, outcome):
    """Append terminal provenance only: abandoned before intent, unresolved after it."""
    result = inspect_attempt(root, attempt_id)
    auth = _authorization(result, capability)
    required = 'abandoned' if result.state == 'PREPARED' else 'unresolved'
    if result.terminal or outcome != required:
        raise AttemptError('invalid test-only terminal disposition')
    slot = SLOTS[1] if result.state == 'PREPARED' else SLOTS[2]
    doc = _checkpoint(result.contract, 'DISPOSITION', result.last_sha256,
                      {'authorization': auth, 'outcome': outcome})
    _publish(root, attempt_id, slot, doc)
    return inspect_attempt(root, attempt_id)
