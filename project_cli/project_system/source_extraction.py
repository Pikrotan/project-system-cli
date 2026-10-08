"""Stage 13B2a deterministic extraction contracts and proposal sealing."""

from hashlib import sha256
import os
from pathlib import Path, PurePosixPath
import tempfile

from yaml import YAMLError

from .extraction_layer import (
    EXTRACTION_CONTRACT_PROFILE,
    EXTRACTION_RUN_PROFILE,
    EXTRACTION_SEALER_PROFILE,
    MAX_RAW_SUBMISSION_BYTES,
    MAX_XCON_BYTES,
    MAX_XCON_SEGMENTS,
    PROPOSAL_KINDS,
    PROPOSAL_PROFILE,
    ExtractionError,
    extraction_contract_id,
    extraction_run_id,
    inspect_extraction_layer,
    normalized_proposal_manifest,
    normalize_submission,
    proposal_manifest_sha256,
    proposal_id,
    representation_descriptors,
    serialize_receipt,
    validate_contract,
    validate_contract_binding,
    validate_executor,
    validate_proposal,
    validate_run,
)
from .intake_layer import inspect_intake_layer, intake_path
from .representation_layer import inspect_representation_layer
from .source_layer import (
    SourceError,
    checked_path,
    inspect_source_layer,
    project_identity,
    stream_source,
)
from .utils import load_yaml


def _exists(path):
    try:
        os.lstat(path)
        return True
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise ExtractionError('extraction artifact cannot be inspected') from exc


def _read_exact(path, limit, label):
    from io import BytesIO
    output = BytesIO()
    try:
        _, size = stream_source(path, limit=limit, sink=output)
    except SourceError as exc:
        raise ExtractionError(f'{label} cannot be read safely') from exc
    return output.getvalue(), size


def _publish_one(root, relative, raw, created):
    destination = intake_path(root, relative)
    destination.parent.mkdir(parents=True, exist_ok=True)
    intake_path(root, relative)
    descriptor, name = tempfile.mkstemp(
        prefix='.extraction-', suffix='.tmp', dir=destination.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, destination)
        except FileExistsError:
            existing, _ = _read_exact(
                destination, max(len(raw), 1), 'existing immutable extraction artifact')
            if existing != raw:
                raise ExtractionError('existing immutable extraction artifact conflicts')
            return False
        info = os.lstat(destination)
        created.append((relative, info.st_dev, info.st_ino))
        return True
    except OSError as exc:
        raise ExtractionError('extraction artifact cannot be published atomically') from exc
    finally:
        temporary.unlink(missing_ok=True)


def _publish_many(root, artifacts):
    """Preflight all deterministic paths, then no-clobber publish with owned rollback."""
    for relative, raw in artifacts:
        destination = intake_path(root, relative)
        if _exists(destination):
            existing, _ = _read_exact(
                destination, max(len(raw), 1), 'existing immutable extraction artifact')
            if existing != raw:
                raise ExtractionError('existing immutable extraction artifact conflicts')
    created = []
    try:
        changed = False
        for relative, raw in artifacts:
            changed = _publish_one(root, relative, raw, created) or changed
        return changed
    except Exception:
        for relative, device, inode in reversed(created):
            try:
                path = intake_path(root, relative)
                info = os.lstat(path)
                if (info.st_dev, info.st_ino) == (device, inode):
                    path.unlink()
            except (OSError, SourceError):
                pass
        raise


def _generated_path(root, relative):
    pure = PurePosixPath(relative)
    prefix = ('.generated', 'source-extractions')
    if (pure.is_absolute() or pure.as_posix() != relative
            or tuple(pure.parts[:2]) != prefix
            or any(part in {'.', '..'} for part in pure.parts)
            or any(char in relative for char in '\\:\x00')):
        raise ExtractionError('unsafe generated extraction path')
    try:
        return checked_path(Path(root).absolute().joinpath(*pure.parts))
    except SourceError as exc:
        raise ExtractionError(str(exc)) from exc


def _materialize_cache(root, run_id, normalized):
    relative = f'.generated/source-extractions/{run_id}/submission.json'
    destination = _generated_path(root, relative)
    destination.parent.mkdir(parents=True, exist_ok=True)
    _generated_path(root, relative)
    if _exists(destination):
        try:
            current, _ = _read_exact(
                destination, max(len(normalized), 1), 'generated extraction cache')
            if current == normalized:
                return False
        except ExtractionError:
            raise
    descriptor, name = tempfile.mkstemp(
        prefix='.submission-', suffix='.tmp', dir=destination.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(normalized)
            stream.flush()
            os.fsync(stream.fileno())
        _generated_path(root, relative)
        os.replace(temporary, destination)
        return True
    except OSError as exc:
        raise ExtractionError('generated extraction cache cannot be materialized') from exc
    finally:
        temporary.unlink(missing_ok=True)


def _layers(root):
    config = load_yaml(checked_path(Path(root) / 'project.yaml'))
    project_id = project_identity(config)
    intake = inspect_intake_layer(root)
    if intake.issues:
        raise ExtractionError('existing intake namespace is invalid; run project validate')
    source = inspect_source_layer(root, config)
    if source.issues:
        raise ExtractionError('existing source layer is invalid; run project validate')
    representations = inspect_representation_layer(root, config, source)
    if representations.issues:
        raise ExtractionError('existing representation layer is invalid; run project validate')
    extraction = inspect_extraction_layer(root, config, representations)
    if extraction.issues:
        raise ExtractionError('existing extraction layer is invalid; run project validate')
    return config, project_id, representations, extraction


def create_extraction_contract(root, representation_id, *, segment_ids=None,
                               allowed_kinds=None):
    root = Path(root).absolute()
    try:
        _, project_id, representations, _ = _layers(root)
        representation = representations.representations.get(representation_id)
        if representation is None:
            raise ExtractionError('REP does not exist or is invalid')
        descriptors = representation_descriptors(root, representation)
        ordered = [item['segment_id'] for item in descriptors]
        if not ordered:
            raise ExtractionError('zero-segment REP cannot create an extraction contract')
        requested = list(segment_ids) if segment_ids is not None else ordered
        if len(requested) != len(set(requested)):
            raise ExtractionError('duplicate --segment is not allowed')
        if len(requested) > MAX_XCON_SEGMENTS:
            raise ExtractionError('extraction contract exceeds segment-count limit')
        known = set(ordered)
        if any(segment not in known for segment in requested):
            raise ExtractionError('requested SEG does not belong to REP')
        requested_set = set(requested)
        presented = [segment for segment in ordered if segment in requested_set]

        kinds = list(allowed_kinds) if allowed_kinds is not None else list(PROPOSAL_KINDS)
        if not kinds:
            raise ExtractionError('extraction contract requires an allowed kind')
        if len(kinds) != len(set(kinds)):
            raise ExtractionError('duplicate --allow-kind is not allowed')
        if any(kind not in PROPOSAL_KINDS for kind in kinds):
            raise ExtractionError('unsupported proposal kind')
        kind_set = set(kinds)
        kinds = [kind for kind in PROPOSAL_KINDS if kind in kind_set]

        contract = {
            'schema_version': 1,
            'profile': EXTRACTION_CONTRACT_PROFILE,
            'project_id': project_id,
            'contract_id': '',
            'representation_id': representation_id,
            'presented_segment_ids': presented,
            'allowed_kinds': kinds,
            'coverage': {
                'presented_segments': len(presented),
                'representation_segments': len(ordered),
                'complete': presented == ordered,
            },
            'canonical_authority': False,
        }
        contract['contract_id'] = extraction_contract_id(contract)
        validate_contract(contract)
        validate_contract_binding(contract, descriptors)
        relative = f'intake/extraction-contracts/{contract["contract_id"]}.json'
        created = _publish_many(
            root, [(relative, serialize_receipt(contract, limit=MAX_XCON_BYTES))])
        return {
            'contract_id': contract['contract_id'],
            'representation_id': representation_id,
            'presented_segments': len(presented),
            'representation_segments': len(ordered),
            'coverage_complete': presented == ordered,
            'status': 'created' if created else 'existing',
        }
    except (ExtractionError, SourceError, OSError, ValueError, TypeError, YAMLError) as exc:
        if isinstance(exc, ExtractionError):
            raise
        if isinstance(exc, SourceError):
            raise ExtractionError(str(exc)) from exc
        raise ExtractionError('extraction contract could not complete safely') from None


def seal_extraction(root, contract_id, submission_path, *, executor_kind,
                    provider, model, instruction_sha256, retention='reference'):
    root = Path(root).absolute()
    try:
        if retention not in {'reference', 'repository_snapshot'}:
            raise ExtractionError('unsupported extraction submission retention')
        _, project_id, representations, extraction = _layers(root)
        contract = extraction.contracts.get(contract_id)
        if contract is None:
            raise ExtractionError('XCON does not exist or is invalid')
        if contract['representation_id'] not in representations.representations:
            raise ExtractionError('XCON representation does not exist or is invalid')
        executor = {
            'kind': executor_kind,
            'provider': provider,
            'model': model,
            'instruction_sha256': instruction_sha256,
        }
        validate_executor(executor)
        raw, _ = _read_exact(
            checked_path(submission_path), MAX_RAW_SUBMISSION_BYTES,
            'untrusted extraction submission')
        normalized, records = normalize_submission(raw, contract)
        submission_hash = sha256(normalized).hexdigest()
        manifest_hash = proposal_manifest_sha256(
            normalized_proposal_manifest(records))
        run = {
            'schema_version': 1,
            'profile': EXTRACTION_RUN_PROFILE,
            'project_id': project_id,
            'run_id': '',
            'contract_id': contract_id,
            'representation_id': contract['representation_id'],
            'sealer_profile': EXTRACTION_SEALER_PROFILE,
            'executor': executor,
            'submission': {
                'sha256': submission_hash,
                'bytes': len(normalized),
                'proposals': len(records),
                'proposal_manifest_sha256': manifest_hash,
                'retention': retention,
                'snapshot': None,
            },
            'proposal_ids': [],
            'canonical_authority': False,
            'human_review_required': True,
        }
        run['run_id'] = extraction_run_id(run)
        if retention == 'repository_snapshot':
            run['submission']['snapshot'] = {
                'path': f'intake/extraction-submissions/{run["run_id"]}/submission.json',
                'sha256': submission_hash,
                'bytes': len(normalized),
            }
        proposals = []
        for record in records:
            proposal = {
                'schema_version': 1,
                'profile': PROPOSAL_PROFILE,
                'project_id': project_id,
                'proposal_id': '',
                'run_id': run['run_id'],
                'payload_sha256': record['payload_sha256'],
                'payload_bytes': len(record['payload_bytes']),
                'evidence_segment_ids': record['payload']['evidence_segment_ids'],
                'canonical_authority': False,
                'human_review_required': True,
            }
            proposal['proposal_id'] = proposal_id(proposal)
            validate_proposal(proposal)
            proposals.append(proposal)
        run['proposal_ids'] = [proposal['proposal_id'] for proposal in proposals]
        validate_run(run)

        artifacts = []
        if retention == 'repository_snapshot':
            artifacts.append((run['submission']['snapshot']['path'], normalized))
        artifacts.extend(
            (f'intake/proposals/{proposal["proposal_id"]}.json', serialize_receipt(proposal))
            for proposal in proposals)
        artifacts.append((
            f'intake/extraction-runs/{run["run_id"]}.json', serialize_receipt(run)))
        created = _publish_many(root, artifacts)
        cache_changed = _materialize_cache(root, run['run_id'], normalized)
        status = 'created' if created else ('rebuilt_cache' if cache_changed else 'existing')
        return {
            'run_id': run['run_id'],
            'contract_id': contract_id,
            'representation_id': contract['representation_id'],
            'proposals': len(proposals),
            'submission_retention': retention,
            'status': status,
        }
    except (ExtractionError, SourceError, OSError, ValueError, TypeError, YAMLError) as exc:
        if isinstance(exc, ExtractionError):
            raise
        if isinstance(exc, SourceError):
            raise ExtractionError(str(exc)) from exc
        raise ExtractionError('extraction sealing could not complete safely') from None
