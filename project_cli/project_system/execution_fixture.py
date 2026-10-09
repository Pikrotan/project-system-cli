"""Pinned synthetic extraction fixture v1, never a model or sealing authority."""

from contextlib import contextmanager
from contextvars import ContextVar
from hashlib import sha256
import json
import os
from pathlib import Path
import stat

from .source_extraction import _layers
from .extraction_layer import ExtractionError, normalize_submission
from .source_layer import checked_path, stream_source


_BATCH = ContextVar('execution_fixture_validation_batch', default=None)
_LINK_SECTION = 'intake/execution-links'


def _input_snapshot(root, sections):
    """Read-only exact-byte/path guard, not a cache or a filesystem transaction."""
    result = {}
    def visit(relative):
        path = checked_path(root / relative)
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            result[relative] = None
            return
        identity = (info.st_dev, info.st_ino, info.st_mode)
        if stat.S_ISDIR(info.st_mode):
            result[relative] = ('directory', *identity)
            for child in sorted(path.iterdir()):
                visit(relative + '/' + child.name)
        elif stat.S_ISREG(info.st_mode):
            digest, size = stream_source(path)
            after = os.lstat(checked_path(path))
            before_fields = (*identity, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
            after_fields = (after.st_dev, after.st_ino, after.st_mode,
                            after.st_size, after.st_mtime_ns, after.st_ctime_ns)
            if before_fields != after_fields:
                raise ExtractionError('execution validation input changed during snapshot')
            result[relative] = ('file', *after_fields, digest, size)
        else:
            raise ExtractionError('unsafe execution validation input')
    for section in sections:
        visit(section)
    return result


@contextmanager
def _validation_batch(root, *, guard_links=True):
    """Private invocation-local reuse; callers cannot supply verified provenance."""
    root = checked_path(Path(root).absolute())
    batch = {'root': root,
             'links': _input_snapshot(root, (_LINK_SECTION,)) if guard_links else None,
             'inputs': None, 'layers': None, 'failed': False}
    token = _BATCH.set(batch)
    try:
        yield
        if batch['inputs'] is not None or batch['links'] is not None:
            sections = ('project.yaml', 'sources', 'intake') if batch['inputs'] is not None else (_LINK_SECTION,)
            expected = batch['inputs'] if batch['inputs'] is not None else batch['links']
            if _input_snapshot(root, sections) != expected:
                raise ExtractionError('execution validation inputs changed during batch; no stale result')
    finally:
        _BATCH.reset(token)


def _validated_layers(root):
    root = checked_path(Path(root).absolute())
    batch = _BATCH.get()
    if batch is None or batch['root'] != root:
        return _layers(root)
    if batch['failed']:
        raise ExtractionError('execution batch provenance validation failed')
    if batch['layers'] is None:
        try:
            inputs = _input_snapshot(root, ('project.yaml', 'sources', 'intake'))
            if batch['links'] is not None:
                links = {key: value for key, value in inputs.items()
                         if key == _LINK_SECTION or key.startswith(_LINK_SECTION + '/')}
                # Absent intake also means an absent optional link namespace.
                if not links:
                    links = {_LINK_SECTION: None}
                if links != batch['links']:
                    raise ExtractionError('execution link inventory changed before batch verification')
            batch['inputs'] = inputs
            batch['layers'] = _layers(root)  # Fresh independent validation, never supplied by a caller.
        except Exception:
            batch['failed'] = True  # Fail closed once; never keep retrying a failed shared scan.
            raise
    return batch['layers']


def response_bytes(root, contract_id):
    # Independently verify durable XCON/REP/SEG, not a caller-provided contract.
    _, _, _, extraction = _validated_layers(root)
    contract = extraction.contracts.get(contract_id)
    if contract is None:
        raise ExtractionError('fixture requires a valid existing XCON')
    document = {
        'schema_version': 1, 'profile': 'project-system-extraction-submission-v1',
        'proposals': [{
            'kind': contract['allowed_kinds'][0],
            'statement': 'SYNTHETIC TEST FIXTURE: not project truth, AI output or human approval.',
            'support': 'ambiguous',
            'evidence_segment_ids': [contract['presented_segment_ids'][0]],
        }],
    }
    raw = (json.dumps(document, indent=2, ensure_ascii=False) + '\n').encode('utf-8')
    if len(raw) > 65536:
        raise ExtractionError('synthetic fixture exceeds response ceiling')
    normalize_submission(raw, contract)
    return raw


def response_commitment(root, contract_id):
    raw = response_bytes(root, contract_id)
    return {'sha256': sha256(raw).hexdigest(), 'bytes': len(raw)}


def dispatch(root, contract):
    if contract['options']['scenario'] == 'malformed_response':
        return None
    return response_bytes(root, contract['contract_id'])
