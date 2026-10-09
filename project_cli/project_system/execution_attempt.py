"""Stage A v1 boundaries and Stage B1 v2 test-only execution observations.

No production approval, model dispatch, retained responses or semantic Evidence.
Root/ancestor directories and published receipts must not be maliciously removed
or replaced. Atomic hard-link claims protect cooperating writers and process
crashes, not hostile writers or universal directory-entry power-loss durability.
"""

from dataclasses import dataclass
from functools import wraps
from hashlib import sha256
import hmac
import json
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
PROFILE_V2 = 'project-system-execution-attempt-test-v2'
SCHEMA_V2 = 'execution-attempt-v2.schema.json'
XINV_PROFILE = 'project-system-execution-invocation-test-v1'
ATTEMPT_RE = re.compile(r'ATTEMPT-[0-9a-f]{32}')
MAX_CHECKPOINT_BYTES = 64 * 1024
MAX_INSTRUCTION_BYTES = 64 * 1024
SLOTS = ('01-prepared.json', '02-boundary.json', '03-disposition.json')
SLOTS_V2 = ('01-prepared.json', '02-boundary.json', '03-outcome.json')
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
    if slot is not None and slot not in (*SLOTS, SLOTS_V2[2]):
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


def _profile(version):
    if type(version) is not int or version not in (1, 2):
        raise AttemptError('unsupported execution schema version')
    return (PROFILE, SCHEMA) if version == 1 else (PROFILE_V2, SCHEMA_V2)


def _checkpoint(contract, state, previous, data, *, schema_version=1):
    profile, schema = _profile(schema_version)
    document = {
        'schema_version': schema_version, 'profile': profile,
        'project_id': contract['project_id'], 'attempt_id': contract['attempt_id'],
        'state': state, 'previous_sha256': previous, 'data': data,
    }
    document['checkpoint_sha256'] = _digest(document)
    _schema(document, schema, 'execution checkpoint')
    return document


def _read_checkpoint(path):
    raw, _, _ = _read_exact(path, MAX_CHECKPOINT_BYTES, 'execution checkpoint')
    return _parse_checkpoint(raw)


def _parse_checkpoint(raw):
    # Untrusted probe selects only the schema; strict parsing still rejects
    # duplicate keys/nonfinite numbers and validates the complete envelope.
    probe = json.loads(raw)
    if not isinstance(probe, dict):
        raise AttemptError('execution checkpoint must be an object')
    _, schema = _profile(probe.get('schema_version'))
    document = _strict_json(raw, schema=schema, label='execution checkpoint')
    if raw != canonical_bytes(document):
        raise AttemptError('execution checkpoint is not canonical JSON')
    unsigned = {key: value for key, value in document.items()
                if key != 'checkpoint_sha256'}
    if document['checkpoint_sha256'] != _digest(unsigned):
        raise AttemptError('execution checkpoint integrity mismatch')
    if type(document['schema_version']) is not int:
        raise AttemptError('invalid execution schema version')
    # V1 keeps its published read/schema boundary; inspect_attempt independently
    # enforces exact integers there. V2 also rejects them before publication.
    if document['schema_version'] == 2 and document['state'] == 'PREPARED':
        contract = document['data']['contract']
        for field in ('pack_bytes', 'instruction_bytes'):
            if type(contract[field]) is not int:
                raise AttemptError(f'execution {field} must be an integer')
        if any(type(value) is not int for value in contract['limits'].values()):
            raise AttemptError('execution limits must be integers')
    xinv = document['data'].get('xinv')
    if xinv is not None:
        if type(xinv['schema_version']) is not int:
            raise AttemptError('invalid XINV schema version')
        if xinv['response'] is not None and type(xinv['response']['bytes']) is not int:
            raise AttemptError('XINV response bytes must be an integer')
    return document


def _boundary(name):
    """Controlled crash injection seam; never an authorization or transport hook."""


def _publish(root, attempt_id, slot, document):
    raw = canonical_bytes(document)
    if len(raw) > MAX_CHECKPOINT_BYTES:
        raise AttemptError('execution checkpoint exceeds byte limit')
    _parse_checkpoint(raw)
    destination = _path(root, attempt_id, slot)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination = _path(root, attempt_id, slot)
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
    schema_version: int = 1
    prepared_sha256: str = None


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
    prepared = _read_checkpoint(_path(root, attempt_id, SLOTS[0]))
    version = prepared['schema_version']
    slots = SLOTS if version == 1 else SLOTS_V2
    if not names.issubset(slots):
        raise AttemptError('mixed or unknown execution checkpoint layout')
    documents = {name: prepared if name == slots[0] else _read_checkpoint(_path(root, attempt_id, name))
                 for name in slots if name in names}
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
    if slots[2] in documents and slots[1] not in documents:
        raise AttemptError('disposition has no dispatch boundary')
    previous = None
    state = 'PREPARED'
    for name in slots:
        doc = documents.get(name)
        if doc is None:
            continue
        if (doc['schema_version'] != version or doc['profile'] != prepared['profile']
                or doc['project_id'] != project_id or doc['attempt_id'] != attempt_id
                or doc['previous_sha256'] != previous):
            raise AttemptError('checkpoint project/attempt/link mismatch')
        if name != SLOTS[0]:
            authorization = doc['data']['authorization']
            expected = _authorization_record(contract_sha, prepared['checkpoint_sha256']
                                             if version == 2 else None)
            if authorization != expected:
                raise AttemptError('test authorization commitment mismatch')
        if name == SLOTS[1]:
            if doc['state'] not in {'DISPATCH_INTENT', 'DISPOSITION'}:
                raise AttemptError('illegal dispatch boundary')
            if doc['state'] == 'DISPOSITION' and doc['data']['outcome'] != 'abandoned':
                raise AttemptError('PREPARED can only be explicitly abandoned')
        if name == slots[2]:
            terminal_states = {'DISPOSITION'} if version == 1 else {'RESPONSE_RECORDED', 'UNRESOLVED'}
            if state != 'DISPATCH_INTENT' or doc['state'] not in terminal_states:
                raise AttemptError('illegal terminal transition')
            if version == 2:
                _verify_xinv(doc, prepared, documents[slots[1]])
            elif doc['data']['outcome'] != 'unresolved':
                raise AttemptError('unknown delivery can only be closed unresolved')
        state = doc['state']
        previous = doc['checkpoint_sha256']
    if state == 'DISPOSITION':
        last = documents[slots[2]] if slots[2] in documents else documents[slots[1]]
        state = last['data']['outcome'].upper()
    elif state == 'DISPATCH_INTENT':
        state = 'DELIVERY_UNKNOWN'
    return AttemptState(attempt_id, state, contract, previous,
                        state in {'ABANDONED', 'UNRESOLVED', 'RESPONSE_RECORDED'},
                        schema_version=version, prepared_sha256=prepared['checkpoint_sha256'])


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
                    retention='metadata_only', limits=None, schema_version=1):
    _profile(schema_version)
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
                      {'contract': contract, 'contract_sha256': _digest(contract)},
                      schema_version=schema_version)
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
    prepared_sha256: str = None


def _signature(contract_sha, prepared_sha=None):
    message = contract_sha if prepared_sha is None else PROFILE_V2 + ':' + contract_sha + ':' + prepared_sha
    return hmac.new(_TEST_ISSUER_KEY, message.encode('ascii'), 'sha256').hexdigest()


@_controlled
def authorize_test_attempt(root, attempt_id, *, expected_contract_sha256):
    """Explicit test harness capability, NOT human approval. Not persistable/resumable."""
    result = inspect_attempt(root, attempt_id)
    digest = _digest(result.contract)
    if result.terminal or not isinstance(expected_contract_sha256, str) or digest != expected_contract_sha256:
        raise AttemptError('explicit test authorization binding mismatch')
    prepared_sha = result.prepared_sha256 if result.schema_version == 2 else None
    return _TestAuthorization(digest, _signature(digest, prepared_sha), prepared_sha)


def _authorization(result, capability):
    digest = _digest(result.contract)
    prepared_sha = result.prepared_sha256 if result.schema_version == 2 else None
    if (type(capability) is not _TestAuthorization
            or not isinstance(capability.signature, str)
            or capability.contract_sha256 != digest
            or capability.prepared_sha256 != prepared_sha
            or not hmac.compare_digest(capability.signature, _signature(digest, prepared_sha))):
        raise AttemptError('fresh test-only authorization capability required')
    return _authorization_record(digest, prepared_sha)


def _authorization_record(digest, prepared_sha=None):
    auth = {'kind': 'test_only', 'contract_sha256': digest}
    if prepared_sha is not None:
        auth['prepared_sha256'] = prepared_sha
    auth['authorization_sha256'] = _digest(auth)
    return auth


def _xinv(contract, prepared_sha, intent_sha, authorization, observation, response):
    record = {
        'schema_version': 1, 'profile': XINV_PROFILE,
        'project_id': contract['project_id'], 'attempt_id': contract['attempt_id'],
        'prepared_sha256': prepared_sha, 'contract_sha256': _digest(contract),
        'dispatch_intent_sha256': intent_sha,
        'authorization_sha256': authorization['authorization_sha256'],
        'observation': observation, 'response': response, 'retention': 'metadata_only',
        'canonical_authority': False, 'semantic_evidence': False,
        'model_authenticity_verified': False, 'human_approval_verified': False,
        'task_completion_claimed': False, 'response_bytes_available': False,
    }
    record['invocation_id'] = 'XINV-' + _digest(record)[:32]
    return record


def _verify_xinv(document, prepared, intent):
    record = document['data']['xinv']
    contract = prepared['data']['contract']
    observation = record['observation']
    expected_response = None
    scenario = contract['options']['scenario']
    if document['state'] == 'RESPONSE_RECORDED':
        if scenario != 'success' or observation != 'fake_success':
            raise AttemptError('XINV fake response protocol mismatch')
        expected_response = execution_fake.response_commitment()
        if expected_response['bytes'] > contract['limits']['max_response_bytes']:
            raise AttemptError('XINV response exceeds authorized limit')
    elif observation != 'operator_unresolved':
        expected = scenario
        if scenario == 'success' and execution_fake.response_commitment()['bytes'] > contract['limits']['max_response_bytes']:
            expected = 'malformed_response'
        if expected not in {'after_intent', 'timeout', 'unknown_delivery', 'malformed_response'} or observation != expected:
            raise AttemptError('XINV unresolved protocol mismatch')
    expected_record = _xinv(contract, prepared['checkpoint_sha256'], intent['checkpoint_sha256'],
                            intent['data']['authorization'], observation, expected_response)
    if record != expected_record:
        raise AttemptError('XINV attempt/authorization/boundary/response commitment mismatch')


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
                         {'authorization': authorization}, schema_version=result.schema_version)
    _publish(root, attempt_id, SLOTS[1], intent)
    _boundary('after_dispatch_intent')
    # No caller-supplied executor. This module is the sole fake-only dispatch route.
    observation = 'fake_success'
    try:
        response = execution_fake.dispatch(result.contract)
        observed_digest = None
        if result.schema_version == 2:
            scenario = result.contract['options']['scenario']
            if scenario in {'success', 'interrupted_after_response'}:
                if type(response) is not bytes:
                    raise AttemptError('unrecognized fake response; delivery unknown')
                observed_digest = {'sha256': sha256(response).hexdigest(), 'bytes': len(response)}
                if observed_digest != execution_fake.response_commitment():
                    raise AttemptError('unrecognized fake response; delivery unknown')
            elif scenario != 'malformed_response' or response is not None:
                raise AttemptError('unrecognized fake observation; delivery unknown')
        if not isinstance(response, bytes) or len(response) > result.contract['limits']['max_response_bytes']:
            observation = 'malformed_response'
        _boundary('after_response_observation')
        if result.contract['options']['scenario'] == 'interrupted_after_response':
            raise AttemptError('fake interruption after observation; delivery unknown')
    except execution_fake.FakeTransportError as exc:
        observation = str(exc)
        if result.schema_version == 2 and (observation != result.contract['options']['scenario']
                                          or observation not in {'after_intent', 'timeout', 'unknown_delivery'}):
            raise AttemptError('unrecognized fake transport result; delivery unknown') from exc
    if result.schema_version == 2:
        response_digest = observed_digest if observation == 'fake_success' else None
        state = 'RESPONSE_RECORDED' if response_digest is not None else 'UNRESOLVED'
        record = _xinv(result.contract, result.prepared_sha256, intent['checkpoint_sha256'],
                       authorization, observation, response_digest)
        outcome = _checkpoint(result.contract, state, intent['checkpoint_sha256'],
                              {'authorization': authorization, 'xinv': record}, schema_version=2)
        prepared = _read_checkpoint(_path(root, attempt_id, SLOTS[0]))
        _verify_xinv(outcome, prepared, intent)
        _publish(root, attempt_id, SLOTS_V2[2], outcome)
    # V1 preserves its published ephemeral-observation / unknown-delivery meaning.
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
    if result.schema_version == 2 and result.state == 'DELIVERY_UNKNOWN':
        slot = SLOTS_V2[2]
        record = _xinv(result.contract, result.prepared_sha256, result.last_sha256,
                       auth, 'operator_unresolved', None)
        doc = _checkpoint(result.contract, 'UNRESOLVED', result.last_sha256,
                          {'authorization': auth, 'xinv': record}, schema_version=2)
    else:
        doc = _checkpoint(result.contract, 'DISPOSITION', result.last_sha256,
                          {'authorization': auth, 'outcome': outcome}, schema_version=result.schema_version)
    _publish(root, attempt_id, slot, doc)
    return inspect_attempt(root, attempt_id)
