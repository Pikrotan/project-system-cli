"""Durable Stage 13B1 representation metadata and deterministic validation."""

from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat

from .source_layer import (
    CAPTURE_RE, PROJECT_RE, SHA_RE, SourceError, canonical_bytes, checked_path,
    load_receipt, stream_source,
)

REPRESENTATION_PROFILE = 'project-system-representation-v1'
ADAPTER_ID = 'utf8-lines'
ADAPTER_VERSION = 1
ADAPTER_OPTIONS = {}
MAX_REPRESENTATION_SEGMENTS = 100_000
MAX_SEGMENT_INDEX_BYTES = 32 * 1024 * 1024
MAX_REPRESENTATION_RECEIPT_BYTES = 64 * 1024
REP_RE = re.compile(r'REP-[0-9a-f]{32}')
SEG_RE = re.compile(r'SEG-[0-9a-f]{32}')


class RepresentationError(SourceError):
    """Representation provenance cannot be produced or trusted."""


def adapter_identity():
    return {'id': ADAPTER_ID, 'version': ADAPTER_VERSION, 'options': {}}


def adapter_fingerprint():
    return sha256(canonical_bytes(adapter_identity())).hexdigest()


def segment_id(project_id, capture_id, fingerprint, descriptor):
    payload = {
        'project_id': project_id, 'capture_id': capture_id,
        'adapter_fingerprint': fingerprint, 'locator': descriptor['locator'],
        'source_span': descriptor['source_span'],
        'source_sha256': descriptor['source_sha256'],
        'source_bytes': descriptor['source_bytes'],
        'rendered_sha256': descriptor['rendered_sha256'],
        'rendered_bytes': descriptor['rendered_bytes'],
    }
    return 'SEG-' + sha256(canonical_bytes(payload)).hexdigest()[:32]


def representation_id(document):
    payload = {
        'project_id': document['project_id'], 'capture_id': document['capture_id'],
        'capture_content_sha256': document['capture_content_sha256'],
        'adapter_fingerprint': document['adapter']['fingerprint'],
        'segment_index_sha256': document['segment_index']['sha256'],
        'segment_index_bytes': document['segment_index']['bytes'],
        'segment_count': document['segment_index']['segments'],
    }
    return 'REP-' + sha256(canonical_bytes(payload)).hexdigest()[:32]


def intake_path(root, relative):
    pure = PurePosixPath(relative)
    if (pure.is_absolute() or pure.as_posix() != relative or not pure.parts
            or pure.parts[0] != 'intake' or any(part in {'.', '..'} for part in pure.parts)
            or any(char in relative for char in '\\:\x00')):
        raise RepresentationError('unsafe representation storage path')
    try:
        return checked_path(Path(root).absolute().joinpath(*pure.parts))
    except SourceError as exc:
        raise RepresentationError(str(exc)) from exc


def _exact_int(value):
    return type(value) is int and value >= 0


def validate_descriptor(document, project_id, capture_id, fingerprint, expected_ordinal,
                        previous_end):
    if not isinstance(document, dict) or set(document) != {
            'segment_id', 'ordinal', 'locator', 'source_span', 'source_sha256',
            'source_bytes', 'rendered_sha256', 'rendered_bytes'}:
        raise RepresentationError('invalid segment descriptor fields')
    if (not isinstance(document['locator'], dict)
            or set(document['locator']) != {'kind', 'line'}
            or document['locator']['kind'] != 'line'
            or not isinstance(document['source_span'], dict)
            or set(document['source_span']) != {'byte_start', 'byte_end'}):
        raise RepresentationError('invalid segment locator/source span')
    ordinal, line = document['ordinal'], document['locator']['line']
    start, end = document['source_span']['byte_start'], document['source_span']['byte_end']
    if (not _exact_int(ordinal) or ordinal != expected_ordinal
            or type(line) is not int or line != ordinal + 1
            or not _exact_int(start) or not _exact_int(end) or end <= start
            or start != previous_end or not _exact_int(document['source_bytes'])
            or document['source_bytes'] != end - start
            or not _exact_int(document['rendered_bytes'])
            or document['rendered_bytes'] > document['source_bytes']
            or not isinstance(document['source_sha256'], str)
            or not SHA_RE.fullmatch(document['source_sha256'])
            or not isinstance(document['rendered_sha256'], str)
            or not SHA_RE.fullmatch(document['rendered_sha256'])
            or not isinstance(document['segment_id'], str)
            or not SEG_RE.fullmatch(document['segment_id'])):
        raise RepresentationError('invalid segment ordering/span/hash/size')
    if document['segment_id'] != segment_id(project_id, capture_id, fingerprint, document):
        raise RepresentationError('segment_id does not match deterministic identity')
    return document


def parse_segment_index(raw, *, project_id, capture_id, fingerprint, capture_bytes=None):
    if len(raw) > MAX_SEGMENT_INDEX_BYTES:
        raise RepresentationError('segment index exceeds size limit')
    if raw and not raw.endswith(b'\n'):
        raise RepresentationError('non-empty segment index must end in LF')
    if b'\r' in raw:
        raise RepresentationError('segment index must use canonical LF serialization')
    lines = raw.split(b'\n')[:-1] if raw else []
    if len(lines) > MAX_REPRESENTATION_SEGMENTS:
        raise RepresentationError('representation exceeds segment-count limit')
    descriptors, previous_end, seen = [], 0, set()

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise RepresentationError('duplicate segment descriptor JSON key')
            result[key] = value
        return result

    def nonfinite(_):
        raise RepresentationError('non-finite segment descriptor JSON value')

    for ordinal, line in enumerate(lines):
        if not line:
            raise RepresentationError('empty segment descriptor line')
        try:
            document = json.loads(line.decode('utf-8'), object_pairs_hook=unique,
                                  parse_constant=nonfinite)
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise RepresentationError('segment index is not strict UTF-8 JSONL') from exc
        validate_descriptor(document, project_id, capture_id, fingerprint, ordinal, previous_end)
        if line != canonical_bytes(document):
            raise RepresentationError('segment descriptor is not canonical JSON')
        if document['segment_id'] in seen:
            raise RepresentationError('duplicate segment_id')
        seen.add(document['segment_id'])
        previous_end = document['source_span']['byte_end']
        descriptors.append(document)
    if capture_bytes is not None:
        if type(capture_bytes) is not int or previous_end != capture_bytes:
            raise RepresentationError('segment spans do not exactly cover Capture bytes')
        if (capture_bytes == 0) != (len(descriptors) == 0):
            raise RepresentationError('empty Capture/segment-count contradiction')
    return descriptors


def validate_representation(document):
    from .source_layer import _schema
    _schema(document, 'representation.schema.json')
    if (type(document['schema_version']) is not int
            or not isinstance(document['project_id'], str)
            or not PROJECT_RE.fullmatch(document['project_id'])
            or not isinstance(document['representation_id'], str)
            or not REP_RE.fullmatch(document['representation_id'])
            or not isinstance(document['capture_id'], str)
            or not CAPTURE_RE.fullmatch(document['capture_id'])
            or not isinstance(document['capture_content_sha256'], str)
            or not SHA_RE.fullmatch(document['capture_content_sha256'])
            or type(document['adapter']['version']) is not int
            or any(type(document['segment_index'][name]) is not int
                   for name in ('bytes', 'segments'))):
        raise RepresentationError('invalid exact representation identity/types')
    if document['adapter'] != {
            **adapter_identity(), 'fingerprint': adapter_fingerprint()}:
        raise RepresentationError('adapter identity/fingerprint mismatch')
    if document['representation_id'] != representation_id(document):
        raise RepresentationError('representation_id does not match deterministic identity')
    expected = f'intake/representations/{document["representation_id"]}.segments.jsonl'
    if document['segment_index']['path'] != expected:
        raise RepresentationError('segment index path does not match representation_id')
    return document


def load_representation(path):
    try:
        return load_receipt(path, validate_representation)
    except SourceError as exc:
        raise RepresentationError(str(exc)) from exc


@dataclass(frozen=True)
class RepresentationLayer:
    representations: dict
    issues: tuple


def inspect_representation_layer(root, config, source_layer):
    root = Path(root).absolute()
    representations, issues = {}, []
    try:
        os.lstat(root / 'intake')
    except FileNotFoundError:
        return RepresentationLayer(representations, ())
    except OSError:
        return RepresentationLayer(representations,
                                   (('ERROR', 'intake', 'intake layer cannot be inspected'),))

    def error(relative, message):
        issues.append(('ERROR', relative, message))

    def children(relative):
        try:
            directory = intake_path(root, relative)
            if not directory.exists():
                return []
            if not directory.is_dir():
                raise RepresentationError('representation storage is not a directory')
            result = []
            for child in sorted(directory.iterdir(), key=lambda item: item.name):
                rel = relative + '/' + child.name
                try:
                    child = intake_path(root, rel)
                    info = os.lstat(child)
                    if child.name == '.gitkeep' and stat.S_ISREG(info.st_mode) and info.st_size == 0:
                        continue
                    result.append((rel, child, info))
                except (RepresentationError, OSError, ValueError):
                    error(rel, 'unsafe representation storage entry')
            return result
        except (RepresentationError, OSError, ValueError):
            error(relative, 'representation storage cannot be inspected safely')
            return []

    files = children('intake/representations')
    receipts, indexes = {}, {}
    for rel, path, info in files:
        if not stat.S_ISREG(info.st_mode):
            error(rel, 'representation artifact must be a regular file')
        elif path.name.endswith('.segments.jsonl'):
            indexes[path.name.removesuffix('.segments.jsonl')] = (rel, path)
        elif path.suffix == '.json':
            receipts[path.stem] = (rel, path)
        else:
            error(rel, 'unexpected representation artifact')
    all_ids = sorted(set(receipts) | set(indexes))
    project = config.get('project') if isinstance(config, dict) else None
    project_id = project.get('id') if isinstance(project, dict) else None
    for rid in all_ids:
        receipt_entry, index_entry = receipts.get(rid), indexes.get(rid)
        location = (receipt_entry or index_entry)[0]
        if receipt_entry is None or index_entry is None:
            error(location, 'representation receipt/index pair is incomplete')
            continue
        try:
            document = load_representation(receipt_entry[1])
            if rid != document['representation_id'] or receipt_entry[1].name != rid + '.json':
                raise RepresentationError('representation filename/internal ID mismatch')
            if document['project_id'] != project_id:
                raise RepresentationError('representation project_id mismatch')
            capture = source_layer.captures.get(document['capture_id'])
            if capture is None:
                raise RepresentationError('representation references a missing valid Capture')
            if document['capture_content_sha256'] != capture['content_sha256']:
                raise RepresentationError('representation Capture content binding mismatch')
            if index_entry[0] != document['segment_index']['path']:
                raise RepresentationError('representation index filename/path mismatch')
            buffer = BytesIO()
            digest, size = stream_source(index_entry[1], limit=MAX_SEGMENT_INDEX_BYTES, sink=buffer)
            raw = buffer.getvalue()
            if (digest != document['segment_index']['sha256']
                    or size != document['segment_index']['bytes']):
                raise RepresentationError('segment index hash/byte binding mismatch')
            descriptors = parse_segment_index(
                raw, project_id=project_id, capture_id=capture['capture_id'],
                fingerprint=document['adapter']['fingerprint'], capture_bytes=capture['bytes'])
            if len(descriptors) != document['segment_index']['segments']:
                raise RepresentationError('segment index descriptor-count mismatch')
            representations.setdefault(rid, document)
        except SourceError as exc:
            error(location, str(exc))
    return RepresentationLayer(representations, tuple(sorted(set(issues))))
