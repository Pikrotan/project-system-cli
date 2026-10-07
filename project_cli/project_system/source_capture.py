"""Explicit local-file capture into immutable durable source provenance."""

from contextlib import contextmanager
import os
from pathlib import Path
import tempfile

from yaml import YAMLError

from .source_layer import (
    CAPTURE_PROFILE, SOURCE_PROFILE, SourceError, capture_id, checked_path,
    inspect_source_layer, load_receipt, project_identity, serialize_receipt,
    source_id, source_path, stream_source, validate_capture, validate_source,
)
from .utils import load_yaml


@contextmanager
def _temporary(root, directory):
    parent = source_path(root, directory)
    parent.mkdir(parents=True, exist_ok=True)
    source_path(root, directory)
    descriptor, name = tempfile.mkstemp(prefix='.source-', suffix='.tmp', dir=parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            yield temporary, stream
    finally:
        # Only our own exclusive temporary pathname is removed. Never follow a
        # replaced directory to another location while cleaning up.
        source_path(root, temporary.relative_to(root).as_posix())
        temporary.unlink(missing_ok=True)


def _matching_receipt(path, expected, validator):
    if not path.exists():
        return False
    if load_receipt(path, validator) != expected:
        raise SourceError('existing immutable receipt conflicts with requested capture')
    return True


def _publish(root, relative, temporary, matches, created):
    """Atomic no-clobber publication; never replace an existing immutable file."""
    destination = source_path(root, relative)
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_path(root, relative)
    try:
        os.link(temporary, destination)
    except FileExistsError:
        source_path(root, relative)
        if not matches(destination):
            raise SourceError('existing immutable source artifact conflicts')
        return False
    info = os.lstat(destination)
    created.append((relative, info.st_dev, info.st_ino))
    return True


def _publish_receipt(root, relative, document, validator, created):
    destination = source_path(root, relative)
    if _matching_receipt(destination, document, validator):
        return False
    with _temporary(root, str(Path(relative).parent).replace('\\', '/')) as (temporary, stream):
        stream.write(serialize_receipt(document))
        stream.flush()
        os.fsync(stream.fileno())
        stream.close()
        return _publish(root, relative, temporary,
                        lambda path: _matching_receipt(path, document, validator), created)


def _capture(root, path, key, provider, kind, media_type, retention, created):
    root = checked_path(root)
    config = load_yaml(checked_path(root / 'project.yaml'))
    project_id = project_identity(config)
    definition = {'schema_version': 1, 'profile': SOURCE_PROFILE, 'project_id': project_id,
                  'key': key, 'provider': provider, 'kind': kind}
    definition['source_id'] = source_id(definition)
    validate_source(definition)
    # Validate capture metadata before opening private input or creating storage.
    probe = {'schema_version': 1, 'profile': CAPTURE_PROFILE, 'project_id': project_id,
             'source_id': definition['source_id'], 'content_sha256': '0' * 64,
             'bytes': 0, 'media_type': media_type, 'retention': retention, 'snapshot': None}
    probe['capture_id'] = capture_id(probe)
    if retention == 'repository_snapshot':
        probe['snapshot'] = {'path': f'sources/snapshots/{probe["capture_id"]}/payload.bin',
                             'sha256': probe['content_sha256'], 'bytes': 0}
    validate_capture(probe)
    layer = inspect_source_layer(root, config)
    if layer.issues:
        raise SourceError('existing source layer is invalid; run project validate')
    if any(item['key'] == key and item != definition for item in layer.definitions.values()):
        raise SourceError('Source key already belongs to a different immutable identity')
    path = checked_path(path)

    def publish(digest, size, temporary=None):
        capture = dict(probe, content_sha256=digest, bytes=size, snapshot=None)
        capture['capture_id'] = capture_id(capture)
        if retention == 'repository_snapshot':
            capture['snapshot'] = {'path': f'sources/snapshots/{capture["capture_id"]}/payload.bin',
                                   'sha256': digest, 'bytes': size}
        validate_capture(capture)
        source_relative = f'sources/definitions/{definition["source_id"]}.json'
        capture_relative = f'sources/captures/{capture["capture_id"]}.json'
        # Check both receipt destinations before publishing any new artifact.
        _matching_receipt(source_path(root, source_relative), definition, validate_source)
        _matching_receipt(source_path(root, capture_relative), capture, validate_capture)
        _publish_receipt(root, source_relative, definition, validate_source, created)
        if temporary is not None:
            _publish(root, capture['snapshot']['path'], temporary,
                     lambda existing: stream_source(existing) == (digest, size), created)
        fresh = _publish_receipt(root, capture_relative, capture, validate_capture, created)
        return {'source_id': definition['source_id'], 'capture_id': capture['capture_id'],
                'retention': retention, 'status': 'created' if fresh else 'existing'}

    if retention == 'reference':
        return publish(*stream_source(path))
    with _temporary(root, 'sources/snapshots') as (temporary, stream):
        digest, size = stream_source(path, sink=stream)
        stream.flush()
        os.fsync(stream.fileno())
        stream.close()
        return publish(digest, size, temporary)


def capture_source(root, path, *, key, provider, kind,
                   media_type='application/octet-stream', retention='reference'):
    """Capture opaque bytes without network, semantic edits, or Git operations."""
    root = Path(root).absolute()
    created = []
    try:
        return _capture(root, path, key, provider, kind, media_type, retention, created)
    except (SourceError, OSError, ValueError, TypeError, YAMLError) as exc:
        # Roll back only files published by this invocation and still belonging
        # to that inode. Existing receipts and snapshots are never rewritten.
        for relative, device, inode in reversed(created):
            try:
                destination = source_path(root, relative)
                current = os.lstat(destination)
                if (current.st_dev, current.st_ino) == (device, inode):
                    destination.unlink()
            except (SourceError, OSError):
                pass
        if isinstance(exc, SourceError):
            raise
        raise SourceError('source capture could not complete safely') from None
