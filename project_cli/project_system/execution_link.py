"""Test-only XLINK content proof; no executor authentication or semantic authority.

Immutable ancestors/receipts and cooperating publishers are assumed. This is not
an OS sandbox, signature scheme or universal power-loss directory guarantee.
"""
from dataclasses import dataclass
from functools import wraps
from hashlib import sha256
import os
from pathlib import Path
import re
import stat
import tempfile

from . import execution_attempt as execution
from .extraction_layer import (
    _strict_json, normalize_submission, normalized_proposal_manifest,
    proposal_manifest_sha256, proposal_id, PROPOSAL_PROFILE, validate_executor,
)
from .extraction_pack import _read_exact
from .intake_layer import intake_path
from .execution_fixture import _validation_batch, _validated_layers
from .source_layer import SourceError, canonical_bytes, checked_path


PROFILE = 'project-system-execution-content-link-test-v1'
SCHEMA = 'execution-link.schema.json'
XLINK_RE = re.compile(r'XLINK-[0-9a-f]{32}')
MAX_LINK_BYTES = 64 * 1024
FALSE_CLAIMS = (
    'canonical_authority', 'semantic_evidence', 'executor_attribution_verified',
    'model_authenticity_verified', 'human_approval_verified', 'task_completion_claimed',
)


class LinkError(SourceError):
    """A test content link cannot be safely published or verified."""


def _controlled(function):
    @wraps(function)
    def call(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except LinkError:
            raise
        except (SourceError, OSError, ValueError, TypeError, KeyError, AttributeError,
                RecursionError, NotImplementedError) as exc:
            raise LinkError('execution content link cannot complete safely') from exc
    return call


def link_identity(document):
    unsigned = {key: value for key, value in document.items() if key != 'link_id'}
    return 'XLINK-' + sha256(PROFILE.encode('ascii') + b'\0' + canonical_bytes(unsigned)).hexdigest()[:32]


def _path(root, identifier):
    if not isinstance(identifier, str) or not XLINK_RE.fullmatch(identifier):
        raise LinkError('invalid XLINK ID')
    return intake_path(root, 'intake/execution-links/' + identifier + '.json')


def _parse(raw):
    doc = _strict_json(raw, schema=SCHEMA, label='execution content link')
    if raw != canonical_bytes(doc):
        raise LinkError('XLINK is not canonical JSON')
    numbers = (doc['schema_version'], doc['attempt']['schema_version'],
               doc['raw_response']['bytes'], doc['normalized_submission']['bytes'],
               doc['normalized_submission']['proposals'])
    if any(type(value) is not int for value in numbers):
        raise LinkError('XLINK versions/counts must be exact integers')
    validate_executor(doc['declared_executor'])
    if (doc['link_id'] != link_identity(doc)
            or len({item['id'] for item in doc['proposals']}) != len(doc['proposals'])):
        raise LinkError('XLINK identity/proposal uniqueness mismatch')
    return doc


def _inventory(root):
    directory = intake_path(root, 'intake/execution-links')
    try:
        info = os.lstat(directory)
    except FileNotFoundError:
        return {}
    if not stat.S_ISDIR(info.st_mode):
        raise LinkError('unsafe execution-links namespace')
    documents, owners = {}, {}
    for child in sorted(directory.iterdir()):
        safe = intake_path(root, 'intake/execution-links/' + child.name)
        info = os.lstat(safe)
        if child.name == '.gitkeep' and stat.S_ISREG(info.st_mode) and info.st_size == 0:
            continue
        if not stat.S_ISREG(info.st_mode) or not XLINK_RE.fullmatch(child.stem) or child.suffix != '.json':
            raise LinkError('unknown/unsafe execution-links artifact')
        raw, _, _ = _read_exact(safe, MAX_LINK_BYTES, 'XLINK')
        doc = _parse(raw)
        if child.name != doc['link_id'] + '.json':
            raise LinkError('XLINK filename/ID mismatch')
        owner = doc['attempt']['id']
        if owner in owners:
            raise LinkError('duplicate XLINK ownership for ATTEMPT')
        owners[owner] = doc['link_id']
        documents[doc['link_id']] = doc
    return documents


def _commit_file(root, relative, limit, schema, expected):
    raw, digest, _ = _read_exact(intake_path(root, relative), limit, 'linked receipt')
    parsed = _strict_json(raw, schema=schema, label='linked receipt')
    if parsed != expected:
        raise LinkError('linked receipt changed during independent inspection')
    return digest


def _proof(root, attempt_id, run_id, *, require_bytes):
    """Reconstruct from independently valid receipts, then actual bytes if present."""
    state = execution.inspect_attempt(root, attempt_id)
    if (state.schema_version != 4 or state.state != 'RESPONSE_RECORDED'
            or state.contract['retention'] != 'sealed_local' or state.sealed_payload is None):
        raise LinkError('XLINK requires a successfully sealed v4 fixture response')
    if state.payload_status not in {'AVAILABLE', 'MISSING'}:
        raise LinkError('linked response is corrupt or unsafe')
    if require_bytes and state.payload_status != 'AVAILABLE':
        raise LinkError('link creation requires locally available verified response bytes')
    _, project_id, _, extraction = _validated_layers(root)
    run = extraction.runs.get(run_id)
    contract = extraction.contracts.get(state.contract['contract_id'])
    if run is None or contract is None:
        raise LinkError('link requires valid existing XRUN and XCON')
    if (run['contract_id'] != contract['contract_id']
            or run['project_id'] != project_id
            or run['executor']['instruction_sha256'] != state.contract['instruction_sha256']):
        raise LinkError('XRUN project/XCON/instruction commitment mismatch')
    contract_sha = _commit_file(
        root, 'intake/extraction-contracts/' + contract['contract_id'] + '.json',
        4 * 1024 * 1024, 'extraction-contract.schema.json', contract)
    if contract_sha != state.contract['contract_sha256']:
        raise LinkError('XCON exact receipt commitment mismatch')
    run_sha = _commit_file(
        root, 'intake/extraction-runs/' + run_id + '.json',
        4 * 1024 * 1024, 'extraction-run.schema.json', run)
    props = []
    for ident in run['proposal_ids']:
        proposal = extraction.proposals.get(ident)
        if proposal is None:
            raise LinkError('missing valid linked PROP')
        digest = _commit_file(root, 'intake/proposals/' + ident + '.json',
                              4 * 1024 * 1024, 'proposal.schema.json', proposal)
        props.append({'id': ident, 'sha256': digest})

    checkpoint_docs, checkpoint_hashes = {}, {}
    for slot in execution.SLOTS_V2:
        raw, digest, _ = _read_exact(execution._path(root, attempt_id, slot),
                                     execution.MAX_CHECKPOINT_BYTES, 'linked checkpoint')
        checkpoint_docs[slot] = execution._parse_checkpoint(raw)
        checkpoint_hashes[slot] = digest
    prepared = checkpoint_docs['01-prepared.json']
    intent = checkpoint_docs['02-boundary.json']
    terminal = checkpoint_docs['03-outcome.json']
    if (prepared['data']['contract'] != state.contract
            or terminal['checkpoint_sha256'] != state.last_sha256
            or intent['checkpoint_sha256'] != terminal['previous_sha256']):
        raise LinkError('execution chain changed during link inspection')
    # Repeat full transition/authorization/protocol checks against current history.
    execution._verify_xinv(terminal, prepared, intent, root=root)
    if execution.inspect_attempt(root, attempt_id) != state:
        raise LinkError('execution history/availability changed during link inspection')
    xinv = terminal['data']['xinv']
    normalized_meta = {key: run['submission'][key] for key in
                       ('sha256', 'bytes', 'proposals', 'proposal_manifest_sha256')}
    reproducibility = 'UNAVAILABLE'
    if state.payload_status == 'AVAILABLE':
        raw = execution.read_response(root, attempt_id)  # B2 verified reader, not hash assertions.
        normalized, records = normalize_submission(raw, contract)
        actual = {
            'sha256': sha256(normalized).hexdigest(), 'bytes': len(normalized),
            'proposals': len(records),
            'proposal_manifest_sha256': proposal_manifest_sha256(normalized_proposal_manifest(records)),
        }
        if actual != normalized_meta:
            raise LinkError('actual normalized response/XRUN commitment mismatch')
        expected_ids = []
        for record in records:
            proposal = {
                'schema_version': 1, 'profile': PROPOSAL_PROFILE, 'project_id': project_id,
                'proposal_id': '', 'run_id': run_id, 'payload_sha256': record['payload_sha256'],
                'payload_bytes': len(record['payload_bytes']),
                'evidence_segment_ids': record['payload']['evidence_segment_ids'],
                'canonical_authority': False, 'human_review_required': True,
            }
            proposal['proposal_id'] = proposal_id(proposal)
            expected_ids.append(proposal['proposal_id'])
            if extraction.proposals.get(proposal['proposal_id']) != proposal:
                raise LinkError('actual normalized response/PROP mismatch')
        if expected_ids != run['proposal_ids']:
            raise LinkError('actual proposal identities/order mismatch')
        reproducibility = 'AVAILABLE'
    document = {
        'schema_version': 1, 'profile': PROFILE, 'project_id': project_id,
        'attempt': {
            'id': attempt_id, 'schema_version': state.schema_version,
            'prepared_sha256': checkpoint_hashes['01-prepared.json'],
            'contract_sha256': execution._digest(state.contract),
            'dispatch_intent_sha256': checkpoint_hashes['02-boundary.json'],
            'authorization_sha256': intent['data']['authorization']['authorization_sha256'],
        },
        'invocation': {'id': xinv['invocation_id'],
                       'terminal_sha256': checkpoint_hashes['03-outcome.json']},
        'contract': {'id': contract['contract_id'], 'sha256': contract_sha},
        'run': {'id': run_id, 'sha256': run_sha},
        'raw_response': dict(xinv['response']),
        'normalized_submission': normalized_meta, 'proposals': props,
        'declared_executor': dict(run['executor']),
        **{field: False for field in FALSE_CLAIMS},
    }
    document['link_id'] = link_identity(document)
    _parse(canonical_bytes(document))
    return document, reproducibility


@dataclass(frozen=True)
class LinkInspection:
    link_id: str
    attempt_id: str
    reproducibility: str
    semantic_evidence: bool = False


@_controlled
def inspect_execution_link(root, link_id):
    _path(root, link_id)
    # Public entry always creates a fresh independently verified scope, even if nested.
    with _validation_batch(root):
        inventory = _inventory(root)  # Duplicate/orphan/unknown namespace fails closed.
        doc = inventory.get(link_id)
        if doc is None:
            raise LinkError('missing valid XLINK')
        return _inspect_document(root, doc)


@_controlled
def _inspect_document(root, doc):
    link_id = doc['link_id']
    raw, _, _ = _read_exact(_path(root, link_id), MAX_LINK_BYTES, 'XLINK')
    if _parse(raw) != doc:
        raise LinkError('XLINK changed during batch inspection')
    expected, status = _proof(root, doc['attempt']['id'], doc['run']['id'], require_bytes=False)
    if doc != expected:
        raise LinkError('XLINK complete receipt binding mismatch')
    return LinkInspection(link_id, doc['attempt']['id'], status)


def _boundary(name):
    """Test crash seam only, not a response-sealing or authorization interface."""


@_controlled
def create_execution_link(root, attempt_id, run_id):
    # Local import avoids validation -> sync planning -> validation import cycle.
    from .sync_intake import _intake_lock, SyncIntakeError
    root = checked_path(Path(root).absolute())
    checked_path(root / '.generated/sync/.intake.lock')
    try:
        with _intake_lock(root):
            checked_path(root / '.generated/sync/.intake.lock')
            with _validation_batch(root):
                inventory = _inventory(root)
                for doc in inventory.values():
                    _inspect_document(root, doc)
                    if doc['attempt']['id'] == attempt_id:
                        raise LinkError('ATTEMPT already owns an immutable XLINK; no duplicate publication')
                document, _ = _proof(root, attempt_id, run_id, require_bytes=True)
            # The input guard must finish successfully before any durable publication.
            raw = canonical_bytes(document)
            if len(raw) > MAX_LINK_BYTES:
                raise LinkError('XLINK exceeds byte ceiling')
            destination = _path(root, document['link_id'])
            destination.parent.mkdir(parents=True, exist_ok=True)
            _path(root, document['link_id'])
            staging = checked_path(root / '.generated/execution-link-staging')
            staging.mkdir(parents=True, exist_ok=True)
            staging = checked_path(staging)
            descriptor, name = tempfile.mkstemp(prefix='.xlink-', suffix='.tmp', dir=staging)
            temporary = Path(name)
            try:
                with os.fdopen(descriptor, 'wb') as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                _path(root, document['link_id'])
                _boundary('before_link_publication')
                try:
                    os.link(temporary, destination)
                except (OSError, NotImplementedError) as exc:
                    raise LinkError('atomic XLINK no-clobber publication failed; no overwrite') from exc
                _boundary('after_link_publication')
                return inspect_execution_link(root, document['link_id'])
            finally:
                temporary.unlink(missing_ok=True)
    except SyncIntakeError as exc:
        raise LinkError('XLINK intake publication lock unavailable or unsafe') from exc


def inspect_execution_link_layer(root, config):
    """Optional namespace, read-only integrity and separate reproducibility diagnostics."""
    issues = []
    try:
        with _validation_batch(root):
            inventory = _inventory(root)
            for identifier, doc in inventory.items():
                relative = 'intake/execution-links/' + identifier + '.json'
                try:
                    result = _inspect_document(root, doc)
                    if result.reproducibility == 'UNAVAILABLE':
                        issues.append(('WARNING', relative,
                                       'XLINK structural chain valid; byte reproducibility unavailable'))
                except LinkError as exc:
                    issues.append(('ERROR', relative, str(exc)))
    except (SourceError, OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError) as exc:
        # A failed final guard cannot leave an earlier structural-validity claim.
        issues = [issue for issue in issues if issue[0] == 'ERROR']
        message = str(exc) if isinstance(exc, LinkError) else 'unsafe execution-links namespace'
        issues.append(('ERROR', 'intake/execution-links', message))
    return tuple(sorted(set(issues)))
