"""Bounded Task Specification v1 binding/validation, not a task lifecycle."""

from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat

from jsonschema import Draft202012Validator

from .ids import PREFIX
from .object_loader import TYPE_DIRECTORIES, object_id_from_filename
from .process_runner import run_process
from .rule_scope import RuleScopeError, canonical_rule_path
from .schemas import validate_object_schema
from .skills import SKILL_NAME_RE, SkillError, _normalize_write_pattern, _pattern_contains
from .utils import ID_RE, atomic_write_text, distribution_root


PROFILE = 'project-system-task-spec-v1'
MAX_SPEC_BYTES = 1024 * 1024
GIT_TIMEOUT_SECONDS = 30
GIT_MAX_CAPTURE_BYTES = 64 * 1024
COMMIT_RE = re.compile(r'[0-9a-f]{40}')
SHA_RE = re.compile(r'[0-9a-f]{64}')
PROJECT_ID_RE = re.compile(r'[a-z0-9][a-z0-9_-]*')


class TaskSpecificationError(RuntimeError):
    """A trustworthy derived task contract cannot be established."""


def normalize_task_mode(mode):
    if not isinstance(mode, str) or not mode.strip() or '\x00' in mode:
        raise TaskSpecificationError('task mode must be a non-empty string')
    return mode.strip()


def task_project_id(config):
    project = config.get('project') if isinstance(config, dict) else None
    value = project.get('id') if isinstance(project, dict) else None
    if not isinstance(value, str) or not PROJECT_ID_RE.fullmatch(value):
        raise TaskSpecificationError('project.yaml project.id must be a canonical project ID')
    return value


def task_git_head(root):
    try:
        result = run_process(
            ['git', 'rev-parse', '--verify', 'HEAD^{commit}'], cwd=Path(root),
            capture_output=True, text=True, encoding='utf-8', check=False, shell=False,
            timeout=GIT_TIMEOUT_SECONDS, max_capture_bytes=GIT_MAX_CAPTURE_BYTES,
        )
    except Exception:
        raise TaskSpecificationError('cannot establish Git HEAD for Task Specification') from None
    value = getattr(result, 'stdout', None)
    if type(getattr(result, 'returncode', None)) is not int or result.returncode != 0 or not isinstance(value, str):
        raise TaskSpecificationError('cannot establish Git HEAD for Task Specification')
    value = value.removesuffix('\r\n') if value.endswith('\r\n') else value.removesuffix('\n')
    if not COMMIT_RE.fullmatch(value):
        raise TaskSpecificationError('Git HEAD identity is malformed or ambiguous')
    return value


def _path(value, label, *, pattern):
    try:
        if pattern:
            # Skills owns task authority grammar: exact paths or terminal /**.
            _normalize_write_pattern(value)
        else:
            canonical_rule_path(value, label, pattern=False)
    except (RuleScopeError, SkillError):
        raise TaskSpecificationError(f'{label} is not a safe canonical repository-relative path') from None
    parts = PurePosixPath(value).parts
    if (any(part.casefold() in {'.git', '.generated'} for part in parts)
            or any(char in parts[0] for char in '*?[]')
            or any(ord(char) < 32 for char in value)):
        raise TaskSpecificationError(f'{label} grants forbidden or derived authority')


def snapshot_task_target(root, layer, target):
    """Bind a discovered, schema-valid record, not filename-derived identity."""
    if not isinstance(target, str) or not ID_RE.fullmatch(target):
        raise TaskSpecificationError('task target must be a canonical object ID')
    record = layer.objects.get(target)
    matches = [item for item in layer.records if item.data.get('id') == target]
    if record is None or len(matches) != 1:
        raise TaskSpecificationError('task target is missing or ambiguous')
    selected = matches[0]
    data = record['data']
    obj_type = data.get('type')
    if (not isinstance(obj_type, str) or obj_type not in TYPE_DIRECTORIES
            or not isinstance(target, str) or not ID_RE.fullmatch(target)
            or selected.filename_id != target or validate_object_schema(data)):
        raise TaskSpecificationError('task target is not a valid canonical object')
    root = Path(root).absolute()
    path = Path(record['path']).absolute()
    try:
        relative = path.relative_to(root).as_posix()
        _path(relative, 'target.path', pattern=False)
        if path.parent != root / 'knowledge' / TYPE_DIRECTORIES[obj_type]:
            raise TaskSpecificationError('task target directory does not match its type')
        for candidate in (root, *(root.joinpath(*PurePosixPath(relative).parts[:index])
                                   for index in range(1, len(PurePosixPath(relative).parts) + 1))):
            info = os.lstat(candidate)
            if (stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0)
                    & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)):
                raise TaskSpecificationError('task target uses a symlink or reparse point')
        raw = path.read_bytes()
    except (OSError, ValueError) as exc:
        raise TaskSpecificationError('task target bytes/path cannot be established') from exc
    return {'id': data['id'], 'type': obj_type, 'path': relative, 'sha256': sha256(raw).hexdigest()}


def validate_task_specification(document):
    schema = json.loads((distribution_root() / 'schemas/task-spec.schema.json').read_text(encoding='utf-8'))
    errors = list(Draft202012Validator(schema).iter_errors(document))
    if errors:
        labels = sorted({'.'.join(map(str, error.absolute_path)) or '<root>' for error in errors})
        raise TaskSpecificationError('invalid Task Specification schema at: ' + ', '.join(labels))
    if type(document['schema_version']) is not int:
        raise TaskSpecificationError('invalid Task Specification version')
    for label, value, matcher in (
        ('project_id', document['project_id'], PROJECT_ID_RE),
        ('base_commit', document['base_commit'], COMMIT_RE),
        ('target.id', document['target']['id'], ID_RE),
        ('target.sha256', document['target']['sha256'], SHA_RE),
        ('skills.registry_sha256', document['skills']['registry_sha256'], SHA_RE),
    ):
        if not matcher.fullmatch(value):
            raise TaskSpecificationError(f'{label} is malformed')
    if normalize_task_mode(document['mode']) != document['mode']:
        raise TaskSpecificationError('task mode is not normalized')
    target = document['target']
    _path(target['path'], 'target.path', pattern=False)
    if (target['id'].split('-', 1)[0] != PREFIX[target['type']]
            or object_id_from_filename(target['path']) != target['id']
            or PurePosixPath(target['path']).suffix != '.md'
            or PurePosixPath(target['path']).parent.as_posix() != 'knowledge/' + TYPE_DIRECTORIES[target['type']]):
        raise TaskSpecificationError('target identity/type/path binding is inconsistent')
    for name, paths in document['write_scope'].items():
        if paths != sorted(set(paths)):
            raise TaskSpecificationError(f'write_scope.{name} must be sorted and unique')
        for value in paths:
            _path(value, f'write_scope.{name}', pattern=True)
    canonical = document['write_scope']['canonical']
    for value in document['write_scope']['effective']:
        if not any(_pattern_contains(ceiling, value) for ceiling in canonical):
            raise TaskSpecificationError('write_scope.effective exceeds canonical task authority')
    names = []
    for record in document['skills']['selected']:
        if (not SKILL_NAME_RE.fullmatch(record['name'])
                or record['path'] != f'.agents/skills/{record["name"]}/SKILL.md'
                or not SHA_RE.fullmatch(record['sha256'])):
            raise TaskSpecificationError('selected Skill identity is malformed')
        names.append(record['name'])
    if names != sorted(set(names)):
        raise TaskSpecificationError('selected Skills must be sorted and unique by name')
    return document


def build_task_specification(config, head, target, mode, manifest):
    if not isinstance(manifest, dict):
        raise TaskSpecificationError('task manifest must be an object')
    scopes = manifest.get('task_write_scope')
    canonical = scopes.get('canonical') if isinstance(scopes, dict) else None
    effective = manifest.get('effective_write_scope')
    selected = manifest.get('selected_skills')
    if (not isinstance(canonical, list) or not isinstance(effective, list)
            or any(not isinstance(value, str) for value in canonical + effective)
            or not isinstance(selected, list) or any(
                not isinstance(item, dict) or set(item) != {'name', 'path', 'sha256'}
                or not isinstance(item['name'], str) for item in selected)):
        raise TaskSpecificationError('task manifest scope/Skills evidence is malformed')
    document = {
        'schema_version': 1, 'profile': PROFILE, 'project_id': task_project_id(config),
        'base_commit': head, 'target': target, 'mode': normalize_task_mode(mode),
        'verification_checkpoint': 'task_verify',
        'write_scope': {'canonical': sorted(set(canonical)), 'effective': sorted(set(effective))},
        'skills': {'registry_sha256': manifest.get('skills_registry_sha256'),
                   'selected': sorted(selected, key=lambda item: item['name'])},
    }
    return validate_task_specification(document)


def serialize_task_specification(document):
    validate_task_specification(document)
    return json.dumps(document, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False) + '\n'


def write_task_specification(output, document):
    try:
        atomic_write_text(Path(output) / 'task-spec.json', serialize_task_specification(document))
    except (OSError, RuntimeError) as exc:
        if isinstance(exc, TaskSpecificationError):
            raise
        raise TaskSpecificationError('Task Specification could not be persisted safely') from exc


def load_task_specification(path):
    def unique_mapping(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise TaskSpecificationError('duplicate Task Specification JSON key')
            result[key] = value
        return result

    def reject_constant(value):
        raise TaskSpecificationError('non-finite Task Specification JSON value')

    try:
        with Path(path).open('rb') as stream:
            raw = stream.read(MAX_SPEC_BYTES + 1)
        if len(raw) > MAX_SPEC_BYTES:
            raise TaskSpecificationError('Task Specification exceeds size limit')
        document = json.loads(raw.decode('utf-8'), object_pairs_hook=unique_mapping,
                              parse_constant=reject_constant)
    except (OSError, ValueError, UnicodeError, RecursionError) as exc:
        raise TaskSpecificationError('Task Specification cannot be loaded') from exc
    return validate_task_specification(document)
