"""Durable source provenance contracts; source bytes have no product authority."""

from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat

from jsonschema import Draft202012Validator

from .utils import distribution_root

MAX_SOURCE_BYTES = 256 * 1024 * 1024
MAX_RECEIPT_BYTES = 64 * 1024
STREAM_CHUNK_BYTES = 128 * 1024
SOURCE_PROFILE = 'project-system-source-v1'
CAPTURE_PROFILE = 'project-system-source-capture-v1'
KINDS = ('conversation', 'document', 'image', 'archive', 'design', 'other')
SOURCE_RE = re.compile(r'SRC-[0-9a-f]{32}')
CAPTURE_RE = re.compile(r'CAP-[0-9a-f]{32}')
SHA_RE = re.compile(r'[0-9a-f]{64}')
PROJECT_RE = re.compile(r'[a-z0-9][a-z0-9_-]*')
KEY_RE = re.compile(r'[a-z0-9][a-z0-9._-]{0,79}')
PROVIDER_RE = re.compile(r'[a-z][a-z0-9_-]{0,63}')
MEDIA_RE = re.compile(r'[a-z0-9][a-z0-9!#$&^_.+-]{0,126}/[a-z0-9][a-z0-9!#$&^_.+-]{0,126}')


class SourceError(RuntimeError):
    """Source input, provenance or storage cannot be trusted."""


def canonical_bytes(document):
    return json.dumps(document, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=False, allow_nan=False).encode('utf-8')


def serialize_receipt(document):
    raw = (json.dumps(document, sort_keys=True, indent=2, ensure_ascii=False,
                      allow_nan=False) + '\n').encode('utf-8')
    if len(raw) > MAX_RECEIPT_BYTES:
        raise SourceError('source receipt exceeds size limit')
    return raw


def source_id(document):
    identity = {key: document[key] for key in ('project_id', 'key', 'provider', 'kind')}
    return 'SRC-' + sha256(canonical_bytes(identity)).hexdigest()[:32]


def capture_id(document):
    identity = {key: document[key] for key in (
        'project_id', 'source_id', 'content_sha256', 'bytes', 'media_type', 'retention')}
    return 'CAP-' + sha256(canonical_bytes(identity)).hexdigest()[:32]


def project_identity(config):
    project = config.get('project') if isinstance(config, dict) else None
    value = project.get('id') if isinstance(project, dict) else None
    if not isinstance(value, str) or not PROJECT_RE.fullmatch(value):
        raise SourceError('source provenance requires a valid current project ID')
    return value


def _schema(document, name):
    schema = json.loads((distribution_root() / 'schemas' / name).read_text('utf-8'))
    try:
        error = next(Draft202012Validator(schema).iter_errors(document), None)
    except (ValueError, TypeError, RecursionError) as exc:
        raise SourceError('invalid source receipt structure') from exc
    if error:
        location = '.'.join(map(str, error.absolute_path)) or '<root>'
        raise SourceError(f'invalid {name} at {location}')
    if type(document['schema_version']) is not int:
        raise SourceError('source schema_version must be an integer')
    if not PROJECT_RE.fullmatch(document['project_id']):
        raise SourceError('invalid source project ID')


def validate_source(document):
    _schema(document, 'source.schema.json')
    if (not KEY_RE.fullmatch(document['key']) or not PROVIDER_RE.fullmatch(document['provider'])
            or not SOURCE_RE.fullmatch(document['source_id'])):
        raise SourceError('invalid normalized source identity')
    if document['source_id'] != source_id(document):
        raise SourceError('source_id does not match semantic identity')
    return document


def validate_capture(document):
    _schema(document, 'source-capture.schema.json')
    if (type(document['bytes']) is not int or not SHA_RE.fullmatch(document['content_sha256'])
            or not CAPTURE_RE.fullmatch(document['capture_id'])
            or not SOURCE_RE.fullmatch(document['source_id'])
            or not MEDIA_RE.fullmatch(document['media_type'])):
        raise SourceError('invalid normalized capture identity')
    if document['capture_id'] != capture_id(document):
        raise SourceError('capture_id does not match semantic identity')
    snapshot = document['snapshot']
    if snapshot is not None:
        if (snapshot['path'] != f'sources/snapshots/{document["capture_id"]}/payload.bin'
                or snapshot['sha256'] != document['content_sha256']
                or type(snapshot['bytes']) is not int or snapshot['bytes'] != document['bytes']):
            raise SourceError('snapshot metadata contradicts capture identity/content')
    return document


def _no_link(info):
    if (stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0)
            & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)):
        raise SourceError('source path contains a symlink or reparse point')


def checked_path(path):
    """Inspect every lexical component without resolving away links."""
    path = Path(os.path.abspath(path))
    for component in (*reversed(path.parents), path):
        try:
            info = os.lstat(component)
        except FileNotFoundError:
            continue
        _no_link(info)
        if component != path and not stat.S_ISDIR(info.st_mode):
            raise SourceError('source path parent is not a directory')
    return path


def source_path(root, relative):
    pure = PurePosixPath(relative)
    if (pure.is_absolute() or pure.as_posix() != relative or not pure.parts
            or pure.parts[0] != 'sources' or any(part in {'.', '..'} for part in pure.parts)
            or any(char in relative for char in '\\:\x00')):
        raise SourceError('unsafe source storage path')
    return checked_path(Path(root).absolute().joinpath(*pure.parts))


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_mode)


def stream_source(path, *, limit=None, sink=None):
    """Read once, optionally copy the same chunks; never execute or parse bytes."""
    limit = MAX_SOURCE_BYTES if limit is None else limit
    try:
        path = checked_path(path)
        before = os.lstat(path)
        _no_link(before)
        if not stat.S_ISREG(before.st_mode):
            raise SourceError('source input must be a regular file')
        if before.st_size > limit:
            raise SourceError('source input exceeds size limit')
        digest, size = sha256(), 0
        with path.open('rb') as stream:
            opened = os.fstat(stream.fileno())
            if _identity(opened) != _identity(before):
                raise SourceError('source input changed while opening')
            for chunk in iter(lambda: stream.read(STREAM_CHUNK_BYTES), b''):
                size += len(chunk)
                if size > limit:
                    raise SourceError('source input exceeds size limit')
                digest.update(chunk)
                if sink is not None:
                    sink.write(chunk)
            if _identity(os.fstat(stream.fileno())) != _identity(before):
                raise SourceError('source input changed during capture')
        checked_path(path)
        after = os.lstat(path)
        if _identity(after) != _identity(before) or size != before.st_size:
            raise SourceError('source input changed during capture')
        return digest.hexdigest(), size
    except (OSError, ValueError) as exc:
        raise SourceError('source file cannot be inspected/read safely') from exc


def load_receipt(path, validator):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise SourceError('duplicate source receipt JSON key')
            result[key] = value
        return result

    def nonfinite(_):
        raise SourceError('non-finite source receipt JSON value')

    # Bounded receipt bytes use the same stable regular-file read boundary.
    from io import BytesIO
    buffer = BytesIO()
    stream_source(path, limit=MAX_RECEIPT_BYTES, sink=buffer)
    try:
        document = json.loads(buffer.getvalue().decode('utf-8'),
                              object_pairs_hook=unique, parse_constant=nonfinite)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise SourceError('source receipt is not strict UTF-8 JSON') from exc
    return validator(document)


@dataclass(frozen=True)
class SourceLayer:
    definitions: dict
    captures: dict
    issues: tuple


def inspect_source_layer(root, config):
    """Normal project validation consumes this deterministic provenance inventory."""
    root = Path(root).absolute()
    issues, definitions, captures = [], {}, {}
    # Old projects without this layer acquire no new filesystem constraints.
    # lstat still sees dangling symlinks, so an unsafe present layer is rejected.
    try:
        os.lstat(root / 'sources')
    except FileNotFoundError:
        return SourceLayer(definitions, captures, ())
    except OSError:
        return SourceLayer(definitions, captures,
                           (('ERROR', 'sources', 'source layer cannot be inspected'),))

    def error(relative, message):
        issues.append(('ERROR', relative, message))

    def children(relative):
        try:
            directory = source_path(root, relative)
            if not directory.exists():
                return []
            if not directory.is_dir():
                raise SourceError('source storage directory is not a directory')
            result = []
            for child in sorted(directory.iterdir(), key=lambda item: item.name):
                rel = relative + '/' + child.name
                try:
                    child = source_path(root, rel)
                    info = os.lstat(child)
                    if child.name == '.gitkeep' and stat.S_ISREG(info.st_mode) and info.st_size == 0:
                        continue
                    result.append((rel, child, info))
                except (SourceError, OSError, ValueError):
                    error(rel, 'unsafe source storage entry')
            return result
        except (SourceError, OSError, ValueError):
            error(relative, 'source storage directory cannot be inspected safely')
            return []

    top = children('sources')
    if not top and not issues:
        return SourceLayer(definitions, captures, ())
    try:
        project_id = project_identity(config)
    except SourceError as exc:
        error('sources', str(exc))
        project_id = None
    for rel, _, info in top:
        if rel not in {'sources/definitions', 'sources/captures', 'sources/snapshots'}:
            error(rel, 'unexpected source layer entry')
        elif not stat.S_ISDIR(info.st_mode):
            error(rel, 'source layer section must be a directory')

    keys, seen = {}, set()
    for directory, validator, field, records in (
            ('definitions', validate_source, 'source_id', definitions),
            ('captures', validate_capture, 'capture_id', captures)):
        for rel, path, info in children('sources/' + directory):
            if not stat.S_ISREG(info.st_mode) or path.suffix != '.json':
                error(rel, 'source definitions/captures support only JSON receipt files')
                continue
            try:
                doc = load_receipt(path, validator)
                identity = doc[field]
                if identity in seen:
                    error(rel, 'duplicate source/capture ID')
                seen.add(identity)
                if field == 'source_id':
                    if doc['key'] in keys:
                        error(rel, 'duplicate Source key')
                    keys[doc['key']] = identity
                if path.name != identity + '.json':
                    raise SourceError('source receipt filename/internal ID mismatch')
                if doc['project_id'] != project_id:
                    raise SourceError('source receipt project_id mismatch')
                if field == 'capture_id' and doc['source_id'] not in definitions:
                    raise SourceError('capture references a missing valid Source Definition')
                records.setdefault(identity, doc)
            except SourceError as exc:
                error(rel, str(exc))

    bound = {doc['snapshot']['path']: doc for doc in captures.values()
             if doc['retention'] == 'repository_snapshot'}
    for relative, doc in sorted(bound.items()):
        try:
            digest, size = stream_source(source_path(root, relative))
            if (digest, size) != (doc['content_sha256'], doc['bytes']):
                raise SourceError('repository snapshot hash/bytes mismatch')
        except (SourceError, OSError, ValueError) as exc:
            error(relative, str(exc) if isinstance(exc, SourceError) else 'unsafe repository snapshot')
    for rel, path, info in children('sources/snapshots'):
        if not stat.S_ISDIR(info.st_mode) or not CAPTURE_RE.fullmatch(path.name):
            error(rel, 'unexpected repository snapshot entry')
            continue
        for payload_rel, _, payload_info in children(rel):
            if not stat.S_ISREG(payload_info.st_mode) or payload_rel not in bound:
                error(payload_rel, 'repository snapshot is not bound by a valid capture receipt')
    return SourceLayer(definitions, captures, tuple(sorted(set(issues))))
