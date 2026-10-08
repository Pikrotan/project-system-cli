"""Stage 13B2a extraction contracts and immutable semantic evidence receipts."""

from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path
import re
import stat
import unicodedata

from jsonschema import Draft202012Validator

from .intake_layer import IntakeError, intake_path
from .representation_layer import (
    MAX_SEGMENT_INDEX_BYTES,
    REP_RE,
    SEG_RE,
    parse_segment_index,
)
from .source_layer import (
    PROJECT_RE,
    SHA_RE,
    SourceError,
    canonical_bytes,
    stream_source,
)
from .utils import distribution_root


EXTRACTION_CONTRACT_PROFILE = 'project-system-extraction-contract-v1'
EXTRACTION_SUBMISSION_PROFILE = 'project-system-extraction-submission-v1'
EXTRACTION_SEALER_PROFILE = 'project-system-extraction-sealer-v1'
EXTRACTION_RUN_PROFILE = 'project-system-extraction-run-v1'
PROPOSAL_PROFILE = 'project-system-proposal-v1'
PROPOSAL_MANIFEST_PROFILE = 'project-system-proposal-manifest-v1'

PROPOSAL_KINDS = ('requirement', 'decision', 'risk', 'question')
SUPPORT_VALUES = ('explicit', 'inferred', 'ambiguous')

MAX_XCON_SEGMENTS = 10_000
MAX_XCON_BYTES = 4 * 1024 * 1024
MAX_RAW_SUBMISSION_BYTES = 4 * 1024 * 1024
MAX_NORMALIZED_SUBMISSION_BYTES = 2 * 1024 * 1024
MAX_PROPOSALS_PER_SUBMISSION = 256
MAX_PROPOSAL_STATEMENT_BYTES = 8 * 1024
MAX_EVIDENCE_SEGMENTS_PER_PROPOSAL = 64
MAX_EXTRACTION_RECEIPT_BYTES = 4 * 1024 * 1024

XCON_RE = re.compile(r'XCON-[0-9a-f]{32}')
XRUN_RE = re.compile(r'XRUN-[0-9a-f]{32}')
PROP_RE = re.compile(r'PROP-[0-9a-f]{32}')
PROVIDER_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}')


class ExtractionError(SourceError):
    """Extraction provenance cannot be produced or trusted."""


def _schema(document, name, label):
    try:
        schema = json.loads((distribution_root() / 'schemas' / name).read_text('utf-8'))
        error = next(Draft202012Validator(schema).iter_errors(document), None)
    except (OSError, ValueError, TypeError, RecursionError) as exc:
        raise ExtractionError(f'invalid {label} structure') from exc
    if error:
        location = '.'.join(map(str, error.absolute_path)) or '<root>'
        raise ExtractionError(f'invalid {label} at {location}')


def _strict_json(raw, *, schema, label):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ExtractionError(f'duplicate {label} JSON key')
            result[key] = value
        return result

    def nonfinite(_):
        raise ExtractionError(f'non-finite {label} JSON value')

    try:
        document = json.loads(raw.decode('utf-8'), object_pairs_hook=unique,
                              parse_constant=nonfinite)
    except ExtractionError:
        raise
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ExtractionError(f'{label} is not strict UTF-8 JSON') from exc
    _schema(document, schema, label)
    return document


def _read_bytes(path, limit, label):
    buffer = BytesIO()
    try:
        digest, size = stream_source(path, limit=limit, sink=buffer)
    except SourceError as exc:
        raise ExtractionError(f'{label} cannot be read safely') from exc
    return buffer.getvalue(), digest, size


def serialize_receipt(document, *, limit=MAX_EXTRACTION_RECEIPT_BYTES):
    try:
        raw = (json.dumps(document, sort_keys=True, indent=2, ensure_ascii=False,
                          allow_nan=False) + '\n').encode('utf-8')
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ExtractionError('extraction receipt cannot be serialized safely') from exc
    if len(raw) > limit:
        raise ExtractionError('extraction receipt exceeds size limit')
    return raw


def extraction_contract_id(document):
    payload = {
        'profile': EXTRACTION_CONTRACT_PROFILE,
        'project_id': document['project_id'],
        'representation_id': document['representation_id'],
        'presented_segment_ids': document['presented_segment_ids'],
        'allowed_kinds': document['allowed_kinds'],
    }
    return 'XCON-' + sha256(canonical_bytes(payload)).hexdigest()[:32]


def extraction_run_id(document):
    payload = {
        'project_id': document['project_id'],
        'contract_id': document['contract_id'],
        'representation_id': document['representation_id'],
        'sealer_profile': document['sealer_profile'],
        'executor': document['executor'],
        'submission_sha256': document['submission']['sha256'],
        'submission_bytes': document['submission']['bytes'],
        'proposal_count': document['submission']['proposals'],
        'proposal_manifest_sha256': document['submission']['proposal_manifest_sha256'],
        'retention': document['submission']['retention'],
    }
    return 'XRUN-' + sha256(canonical_bytes(payload)).hexdigest()[:32]


def proposal_id(document):
    payload = {
        'project_id': document['project_id'],
        'run_id': document['run_id'],
        'payload_sha256': document['payload_sha256'],
        'payload_bytes': document['payload_bytes'],
        'evidence_segment_ids': document['evidence_segment_ids'],
    }
    return 'PROP-' + sha256(canonical_bytes(payload)).hexdigest()[:32]


def proposal_manifest(entries):
    return {
        'profile': PROPOSAL_MANIFEST_PROFILE,
        'proposals': [{
            'payload_sha256': item['payload_sha256'],
            'payload_bytes': item['payload_bytes'],
            'evidence_segment_ids': item['evidence_segment_ids'],
        } for item in entries],
    }


def proposal_manifest_sha256(manifest):
    return sha256(canonical_bytes(manifest)).hexdigest()


def normalized_proposal_manifest(records):
    return proposal_manifest([{
        'payload_sha256': record['payload_sha256'],
        'payload_bytes': len(record['payload_bytes']),
        'evidence_segment_ids': record['payload']['evidence_segment_ids'],
    } for record in records])


def receipt_proposal_manifest(proposals):
    return proposal_manifest([{
        'payload_sha256': item['payload_sha256'],
        'payload_bytes': item['payload_bytes'],
        'evidence_segment_ids': item['evidence_segment_ids'],
    } for item in proposals])


def validate_executor(executor):
    if not isinstance(executor, dict) or set(executor) != {
            'kind', 'provider', 'model', 'instruction_sha256'}:
        raise ExtractionError('invalid declared executor fields')
    provider, model = executor.get('provider'), executor.get('model')
    if (executor.get('kind') != 'ai' or not isinstance(provider, str)
            or not PROVIDER_RE.fullmatch(provider) or not isinstance(model, str)
            or not model or len(model.encode('utf-8')) > 128
            or any(unicodedata.category(char).startswith('C') for char in model)
            or not isinstance(executor.get('instruction_sha256'), str)
            or not SHA_RE.fullmatch(executor['instruction_sha256'])):
        raise ExtractionError('invalid declared executor identity')
    return executor


def validate_contract(document):
    _schema(document, 'extraction-contract.schema.json', 'extraction contract')
    if (type(document['schema_version']) is not int
            or not PROJECT_RE.fullmatch(document['project_id'])
            or not XCON_RE.fullmatch(document['contract_id'])
            or not REP_RE.fullmatch(document['representation_id'])
            or len(document['presented_segment_ids']) > MAX_XCON_SEGMENTS
            or document['allowed_kinds'] != [
                kind for kind in PROPOSAL_KINDS if kind in document['allowed_kinds']]
            or len(set(document['presented_segment_ids'])) != len(document['presented_segment_ids'])
            or len(set(document['allowed_kinds'])) != len(document['allowed_kinds'])):
        raise ExtractionError('invalid extraction contract identity/order/types')
    if document['coverage']['presented_segments'] != len(document['presented_segment_ids']):
        raise ExtractionError('extraction contract presented coverage mismatch')
    if document['contract_id'] != extraction_contract_id(document):
        raise ExtractionError('contract_id does not match deterministic identity')
    serialize_receipt(document, limit=MAX_XCON_BYTES)
    return document


def validate_run(document):
    _schema(document, 'extraction-run.schema.json', 'extraction run')
    if (type(document['schema_version']) is not int
            or not PROJECT_RE.fullmatch(document['project_id'])
            or not XRUN_RE.fullmatch(document['run_id'])
            or not XCON_RE.fullmatch(document['contract_id'])
            or not REP_RE.fullmatch(document['representation_id'])
            or any(type(document['submission'][key]) is not int
                   for key in ('bytes', 'proposals'))):
        raise ExtractionError('invalid extraction run identity/types')
    validate_executor(document['executor'])
    submission = document['submission']
    if submission['retention'] == 'reference':
        if submission['snapshot'] is not None:
            raise ExtractionError('reference extraction run must not bind a snapshot')
    else:
        expected = f'intake/extraction-submissions/{document["run_id"]}/submission.json'
        snapshot = submission['snapshot']
        if (snapshot is None or snapshot['path'] != expected
                or snapshot['sha256'] != submission['sha256']
                or snapshot['bytes'] != submission['bytes']):
            raise ExtractionError('repository snapshot metadata contradicts extraction run')
    if document['run_id'] != extraction_run_id(document):
        raise ExtractionError('run_id does not match deterministic identity')
    serialize_receipt(document)
    return document


def validate_proposal(document):
    _schema(document, 'proposal.schema.json', 'proposal receipt')
    if (type(document['schema_version']) is not int
            or not PROJECT_RE.fullmatch(document['project_id'])
            or not PROP_RE.fullmatch(document['proposal_id'])
            or not XRUN_RE.fullmatch(document['run_id'])
            or type(document['payload_bytes']) is not int
            or len(set(document['evidence_segment_ids'])) != len(document['evidence_segment_ids'])):
        raise ExtractionError('invalid proposal receipt identity/types')
    if document['proposal_id'] != proposal_id(document):
        raise ExtractionError('proposal_id does not match deterministic identity')
    serialize_receipt(document)
    return document


def load_contract(path):
    raw, _, _ = _read_bytes(path, MAX_XCON_BYTES, 'extraction contract')
    return validate_contract(_strict_json(
        raw, schema='extraction-contract.schema.json', label='extraction contract'))


def load_run(path):
    raw, _, _ = _read_bytes(path, MAX_EXTRACTION_RECEIPT_BYTES, 'extraction run')
    return validate_run(_strict_json(
        raw, schema='extraction-run.schema.json', label='extraction run'))


def load_proposal(path):
    raw, _, _ = _read_bytes(path, MAX_EXTRACTION_RECEIPT_BYTES, 'proposal receipt')
    return validate_proposal(_strict_json(
        raw, schema='proposal.schema.json', label='proposal receipt'))


def representation_descriptors(root, representation):
    path = intake_path(root, representation['segment_index']['path'])
    raw, digest, size = _read_bytes(path, MAX_SEGMENT_INDEX_BYTES, 'segment index')
    if (digest != representation['segment_index']['sha256']
            or size != representation['segment_index']['bytes']):
        raise ExtractionError('representation segment index binding mismatch')
    descriptors = parse_segment_index(
        raw, project_id=representation['project_id'],
        capture_id=representation['capture_id'],
        fingerprint=representation['adapter']['fingerprint'])
    if len(descriptors) != representation['segment_index']['segments']:
        raise ExtractionError('representation segment count mismatch')
    return descriptors


def validate_contract_binding(document, descriptors):
    ordered = [item['segment_id'] for item in descriptors]
    presented = document['presented_segment_ids']
    known = set(ordered)
    if any(segment not in known for segment in presented):
        raise ExtractionError('extraction contract references unknown representation SEG')
    if presented != [segment for segment in ordered if segment in set(presented)]:
        raise ExtractionError('extraction contract SEG order is not canonical REP order')
    expected = {
        'presented_segments': len(presented),
        'representation_segments': len(ordered),
        'complete': presented == ordered,
    }
    if document['coverage'] != expected:
        raise ExtractionError('extraction contract coverage mismatch')
    return document


def normalize_submission(raw, contract):
    if len(raw) > MAX_RAW_SUBMISSION_BYTES:
        raise ExtractionError('raw extraction submission exceeds size limit')
    document = _strict_json(
        raw, schema='extraction-submission.schema.json', label='extraction submission')
    if type(document['schema_version']) is not int:
        raise ExtractionError('invalid extraction submission schema_version type')
    if not document['proposals'] or len(document['proposals']) > MAX_PROPOSALS_PER_SUBMISSION:
        raise ExtractionError('invalid extraction submission proposal count')
    positions = {segment: index for index, segment in enumerate(
        contract['presented_segment_ids'])}
    allowed = set(contract['allowed_kinds'])
    normalized = []
    for proposal in document['proposals']:
        kind, statement = proposal['kind'], proposal['statement']
        if kind not in allowed:
            raise ExtractionError('proposal kind is not allowed by extraction contract')
        if not statement or statement.isspace():
            raise ExtractionError('proposal statement is semantically empty')
        if len(statement.encode('utf-8')) > MAX_PROPOSAL_STATEMENT_BYTES:
            raise ExtractionError('proposal statement exceeds size limit')
        evidence = proposal['evidence_segment_ids']
        if (not evidence or len(evidence) > MAX_EVIDENCE_SEGMENTS_PER_PROPOSAL
                or len(set(evidence)) != len(evidence)):
            raise ExtractionError('invalid proposal evidence list')
        if any(segment not in positions for segment in evidence):
            raise ExtractionError('proposal cites SEG not presented by extraction contract')
        payload = {
            'kind': kind,
            'statement': statement,
            'support': proposal['support'],
            'evidence_segment_ids': sorted(evidence, key=positions.__getitem__),
        }
        payload_bytes = canonical_bytes(payload)
        normalized.append({
            'payload': payload,
            'payload_bytes': payload_bytes,
            'payload_sha256': sha256(payload_bytes).hexdigest(),
        })
    normalized.sort(key=lambda item: (item['payload_sha256'], item['payload_bytes']))
    for previous, current in zip(normalized, normalized[1:]):
        if previous['payload_bytes'] == current['payload_bytes']:
            raise ExtractionError('duplicate normalized Proposal')
    normalized_document = {
        'schema_version': 1,
        'profile': EXTRACTION_SUBMISSION_PROFILE,
        'proposals': [item['payload'] for item in normalized],
    }
    normalized_bytes = canonical_bytes(normalized_document)
    if len(normalized_bytes) > MAX_NORMALIZED_SUBMISSION_BYTES:
        raise ExtractionError('normalized extraction submission exceeds size limit')
    return normalized_bytes, normalized


def _children(root, relative, issues, *, allow_gitkeep=True):
    try:
        directory = intake_path(root, relative)
        if not directory.exists():
            return []
        info = os.lstat(directory)
        if not stat.S_ISDIR(info.st_mode) or not directory.is_dir():
            raise IntakeError('intake section is not a directory')
        result = []
        for child in sorted(directory.iterdir(), key=lambda item: item.name):
            child_relative = relative + '/' + child.name
            try:
                safe = intake_path(root, child_relative)
                child_info = os.lstat(safe)
                if (allow_gitkeep and safe.name == '.gitkeep' and stat.S_ISREG(child_info.st_mode)
                        and child_info.st_size == 0):
                    continue
                result.append((child_relative, safe, child_info))
            except (IntakeError, OSError, ValueError):
                issues.append(('ERROR', child_relative, 'unsafe extraction storage entry'))
        return result
    except (IntakeError, OSError, ValueError):
        issues.append(('ERROR', relative, 'extraction storage cannot be inspected safely'))
        return []


@dataclass(frozen=True)
class ExtractionLayer:
    contracts: dict
    runs: dict
    proposals: dict
    issues: tuple


def inspect_extraction_layer(root, config, representation_layer):
    root = Path(root).absolute()
    issues, contracts, runs, proposals = [], {}, {}, {}

    def error(relative, message):
        issues.append(('ERROR', relative, message))

    project = config.get('project') if isinstance(config, dict) else None
    project_id = project.get('id') if isinstance(project, dict) else None
    descriptor_cache = {}

    def descriptors_for(representation_id):
        if representation_id not in descriptor_cache:
            representation = representation_layer.representations.get(representation_id)
            if representation is None:
                raise ExtractionError('extraction artifact references a missing valid REP')
            descriptor_cache[representation_id] = representation_descriptors(root, representation)
        return descriptor_cache[representation_id]

    contract_entries = _children(root, 'intake/extraction-contracts', issues)
    for relative, path, info in contract_entries:
        if not stat.S_ISREG(info.st_mode) or path.suffix != '.json':
            error(relative, 'extraction contracts support only JSON receipt files')
            continue
        try:
            document = load_contract(path)
            if path.name != document['contract_id'] + '.json':
                raise ExtractionError('extraction contract filename/internal ID mismatch')
            if document['project_id'] != project_id:
                raise ExtractionError('extraction contract project_id mismatch')
            validate_contract_binding(document, descriptors_for(document['representation_id']))
            if document['contract_id'] in contracts:
                raise ExtractionError('duplicate extraction contract ID')
            contracts[document['contract_id']] = document
        except (ExtractionError, SourceError) as exc:
            error(relative, str(exc))

    run_entries = _children(root, 'intake/extraction-runs', issues)
    for relative, path, info in run_entries:
        if not stat.S_ISREG(info.st_mode) or path.suffix != '.json':
            error(relative, 'extraction runs support only JSON receipt files')
            continue
        try:
            document = load_run(path)
            if path.name != document['run_id'] + '.json':
                raise ExtractionError('extraction run filename/internal ID mismatch')
            if document['project_id'] != project_id:
                raise ExtractionError('extraction run project_id mismatch')
            contract = contracts.get(document['contract_id'])
            if contract is None:
                raise ExtractionError('extraction run references a missing valid XCON')
            if document['representation_id'] != contract['representation_id']:
                raise ExtractionError('extraction run representation/XCON mismatch')
            if document['run_id'] in runs:
                raise ExtractionError('duplicate extraction run ID')
            runs[document['run_id']] = document
        except (ExtractionError, SourceError) as exc:
            error(relative, str(exc))

    proposal_entries = _children(root, 'intake/proposals', issues)
    for relative, path, info in proposal_entries:
        if not stat.S_ISREG(info.st_mode) or path.suffix != '.json':
            error(relative, 'proposals support only JSON receipt files')
            continue
        try:
            document = load_proposal(path)
            if path.name != document['proposal_id'] + '.json':
                raise ExtractionError('proposal filename/internal ID mismatch')
            if document['project_id'] != project_id:
                raise ExtractionError('proposal project_id mismatch')
            run = runs.get(document['run_id'])
            if run is None:
                raise ExtractionError('proposal references a missing valid XRUN')
            contract = contracts[run['contract_id']]
            order = {segment: index for index, segment in enumerate(
                contract['presented_segment_ids'])}
            evidence = document['evidence_segment_ids']
            if any(segment not in order for segment in evidence):
                raise ExtractionError('proposal evidence is outside XCON presented SEG set')
            if evidence != sorted(evidence, key=order.__getitem__):
                raise ExtractionError('proposal evidence is not in canonical XCON order')
            if document['proposal_id'] in proposals:
                raise ExtractionError('duplicate proposal ID')
            proposals[document['proposal_id']] = document
        except (ExtractionError, SourceError) as exc:
            error(relative, str(exc))

    for run_id, run in sorted(runs.items()):
        relative = f'intake/extraction-runs/{run_id}.json'
        listed = run['proposal_ids']
        if len(listed) != run['submission']['proposals']:
            error(relative, 'extraction run proposal count/list mismatch')
            continue
        bound = []
        for proposal_identifier in listed:
            proposal = proposals.get(proposal_identifier)
            if proposal is None:
                error(relative, f'extraction run references missing valid PROP {proposal_identifier}')
            elif proposal['run_id'] != run_id:
                error(relative, 'extraction run lists PROP bound to another XRUN')
            else:
                bound.append(proposal)
        hashes = [proposal['payload_sha256'] for proposal in bound]
        if len(hashes) != len(set(hashes)) or hashes != sorted(hashes):
            error(relative, 'extraction run proposal order/hash uniqueness mismatch')
        owned = sorted(identifier for identifier, proposal in proposals.items()
                       if proposal['run_id'] == run_id)
        if sorted(listed) != owned:
            error(relative, 'PROP exists but is not listed exactly by its XRUN')
        if len(bound) == len(listed):
            manifest_hash = proposal_manifest_sha256(
                receipt_proposal_manifest(bound))
            if manifest_hash != run['submission']['proposal_manifest_sha256']:
                error(relative, 'extraction run Proposal manifest commitment mismatch')

    snapshot_entries = _children(root, 'intake/extraction-submissions', issues)
    snapshots = {}
    for relative, path, info in snapshot_entries:
        if not stat.S_ISDIR(info.st_mode) or not XRUN_RE.fullmatch(path.name):
            error(relative, 'unexpected extraction submission snapshot entry')
            continue
        nested = _children(root, relative, issues, allow_gitkeep=False)
        if len(nested) != 1:
            error(relative, 'extraction submission snapshot must contain only submission.json')
        for payload_relative, payload_path, payload_info in nested:
            if (payload_path.name != 'submission.json'
                    or not stat.S_ISREG(payload_info.st_mode)):
                error(payload_relative, 'unexpected extraction submission snapshot artifact')
            else:
                snapshots[path.name] = (payload_relative, payload_path)

    expected_snapshot_runs = {
        run_id for run_id, run in runs.items()
        if run['submission']['retention'] == 'repository_snapshot'
    }
    for run_id in sorted(set(snapshots) - expected_snapshot_runs):
        error(snapshots[run_id][0], 'orphan extraction submission snapshot')
    for run_id in sorted(expected_snapshot_runs):
        run = runs[run_id]
        relative = f'intake/extraction-runs/{run_id}.json'
        snapshot = snapshots.get(run_id)
        if snapshot is None:
            error(relative, 'repository_snapshot extraction run is missing submission snapshot')
            continue
        try:
            raw, digest, size = _read_bytes(
                snapshot[1], MAX_NORMALIZED_SUBMISSION_BYTES,
                'normalized extraction submission snapshot')
            if (digest != run['submission']['sha256']
                    or size != run['submission']['bytes']):
                raise ExtractionError('normalized submission snapshot hash/bytes mismatch')
            contract = contracts[run['contract_id']]
            normalized, records = normalize_submission(raw, contract)
            if normalized != raw:
                raise ExtractionError('submission snapshot is not exact canonical normalized JSON')
            if len(records) != run['submission']['proposals']:
                raise ExtractionError('submission snapshot proposal count mismatch')
            manifest_hash = proposal_manifest_sha256(
                normalized_proposal_manifest(records))
            if manifest_hash != run['submission']['proposal_manifest_sha256']:
                raise ExtractionError(
                    'submission snapshot Proposal manifest commitment mismatch')
            expected_ids = []
            for record in records:
                proposal = {
                    'schema_version': 1, 'profile': PROPOSAL_PROFILE,
                    'project_id': project_id, 'proposal_id': '', 'run_id': run_id,
                    'payload_sha256': record['payload_sha256'],
                    'payload_bytes': len(record['payload_bytes']),
                    'evidence_segment_ids': record['payload']['evidence_segment_ids'],
                    'canonical_authority': False, 'human_review_required': True,
                }
                proposal['proposal_id'] = proposal_id(proposal)
                expected_ids.append(proposal['proposal_id'])
                if proposals.get(proposal['proposal_id']) != proposal:
                    raise ExtractionError('snapshot semantic payload/PROP receipt mismatch')
            if expected_ids != run['proposal_ids']:
                raise ExtractionError('snapshot proposal ordering/XRUN mismatch')
        except (ExtractionError, SourceError) as exc:
            error(snapshot[0], str(exc))

    return ExtractionLayer(
        contracts, runs, proposals, tuple(sorted(set(issues))))
