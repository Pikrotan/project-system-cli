"""Creation-time canonical bindings and locators, not task verification policy."""

from hashlib import sha256
import json
from pathlib import Path, PurePosixPath

from jsonschema import Draft202012Validator
from yaml import YAMLError

from .frontmatter import read_object
from .object_loader import load_object_layer, object_id_from_filename
from .task_specification import (
    MAX_SPEC_BYTES, SHA_RE, TaskSpecificationError, _path,
    load_task_specification, snapshot_task_target,
)
from .utils import atomic_write_text, distribution_root


PROFILE = 'project-system-task-obligations-v1'
MAX_OBLIGATIONS_BYTES = 4 * 1024 * 1024
MAX_ACTIVE_CRITERIA_PER_REQUIREMENT = 9999


class TaskObligationsError(RuntimeError):
    """A trustworthy derived obligations artifact cannot be established."""


def _obligations(requirements):
    # Preflight every active source before allocating any obligation records.
    for source in requirements:
        if (source['status'] == 'active'
                and len(source['acceptance_criteria']) > MAX_ACTIVE_CRITERIA_PER_REQUIREMENT):
            raise TaskObligationsError(
                f'active requirement has more than {MAX_ACTIVE_CRITERIA_PER_REQUIREMENT} acceptance criteria'
            )
    return [
        {'id': f'{source["id"]}#acceptance-{index:04d}',
         'kind': 'acceptance_criterion', 'source_id': source['id'],
         'criterion_index': index, 'text': text}
        for source in requirements if source['status'] == 'active'
        for index, text in enumerate(source['acceptance_criteria'], 1)
    ]


def validate_task_obligations(document):
    schema = json.loads((distribution_root() / 'schemas/task-obligations.schema.json').read_text(encoding='utf-8'))
    try:
        errors = list(Draft202012Validator(schema).iter_errors(document))
    except RecursionError as exc:
        raise TaskObligationsError('Task Obligations nesting exceeds limits') from exc
    if errors:
        labels = sorted({'.'.join(map(str, error.absolute_path)) or '<root>' for error in errors})
        raise TaskObligationsError('invalid Task Obligations schema at: ' + ', '.join(labels))
    if type(document['schema_version']) is not int:
        raise TaskObligationsError('invalid Task Obligations version')
    if not SHA_RE.fullmatch(document['task_spec_sha256']):
        raise TaskObligationsError('invalid task_spec_sha256')
    for key, prefix, directory in (('requirements', 'REQ', 'requirements'), ('risks', 'RISK', 'risks')):
        sources = document[key]
        ids = [item['id'] for item in sources]
        if ids != sorted(set(ids)):
            raise TaskObligationsError(f'{key} must be sorted and unique by ID')
        for source in sources:
            try:
                _path(source['path'], f'{key}.path', pattern=False)
            except TaskSpecificationError as exc:
                raise TaskObligationsError(str(exc)) from exc
            path = PurePosixPath(source['path'])
            if (path.parent.as_posix() != f'knowledge/{directory}' or path.suffix != '.md'
                    or object_id_from_filename(source['path']) != source['id']
                    or source['id'].split('-', 1)[0] != prefix
                    or not SHA_RE.fullmatch(source['sha256'])):
                raise TaskObligationsError(f'{key} source identity/path/hash is inconsistent')
    for risk in document['risks']:
        if risk['affects'] != sorted(set(risk['affects'])):
            raise TaskObligationsError('risk affects must be sorted and unique')
    if any(type(item['criterion_index']) is not int for item in document['obligations']):
        raise TaskObligationsError('criterion_index must be an integer')
    if document['obligations'] != _obligations(document['requirements']):
        raise TaskObligationsError('obligations do not exactly match active source criteria')
    return document


def _read_bounded(path, limit, label):
    try:
        with Path(path).open('rb') as stream:
            raw = stream.read(limit + 1)
        if len(raw) > limit:
            raise TaskObligationsError(f'{label} exceeds size limit')
        return raw
    except OSError as exc:
        raise TaskObligationsError(f'{label} cannot be loaded') from exc


def _source(root, layer, identity):
    try:
        binding = snapshot_task_target(root, layer, identity)
        # Metadata must still describe the bytes bound by the safe snapshot.
        data, _ = read_object(Path(root) / binding['path'])
        if (data != layer.objects[identity]['data']
                or sha256((Path(root) / binding['path']).read_bytes()).hexdigest() != binding['sha256']):
            raise TaskObligationsError('canonical source changed during task binding')
        return binding, data
    except (TaskSpecificationError, OSError, ValueError, TypeError, YAMLError, RecursionError) as exc:
        raise TaskObligationsError('cannot bind canonical source: ' + identity) from exc


def build_task_obligations(root, spec_path, target):
    """Read the persisted spec, never regenerate it to conceal source drift."""
    raw = _read_bounded(spec_path, MAX_SPEC_BYTES, 'Task Specification')
    try:
        spec = load_task_specification(spec_path)
    except TaskSpecificationError as exc:
        raise TaskObligationsError('Task Specification binding is invalid') from exc
    if raw != _read_bounded(spec_path, MAX_SPEC_BYTES, 'Task Specification'):
        raise TaskObligationsError('Task Specification changed during binding')
    if spec['target']['id'] != target:
        raise TaskObligationsError('Task Specification target contradicts selected target')
    try:
        layer = load_object_layer(root)
    except (OSError, ValueError, TypeError, YAMLError, RecursionError) as exc:
        raise TaskObligationsError('canonical source inventory cannot be loaded') from exc
    if any(error.path.is_relative_to(Path(root) / 'knowledge/risks') for error in layer.errors):
        raise TaskObligationsError('risk inventory cannot be parsed; relevance cannot be established')
    binding, data = _source(root, layer, target)
    if binding != spec['target']:
        raise TaskObligationsError('Task Specification target contradicts current canonical source')
    selected = []
    if data['type'] == 'requirement':
        selected = [target]
    elif data['type'] == 'feature':
        selected = data.get('requirements', [])
        if (not isinstance(selected, list) or any(not isinstance(item, str) for item in selected)
                or len(selected) != len(set(selected))):
            raise TaskObligationsError('feature requirements must be unique string references')
    requirements = []
    for identity in sorted(selected):
        source, metadata = _source(root, layer, identity)
        if identity == target and source != binding:
            raise TaskObligationsError('canonical task target changed during source resolution')
        if metadata['type'] != 'requirement':
            raise TaskObligationsError('feature requirement reference has wrong object type')
        requirements.append({key: source[key] for key in ('id', 'path', 'sha256')} | {
            'status': metadata['status'], 'priority': metadata.get('priority'),
            'acceptance_criteria': metadata.get('acceptance_criteria', []),
        })
    relevant = {target, *selected}
    risk_ids = set()
    for record in layer.records:
        metadata = record.data
        if metadata.get('type') != 'risk':
            continue
        affects = metadata.get('affects', [])
        if not isinstance(affects, list) or any(not isinstance(item, str) for item in affects):
            raise TaskObligationsError('risk affects must be structured string references')
        if metadata.get('id') == target or relevant.intersection(affects):
            identity = metadata.get('id')
            if not isinstance(identity, str):
                raise TaskObligationsError('relevant risk identity is malformed')
            risk_ids.add(identity)
    risks = []
    for identity in sorted(risk_ids):
        source, metadata = _source(root, layer, identity)
        if identity == target and source != binding:
            raise TaskObligationsError('canonical task target changed during source resolution')
        risks.append({key: source[key] for key in ('id', 'path', 'sha256')} | {
            'status': metadata['status'], 'severity': metadata.get('severity'),
            'mitigation': metadata.get('mitigation'), 'affects': sorted(set(metadata.get('affects', []))),
        })
    return validate_task_obligations({
        'schema_version': 1, 'profile': PROFILE, 'task_spec_sha256': sha256(raw).hexdigest(),
        'requirements': requirements, 'risks': risks, 'obligations': _obligations(requirements),
    })


def serialize_task_obligations(document):
    validate_task_obligations(document)
    text = json.dumps(document, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False) + '\n'
    if len(text.encode('utf-8')) > MAX_OBLIGATIONS_BYTES:
        raise TaskObligationsError('Task Obligations exceeds size limit')
    return text


def write_task_obligations(output, document):
    try:
        atomic_write_text(Path(output) / 'task-obligations.json', serialize_task_obligations(document))
    except (OSError, RuntimeError) as exc:
        if isinstance(exc, TaskObligationsError):
            raise
        raise TaskObligationsError('Task Obligations could not be persisted safely') from exc


def load_task_obligations(path):
    def unique_mapping(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise TaskObligationsError('duplicate Task Obligations JSON key')
            result[key] = value
        return result

    def reject_constant(value):
        raise TaskObligationsError('non-finite Task Obligations JSON value')

    raw = _read_bounded(path, MAX_OBLIGATIONS_BYTES, 'Task Obligations')
    try:
        document = json.loads(raw.decode('utf-8'), object_pairs_hook=unique_mapping,
                              parse_constant=reject_constant)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise TaskObligationsError('Task Obligations cannot be loaded') from exc
    return validate_task_obligations(document)
