"""Stage 13B1 deterministic utf8-lines representation of verified Capture bytes."""

import codecs
from hashlib import sha256
from io import BytesIO
import os
from pathlib import Path, PurePosixPath
import shutil
import tempfile

from yaml import YAMLError

from .representation_layer import (
    ADAPTER_ID, MAX_REPRESENTATION_SEGMENTS,
    MAX_SEGMENT_INDEX_BYTES, REPRESENTATION_PROFILE, RepresentationError,
    adapter_fingerprint, adapter_identity, intake_path, load_representation,
    parse_segment_index, representation_id, segment_id, validate_representation,
)
from .source_layer import (
    SourceError, canonical_bytes, checked_path, inspect_source_layer,
    project_identity, serialize_receipt, source_path, stream_source,
)
from .utils import load_yaml

ADAPTER_READ_CHUNK_BYTES = 128 * 1024


def _controlled_path(root, relative, prefix):
    pure = PurePosixPath(relative)
    if (pure.is_absolute() or pure.as_posix() != relative or not pure.parts
            or tuple(pure.parts[:len(prefix)]) != tuple(prefix)
            or any(part in {'.', '..'} for part in pure.parts)
            or any(char in relative for char in '\\:\x00')):
        raise RepresentationError('unsafe generated representation path')
    try:
        return checked_path(Path(root).absolute().joinpath(*pure.parts))
    except SourceError as exc:
        raise RepresentationError(str(exc)) from exc


def _file_exists(path):
    try:
        os.lstat(path)
        return True
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise RepresentationError('representation artifact cannot be inspected') from exc


def _read_exact(path, limit, label):
    buffer = BytesIO()
    try:
        _, size = stream_source(path, limit=limit, sink=buffer)
    except SourceError as exc:
        raise RepresentationError(f'{label} cannot be read safely') from exc
    return buffer.getvalue(), size


def _utf8_lines(verified, rendered_directory, *, project_id, capture_id, fingerprint):
    """Stream exact physical lines and keep rendered bytes only in temporary files."""
    rendered_directory.mkdir()
    decoder = codecs.getincrementaldecoder('utf-8')('strict')
    index_parts, descriptors, source_offset, index_size = [], [], 0, 0
    current_ordinal = 0
    current_path = rendered_directory / f'{current_ordinal:08d}.bin'
    output = current_path.open('wb')
    source_digest, rendered_digest = sha256(), sha256()
    source_size = rendered_size = 0
    pending = b''

    def write_rendered(data):
        nonlocal rendered_size
        if data:
            output.write(data)
            rendered_digest.update(data)
            rendered_size += len(data)

    def feed_content(data):
        nonlocal pending, source_size
        if not data:
            return
        source_digest.update(data)
        source_size += len(data)
        if pending:
            write_rendered(pending)
        if len(data) > 1:
            write_rendered(data[:-1])
        pending = data[-1:]

    def finish_line(has_lf):
        nonlocal output, source_digest, rendered_digest, source_size, rendered_size
        nonlocal pending, source_offset, current_ordinal, current_path, index_size
        if has_lf:
            source_digest.update(b'\n')
            source_size += 1
            if pending != b'\r':
                write_rendered(pending)
        else:
            write_rendered(pending)
        pending = b''
        output.flush()
        os.fsync(output.fileno())
        output.close()
        descriptor = {
            'ordinal': current_ordinal,
            'locator': {'kind': 'line', 'line': current_ordinal + 1},
            'source_span': {'byte_start': source_offset,
                            'byte_end': source_offset + source_size},
            'source_sha256': source_digest.hexdigest(), 'source_bytes': source_size,
            'rendered_sha256': rendered_digest.hexdigest(), 'rendered_bytes': rendered_size,
        }
        descriptor['segment_id'] = segment_id(
            project_id, capture_id, fingerprint, descriptor)
        line = canonical_bytes(descriptor) + b'\n'
        if current_ordinal >= MAX_REPRESENTATION_SEGMENTS:
            raise RepresentationError('representation exceeds segment-count limit')
        if index_size + len(line) > MAX_SEGMENT_INDEX_BYTES:
            raise RepresentationError('segment index exceeds size limit')
        final_path = rendered_directory / f'{descriptor["segment_id"]}.txt'
        os.replace(current_path, final_path)
        descriptors.append((descriptor, final_path))
        index_parts.append(line)
        index_size += len(line)
        source_offset += source_size
        current_ordinal += 1
        current_path = rendered_directory / f'{current_ordinal:08d}.bin'
        output = current_path.open('wb')
        source_digest, rendered_digest = sha256(), sha256()
        source_size = rendered_size = 0

    try:
        with Path(verified).open('rb') as stream:
            while True:
                chunk = stream.read(ADAPTER_READ_CHUNK_BYTES)
                if not chunk:
                    break
                decoder.decode(chunk, final=False)
                cursor = 0
                while True:
                    boundary = chunk.find(b'\n', cursor)
                    if boundary < 0:
                        feed_content(chunk[cursor:])
                        break
                    feed_content(chunk[cursor:boundary])
                    finish_line(True)
                    cursor = boundary + 1
            decoder.decode(b'', final=True)
        if source_size:
            finish_line(False)
        else:
            output.close()
            current_path.unlink(missing_ok=True)
    except UnicodeDecodeError as exc:
        raise RepresentationError('utf8-lines input is not strict UTF-8') from exc
    finally:
        if not output.closed:
            output.close()
    index = b''.join(index_parts)
    parse_segment_index(index, project_id=project_id, capture_id=capture_id,
                        fingerprint=fingerprint, capture_bytes=source_offset)
    return index, descriptors


def _publish_bytes(root, relative, raw, created):
    destination = intake_path(root, relative)
    destination.parent.mkdir(parents=True, exist_ok=True)
    intake_path(root, relative)
    descriptor, name = tempfile.mkstemp(prefix='.representation-', suffix='.tmp',
                                        dir=destination.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, destination)
        except FileExistsError:
            existing, _ = _read_exact(destination, max(len(raw), 1), 'existing representation artifact')
            if existing != raw:
                raise RepresentationError('existing immutable representation artifact conflicts')
            return False
        info = os.lstat(destination)
        created.append((relative, info.st_dev, info.st_ino))
        return True
    except OSError as exc:
        raise RepresentationError('representation artifact cannot be published atomically') from exc
    finally:
        temporary.unlink(missing_ok=True)


def _durable(root, document, index):
    receipt_relative = f'intake/representations/{document["representation_id"]}.json'
    index_relative = document['segment_index']['path']
    receipt_path, index_path = intake_path(root, receipt_relative), intake_path(root, index_relative)
    receipt_exists, index_exists = _file_exists(receipt_path), _file_exists(index_path)
    if receipt_exists != index_exists:
        raise RepresentationError('immutable representation receipt/index pair is incomplete')
    if receipt_exists:
        if load_representation(receipt_path) != document:
            raise RepresentationError('existing immutable representation receipt conflicts')
        existing, _ = _read_exact(index_path, MAX_SEGMENT_INDEX_BYTES, 'segment index')
        if existing != index:
            raise RepresentationError('existing immutable segment index conflicts')
        return False
    created = []
    try:
        index_created = _publish_bytes(root, index_relative, index, created)
        receipt_created = _publish_bytes(
            root, receipt_relative, serialize_receipt(document), created)
        return index_created or receipt_created
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


def _materialize_cache(root, representation, descriptors):
    base = (f'.generated/source-representations/'
            f'{representation["representation_id"]}/segments')
    directory = _controlled_path(root, base, ('.generated', 'source-representations'))
    directory.mkdir(parents=True, exist_ok=True)
    _controlled_path(root, base, ('.generated', 'source-representations'))
    changed = False
    for descriptor, temporary in descriptors:
        relative = f'{base}/{descriptor["segment_id"]}.txt'
        destination = _controlled_path(root, relative, ('.generated', 'source-representations'))
        if _file_exists(destination):
            try:
                digest, size = stream_source(destination)
                if (digest, size) == (descriptor['rendered_sha256'], descriptor['rendered_bytes']):
                    continue
            except SourceError:
                raise RepresentationError('generated segment cache is unsafe') from None
        fd, name = tempfile.mkstemp(prefix='.segment-', suffix='.tmp', dir=directory)
        cache_temp = Path(name)
        try:
            with os.fdopen(fd, 'wb') as output, temporary.open('rb') as source:
                shutil.copyfileobj(source, output, ADAPTER_READ_CHUNK_BYTES)
                output.flush()
                os.fsync(output.fileno())
            _controlled_path(root, relative, ('.generated', 'source-representations'))
            os.replace(cache_temp, destination)
            changed = True
        except OSError as exc:
            raise RepresentationError('generated segment cache cannot be materialized') from exc
        finally:
            cache_temp.unlink(missing_ok=True)
    return changed


def represent_source(root, capture_id, *, adapter, input_path=None):
    root = Path(root).absolute()
    try:
        if adapter != ADAPTER_ID:
            raise RepresentationError('unsupported representation adapter')
        config = load_yaml(checked_path(root / 'project.yaml'))
        project_id = project_identity(config)
        source_layer = inspect_source_layer(root, config)
        if source_layer.issues:
            raise RepresentationError('existing source layer is invalid; run project validate')
        capture = source_layer.captures.get(capture_id)
        if capture is None:
            raise RepresentationError('Capture does not exist or is invalid')
        if capture['retention'] == 'reference':
            if input_path is None:
                raise RepresentationError('reference Capture representation requires --input')
            source = checked_path(input_path)
        else:
            if input_path is not None:
                raise RepresentationError('repository_snapshot Capture forbids --input')
            source = source_path(root, capture['snapshot']['path'])
        with tempfile.TemporaryDirectory(prefix='project-representation-') as directory_name:
            directory = Path(directory_name)
            verified = directory / 'verified.bin'
            with verified.open('wb') as output:
                digest, size = stream_source(source, sink=output)
                output.flush()
                os.fsync(output.fileno())
            if (digest, size) != (capture['content_sha256'], capture['bytes']):
                raise RepresentationError('supplied bytes do not match Capture hash/size')
            rendered = directory / 'rendered'
            fingerprint = adapter_fingerprint()
            index, descriptors = _utf8_lines(
                verified, rendered, project_id=project_id,
                capture_id=capture_id, fingerprint=fingerprint)
            representation = {
                'schema_version': 1, 'profile': REPRESENTATION_PROFILE,
                'project_id': project_id, 'capture_id': capture_id,
                'capture_content_sha256': capture['content_sha256'],
                'adapter': {**adapter_identity(), 'fingerprint': fingerprint},
                'segment_index': {'path': '', 'sha256': sha256(index).hexdigest(),
                                  'bytes': len(index), 'segments': len(descriptors)},
                'canonical_authority': False,
            }
            representation['representation_id'] = representation_id(representation)
            representation['segment_index']['path'] = (
                f'intake/representations/{representation["representation_id"]}.segments.jsonl')
            validate_representation(representation)
            created = _durable(root, representation, index)
            cache_changed = _materialize_cache(root, representation, descriptors)
            status = 'created' if created else ('rebuilt_cache' if cache_changed else 'existing')
            return {'representation_id': representation['representation_id'],
                    'capture_id': capture_id, 'adapter': adapter,
                    'segments': len(descriptors), 'status': status}
    except (RepresentationError, SourceError, OSError, ValueError, TypeError, YAMLError) as exc:
        if isinstance(exc, RepresentationError):
            raise
        if isinstance(exc, SourceError):
            raise RepresentationError(str(exc)) from exc
        raise RepresentationError('source representation could not complete safely') from None
