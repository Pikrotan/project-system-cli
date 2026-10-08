"""Stage 13B2b1 deterministic Verified Extraction Packs."""

from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tempfile

from jsonschema import Draft202012Validator

from .extraction_layer import (
    ExtractionError,
    XCON_RE,
    representation_descriptors,
)
from .source_extraction import _layers
from .source_layer import SourceError, canonical_bytes, checked_path, stream_source
from .utils import distribution_root


VERIFIED_EXTRACTION_PACK_PROFILE = 'project-system-verified-extraction-pack-v1'
MAX_PACK_SEGMENTS = 512
MAX_SINGLE_RENDERED_SEGMENT_BYTES = 256 * 1024
MAX_PACK_CANONICAL_BYTES = 2 * 1024 * 1024
XPACK_RE = re.compile(r'XPACK-[0-9a-f]{32}')


class ExtractionPackError(ExtractionError):
    """A verified extraction pack cannot be produced or trusted."""


def _schema(document):
    try:
        schema = json.loads((distribution_root() / 'schemas' /
                             'verified-extraction-pack.schema.json').read_text('utf-8'))
        error = next(Draft202012Validator(schema).iter_errors(document), None)
    except (OSError, ValueError, TypeError, RecursionError) as exc:
        raise ExtractionPackError('invalid verified extraction pack structure') from exc
    if error:
        location = '.'.join(map(str, error.absolute_path)) or '<root>'
        raise ExtractionPackError(f'invalid verified extraction pack at {location}')


def _strict_pack_json(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ExtractionPackError('duplicate verified extraction pack JSON key')
            result[key] = value
        return result

    def nonfinite(_):
        raise ExtractionPackError('non-finite verified extraction pack JSON value')

    try:
        document = json.loads(raw.decode('utf-8'), object_pairs_hook=unique,
                              parse_constant=nonfinite)
    except ExtractionPackError:
        raise
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ExtractionPackError(
            'verified extraction pack is not strict UTF-8 JSON') from exc
    _schema(document)
    return document


def _controlled_generated_path(root, relative, prefix):
    pure = PurePosixPath(relative)
    if (pure.is_absolute() or pure.as_posix() != relative
            or tuple(pure.parts[:len(prefix)]) != tuple(prefix)
            or any(part in {'.', '..'} for part in pure.parts)
            or any(char in relative for char in '\\:\x00')):
        raise ExtractionPackError('unsafe generated extraction pack path')
    try:
        return checked_path(Path(root).absolute().joinpath(*pure.parts))
    except SourceError as exc:
        raise ExtractionPackError('unsafe generated extraction pack path') from exc


def _pack_path(root, pack_id):
    if not isinstance(pack_id, str) or not XPACK_RE.fullmatch(pack_id):
        raise ExtractionPackError('invalid XPACK ID')
    return _controlled_generated_path(
        root, f'.generated/source-extraction-packs/{pack_id}/pack.json',
        ('.generated', 'source-extraction-packs'))


def _segment_cache_path(root, representation_id, segment_id):
    return _controlled_generated_path(
        root, (f'.generated/source-representations/{representation_id}/segments/'
               f'{segment_id}.txt'),
        ('.generated', 'source-representations'))


def _read_exact(path, limit, label):
    output = BytesIO()
    try:
        digest, size = stream_source(path, limit=limit, sink=output)
    except SourceError as exc:
        raise ExtractionPackError(f'{label} cannot be read safely') from exc
    return output.getvalue(), digest, size


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_mode)


def pack_identity(document):
    try:
        raw = canonical_bytes(document)
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise ExtractionPackError('verified extraction pack cannot be serialized') from exc
    if len(raw) > MAX_PACK_CANONICAL_BYTES:
        raise ExtractionPackError('verified extraction pack exceeds canonical byte limit')
    digest = sha256(raw).hexdigest()
    return raw, digest, 'XPACK-' + digest[:32]


def _context(root, contract_id):
    if not isinstance(contract_id, str) or not XCON_RE.fullmatch(contract_id):
        raise ExtractionPackError('invalid XCON ID')
    _, project_id, representations, extraction = _layers(root)
    contract = extraction.contracts.get(contract_id)
    if contract is None:
        raise ExtractionPackError('XCON does not exist or is invalid')
    representation = representations.representations.get(contract['representation_id'])
    if representation is None:
        raise ExtractionPackError('XCON representation does not exist or is invalid')
    descriptors = representation_descriptors(root, representation)
    return project_id, contract, representation, descriptors


def _base_document(project_id, contract, representation):
    return {
        'schema_version': 1,
        'profile': VERIFIED_EXTRACTION_PACK_PROFILE,
        'project_id': project_id,
        'contract_id': contract['contract_id'],
        'representation_id': representation['representation_id'],
        'segment_index_sha256': representation['segment_index']['sha256'],
        'allowed_kinds': list(contract['allowed_kinds']),
        'segments': [],
        'canonical_authority': False,
    }


def _build_pack(root, contract_id):
    project_id, contract, representation, descriptors = _context(root, contract_id)
    presented = contract['presented_segment_ids']
    if len(presented) > MAX_PACK_SEGMENTS:
        raise ExtractionPackError('XCON exceeds verified extraction pack segment limit')
    by_id = {item['segment_id']: item for item in descriptors}
    document = _base_document(project_id, contract, representation)
    projected_size = len(canonical_bytes(document))
    for segment_id in presented:
        descriptor = by_id.get(segment_id)
        if descriptor is None:
            raise ExtractionPackError('XCON references missing durable SEG descriptor')
        if descriptor['rendered_bytes'] > MAX_SINGLE_RENDERED_SEGMENT_BYTES:
            raise ExtractionPackError('rendered SEG exceeds verified extraction pack byte limit')
        cache = _segment_cache_path(root, representation['representation_id'], segment_id)
        raw, digest, size = _read_exact(
            cache, MAX_SINGLE_RENDERED_SEGMENT_BYTES, 'authorized rendered SEG cache')
        if (digest != descriptor['rendered_sha256']
                or size != descriptor['rendered_bytes']):
            raise ExtractionPackError('rendered SEG cache hash/byte commitment mismatch')
        try:
            text = raw.decode('utf-8')
        except UnicodeError as exc:
            raise ExtractionPackError('rendered SEG cache is not strict UTF-8') from exc
        segment = {
            'segment_id': segment_id,
            'locator': dict(descriptor['locator']),
            'rendered_sha256': descriptor['rendered_sha256'],
            'rendered_bytes': descriptor['rendered_bytes'],
            'text': text,
        }
        segment_size = len(canonical_bytes(segment))
        projected_size += segment_size + (1 if document['segments'] else 0)
        if projected_size > MAX_PACK_CANONICAL_BYTES:
            raise ExtractionPackError('verified extraction pack exceeds canonical byte limit')
        document['segments'].append(segment)
    _schema(document)
    raw, digest, pack_id = pack_identity(document)
    if len(raw) != projected_size:
        raise ExtractionPackError('verified extraction pack size accounting mismatch')
    return document, raw, digest, pack_id


def _existing_cache(path):
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        raise ExtractionPackError('generated extraction pack cannot be inspected') from exc
    if not stat.S_ISREG(info.st_mode):
        raise ExtractionPackError('generated extraction pack must be a regular file')
    identity = _identity(info)
    if info.st_size > MAX_PACK_CANONICAL_BYTES:
        return None, identity
    raw, _, _ = _read_exact(path, MAX_PACK_CANONICAL_BYTES, 'generated extraction pack')
    try:
        after = os.lstat(path)
    except OSError as exc:
        raise ExtractionPackError('generated extraction pack changed during inspection') from exc
    if _identity(after) != identity:
        raise ExtractionPackError('generated extraction pack changed during inspection')
    return raw, identity


def _materialize_pack(root, pack_id, raw):
    destination = _pack_path(root, pack_id)
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ExtractionPackError('generated extraction pack directory cannot be created') from exc
    destination = _pack_path(root, pack_id)
    current, original_identity = _existing_cache(destination)
    if current == raw:
        return 'existing'
    if original_identity is not None:
        raise ExtractionPackError(
            'generated extraction pack cache conflicts; '
            'explicit cache removal is required before recreation')
    descriptor, name = tempfile.mkstemp(
        prefix='.xpack-', suffix='.tmp', dir=destination.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        _pack_path(root, pack_id)
        try:
            # The completed temporary file is on the destination filesystem.
            # Linking creates the name atomically and never replaces another file.
            os.link(temporary, destination)
        except FileExistsError as exc:
            raise ExtractionPackError(
                'generated extraction pack appeared during publication; '
                'existing destination preserved') from exc
        return 'created'
    except ExtractionPackError:
        raise
    except (OSError, NotImplementedError) as exc:
        raise ExtractionPackError(
            'generated extraction pack cannot be published atomically') from exc
    finally:
        temporary.unlink(missing_ok=True)


def _verify_document(root, raw, document, expected_pack_id):
    if raw != canonical_bytes(document):
        raise ExtractionPackError('verified extraction pack is not canonical JSON')
    if len(document['segments']) > MAX_PACK_SEGMENTS:
        raise ExtractionPackError('verified extraction pack exceeds segment limit')
    canonical, digest, actual_pack_id = pack_identity(document)
    if canonical != raw or actual_pack_id != expected_pack_id:
        raise ExtractionPackError('verified extraction pack hash/identity mismatch')
    project_id, contract, representation, descriptors = _context(
        root, document['contract_id'])
    if document['project_id'] != project_id:
        raise ExtractionPackError('verified extraction pack project binding mismatch')
    if document['representation_id'] != contract['representation_id']:
        raise ExtractionPackError('verified extraction pack XCON/REP binding mismatch')
    if representation['representation_id'] != document['representation_id']:
        raise ExtractionPackError('verified extraction pack REP binding mismatch')
    if document['segment_index_sha256'] != representation['segment_index']['sha256']:
        raise ExtractionPackError('verified extraction pack segment-index binding mismatch')
    if document['allowed_kinds'] != contract['allowed_kinds']:
        raise ExtractionPackError('verified extraction pack allowed-kinds binding mismatch')
    expected_ids = contract['presented_segment_ids']
    actual_ids = [item['segment_id'] for item in document['segments']]
    if actual_ids != expected_ids or len(actual_ids) != len(set(actual_ids)):
        raise ExtractionPackError('verified extraction pack SEG membership/order mismatch')
    by_id = {item['segment_id']: item for item in descriptors}
    for segment in document['segments']:
        descriptor = by_id.get(segment['segment_id'])
        if descriptor is None:
            raise ExtractionPackError('verified extraction pack references unknown SEG')
        if descriptor['rendered_bytes'] > MAX_SINGLE_RENDERED_SEGMENT_BYTES:
            raise ExtractionPackError('verified extraction pack SEG exceeds byte limit')
        if (segment['locator'] != descriptor['locator']
                or segment['rendered_sha256'] != descriptor['rendered_sha256']
                or segment['rendered_bytes'] != descriptor['rendered_bytes']):
            raise ExtractionPackError('verified extraction pack SEG metadata mismatch')
        rendered = segment['text'].encode('utf-8')
        if len(rendered) > MAX_SINGLE_RENDERED_SEGMENT_BYTES:
            raise ExtractionPackError('verified extraction pack SEG exceeds byte limit')
        if (len(rendered) != segment['rendered_bytes']
                or sha256(rendered).hexdigest() != segment['rendered_sha256']):
            raise ExtractionPackError('verified extraction pack SEG text commitment mismatch')
    return {
        'pack_id': expected_pack_id,
        'pack_sha256': digest,
        'segments': len(document['segments']),
        'verified': True,
    }


def create_extraction_pack(root, contract_id):
    root = Path(root).absolute()
    try:
        document, raw, digest, pack_id = _build_pack(root, contract_id)
        # Reverify the exact bytes before publication; this path does not trust cache state.
        _verify_document(root, raw, document, pack_id)
        status = _materialize_pack(root, pack_id, raw)
        return {
            'pack_id': pack_id,
            'pack_sha256': digest,
            'pack_bytes': len(raw),
            'contract_id': document['contract_id'],
            'representation_id': document['representation_id'],
            'segments': len(document['segments']),
            'status': status,
        }
    except (ExtractionPackError, ExtractionError, SourceError, OSError,
            ValueError, TypeError) as exc:
        if isinstance(exc, ExtractionPackError):
            raise
        if isinstance(exc, (ExtractionError, SourceError)):
            raise ExtractionPackError(str(exc)) from exc
        raise ExtractionPackError(
            'verified extraction pack creation could not complete safely') from None


def verify_extraction_pack(root, pack_id):
    root = Path(root).absolute()
    try:
        path = _pack_path(root, pack_id)
        raw, _, _ = _read_exact(
            path, MAX_PACK_CANONICAL_BYTES, 'verified extraction pack')
        document = _strict_pack_json(raw)
        return _verify_document(root, raw, document, pack_id)
    except (ExtractionPackError, ExtractionError, SourceError, OSError,
            ValueError, TypeError) as exc:
        if isinstance(exc, ExtractionPackError):
            raise
        if isinstance(exc, (ExtractionError, SourceError)):
            raise ExtractionPackError(str(exc)) from exc
        raise ExtractionPackError(
            'verified extraction pack verification could not complete safely') from None
