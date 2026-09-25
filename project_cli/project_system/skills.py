"""Project-local portable Skills with deterministic registry and scope checks."""

from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
from urllib.parse import unquote, urlsplit

from jsonschema import Draft202012Validator
import yaml

from .frontmatter import StrictSafeLoader
from .utils import atomic_write_text, distribution_root, load_yaml


SKILLS_SCHEMA_VERSION = 1
SKILLS_ERA_PROJECT_CLI_VERSION = '0.12.0'
SKILLS_PROFILE = 'project-system-skills-v1'
CORE_SKILLS = (
    'knowledge-sync',
    'decision-management',
    'requirements-management',
    'architecture-impact',
    'implementation-plan',
    'project-validation',
    'release-check',
)
DESIGN_SKILL = 'design-handoff'
PACKAGED_SKILLS = frozenset((*CORE_SKILLS, DESIGN_SKILL))
SKILL_NAME_RE = re.compile(r'^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$')
SHA256_RE = re.compile(r'^[0-9a-f]{64}$')
MAX_REGISTRY_BYTES = 1024 * 1024
MAX_SKILL_BYTES = 512 * 1024
MAX_SKILL_FRONTMATTER_BYTES = 32 * 1024
KNOWN_CAPABILITIES = frozenset({
    'project.new',
    'project.context',
    'project.impact',
    'project.validate',
    'project.generate',
    'project.health',
    'project.prepare-pr',
    'project.sync.intake',
    'project.sync.plan',
    'project.sync.verify',
    'project.sync.finalize.prepare',
    'project.google.workspace.status',
    'project.google.workspace.sync',
})
FORBIDDEN_WRITE_ROOTS = frozenset({
    '.git', '.github', '.project', '.agents', '.generated', 'history',
})
FORBIDDEN_WRITE_ROOT_IDENTITIES = frozenset(
    value.casefold() for value in FORBIDDEN_WRITE_ROOTS
)
_LOCAL_LINK_RE = re.compile(r'!?\[[^\]]*\]\(([^)]+)\)')
_V011_AGENTS_SHA256 = 'af4a1932da7217f906ba6e689cc4b0b4c921bc863918e069def80d161b4b84fb'
_V011_RULES_SHA256 = '758542192c2a1b0dc6a7a521d9b3d2200d33371b50ecc6638645829c13f47b04'


class SkillError(RuntimeError):
    pass


@dataclass(frozen=True)
class SkillRecord:
    name: str
    path: Path
    description: str
    body: str
    content: str
    sha256: str
    max_writes: tuple[str, ...]
    capabilities: tuple[str, ...]


@dataclass
class SkillLayer:
    active: bool
    registry: dict | None
    registry_sha256: str | None
    records: dict[str, SkillRecord]
    issues: list[tuple[str, str, str]]


def _reject_aliases(text, label):
    try:
        tokens = yaml.scan(text, Loader=yaml.SafeLoader)
        for token in tokens:
            if isinstance(token, (yaml.tokens.AnchorToken, yaml.tokens.AliasToken)):
                raise SkillError(f'YAML anchors and aliases are not allowed in {label}')
    except SkillError:
        raise
    except Exception as exc:
        raise SkillError(f'cannot scan {label}: {exc}') from exc


def _strict_yaml_bytes(raw, label):
    if len(raw) > MAX_REGISTRY_BYTES:
        raise SkillError(f'{label} exceeds {MAX_REGISTRY_BYTES} bytes')
    try:
        text = raw.decode('utf-8-sig')
    except UnicodeDecodeError as exc:
        raise SkillError(f'{label} must be UTF-8') from exc
    _reject_aliases(text, label)
    try:
        value = yaml.load(text, Loader=StrictSafeLoader)
    except Exception as exc:
        raise SkillError(f'cannot parse {label}: {exc}') from exc
    if not isinstance(value, dict):
        raise SkillError(f'{label} root must be a mapping/object')
    return value


def _schema_messages(document):
    schema = json.loads(
        (distribution_root() / 'schemas' / 'skills.schema.json').read_text(encoding='utf-8')
    )
    validator = Draft202012Validator(schema)
    messages = []
    for error in sorted(validator.iter_errors(document), key=lambda item: list(item.path)):
        location = '.'.join(str(part) for part in error.path) or '<root>'
        messages.append(f'{location}: {error.message}')
    return messages


def _path_has_reparse(path):
    path = Path(path)
    if not path.exists() and not path.is_symlink():
        return False
    if path.is_symlink():
        return True
    try:
        attributes = getattr(os.lstat(path), 'st_file_attributes', 0)
    except OSError:
        return True
    flag = getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)
    return bool(attributes & flag)


def _safe_existing_path(base, candidate, label):
    base = Path(base).resolve()
    candidate = Path(candidate)
    try:
        relative = candidate.absolute().relative_to(base)
    except ValueError as exc:
        raise SkillError(f'{label} escapes its allowed root') from exc
    current = base
    for part in relative.parts:
        current = current / part
        if _path_has_reparse(current):
            raise SkillError(f'{label} contains a symlink or reparse point: {current}')
    try:
        candidate.resolve(strict=False).relative_to(base)
    except ValueError as exc:
        raise SkillError(f'{label} resolves outside its allowed root') from exc
    return candidate


def _read_skill_document(path):
    raw = Path(path).read_bytes()
    if len(raw) > MAX_SKILL_BYTES:
        raise SkillError(f'SKILL.md exceeds {MAX_SKILL_BYTES} bytes')
    try:
        text = raw.decode('utf-8')
    except UnicodeDecodeError as exc:
        raise SkillError('SKILL.md must be UTF-8') from exc
    normalized = text.replace('\r\n', '\n')
    if not normalized.startswith('---\n'):
        raise SkillError('SKILL.md is missing YAML frontmatter')
    parts = normalized.split('---\n', 2)
    if len(parts) != 3:
        raise SkillError('SKILL.md has unterminated YAML frontmatter')
    frontmatter_text = parts[1]
    if len(frontmatter_text.encode('utf-8')) > MAX_SKILL_FRONTMATTER_BYTES:
        raise SkillError('SKILL.md frontmatter exceeds its size limit')
    _reject_aliases(frontmatter_text, 'SKILL.md frontmatter')
    try:
        frontmatter = yaml.load(frontmatter_text, Loader=StrictSafeLoader)
    except Exception as exc:
        raise SkillError(f'cannot parse SKILL.md frontmatter: {exc}') from exc
    if not isinstance(frontmatter, dict):
        raise SkillError('SKILL.md frontmatter must be a mapping/object')
    if set(frontmatter) != {'name', 'description'}:
        raise SkillError('SKILL.md frontmatter must contain only name and description')
    name = frontmatter.get('name')
    description = frontmatter.get('description')
    if not isinstance(name, str) or not SKILL_NAME_RE.fullmatch(name):
        raise SkillError('SKILL.md name is invalid')
    if (
        not isinstance(description, str)
        or not description.strip()
        or description != description.strip()
        or '\n' in description
    ):
        raise SkillError('SKILL.md description must be a non-empty single line')
    body = parts[2].lstrip('\n')
    if not body.strip():
        raise SkillError('SKILL.md body must not be empty')
    return name, description, body, normalized, sha256(raw).hexdigest()


def _normalize_write_pattern(value):
    if not isinstance(value, str) or not value:
        raise SkillError('max_writes entry must be a non-empty string')
    if value != value.strip() or '\\' in value or ':' in value or '\x00' in value:
        raise SkillError(f'unsafe max_writes path: {value}')
    is_tree = value.endswith('/**')
    base = value[:-3] if is_tree else value
    if '*' in base or '?' in base or '[' in base or ']' in base:
        raise SkillError(f'unsupported max_writes glob: {value}')
    pure = PurePosixPath(base)
    if pure.is_absolute() or not pure.parts or any(part in {'', '.', '..'} for part in pure.parts):
        raise SkillError(f'unsafe max_writes path: {value}')
    if (
        pure.parts[0].casefold() in FORBIDDEN_WRITE_ROOT_IDENTITIES
        or pure.as_posix().casefold() == 'project.yaml'
    ):
        raise SkillError(f'forbidden max_writes root: {value}')
    normalized = pure.as_posix() + ('/**' if is_tree else '')
    if normalized != value:
        raise SkillError(f'max_writes path is not normalized: {value}')
    return normalized


def _pattern_contains(pattern, candidate):
    if pattern.endswith('/**'):
        base = pattern[:-3]
        return candidate == base or candidate.startswith(base + '/')
    return candidate == pattern


def _intersect_patterns(left, right):
    if _pattern_contains(left, right):
        return right
    if _pattern_contains(right, left):
        return left
    return None


def effective_write_scope(records, task_paths, governance_paths=None):
    """Intersect every Skill independently; never compose partial authority."""
    normalized_task = tuple(_normalize_write_pattern(path) for path in task_paths)
    governance_paths = normalized_task if governance_paths is None else tuple(
        _normalize_write_pattern(path) for path in governance_paths
    )
    governed = set()
    for task_path in normalized_task:
        for policy_path in governance_paths:
            intersection = _intersect_patterns(task_path, policy_path)
            if intersection is not None:
                governed.add(intersection)
    authorizations = {}
    for name, record in records.items():
        for skill_path in record.max_writes:
            for task_path in governed:
                intersection = _intersect_patterns(skill_path, task_path)
                if intersection is not None:
                    authorizations.setdefault(intersection, set()).add(name)
    rendered = {
        path: sorted(names) for path, names in sorted(authorizations.items())
    }
    return sorted(rendered), rendered


def design_integration_enabled(config):
    if not isinstance(config, dict):
        return False
    external = config.get('external_systems') or {}
    if not isinstance(external, dict):
        return False
    google = external.get('google_workspace') or {}
    if isinstance(google, dict) and google.get('enabled') is True:
        if google.get('design_knowledge', True) is True or google.get('design_changes', True) is True:
            return True
    for key in ('figma', 'designer_docs', 'design_changes'):
        value = external.get(key) or {}
        if isinstance(value, dict) and value.get('enabled') is True:
            return True
    return False


def required_skill_names(config):
    names = list(CORE_SKILLS)
    if design_integration_enabled(config):
        names.append(DESIGN_SKILL)
    return tuple(names)


def skills_enabled(config):
    tooling = config.get('tooling', {}) if isinstance(config, dict) else {}
    marker = tooling.get('skills_schema_version') if isinstance(tooling, dict) else None
    return type(marker) is int and marker == SKILLS_SCHEMA_VERSION


def _catalog_path():
    return distribution_root() / 'skills' / 'catalog.yaml'


def load_packaged_catalog():
    path = _catalog_path()
    raw = path.read_bytes()
    catalog = _strict_yaml_bytes(raw, 'packaged Skills catalog')
    messages = _schema_messages(catalog)
    if messages:
        raise SkillError('packaged Skills catalog schema failed: ' + '; '.join(messages))
    if set(catalog['skills']) != PACKAGED_SKILLS:
        raise SkillError('packaged Skills catalog does not contain the exact v1 Skill set')
    migrations = distribution_root() / 'skills' / 'migrations' / 'v0.11'
    migration_hashes = {
        'AGENTS.md': _V011_AGENTS_SHA256,
        'PROJECT_RULES.md': _V011_RULES_SHA256,
    }
    for filename, expected_hash in migration_hashes.items():
        path = migrations / filename
        if not path.is_file() or sha256(path.read_bytes()).hexdigest() != expected_hash:
            raise SkillError(f'packaged v0.11 migration identity is invalid: {filename}')
    return catalog


def _parse_local_link(raw):
    value = raw.strip()
    if value.startswith('<') and '>' in value:
        value = value[1:value.index('>')]
    elif ' ' in value:
        value = value.split(' ', 1)[0]
    parsed = urlsplit(value)
    if parsed.scheme in {'http', 'https', 'mailto'}:
        return None
    if parsed.scheme or parsed.netloc:
        raise SkillError(f'unsupported SKILL.md link target: {raw}')
    path = unquote(parsed.path)
    if not path:
        return None
    return path


def _validate_skill_resources(skill_dir, body):
    for candidate in skill_dir.iterdir():
        if candidate.is_dir() and candidate.name not in {'references', 'assets'}:
            raise SkillError(f'unsupported Skill resource directory: {candidate.name}')
        if candidate.is_file() and candidate.name != 'SKILL.md':
            raise SkillError(f'unsupported Skill root file: {candidate.name}')
    for candidate in skill_dir.rglob('*'):
        if _path_has_reparse(candidate):
            raise SkillError(f'Skill contains a symlink or reparse point: {candidate.name}')
        relative = candidate.relative_to(skill_dir)
        if relative.parts and relative.parts[0] == 'scripts':
            raise SkillError('skill-local executable scripts are not supported in Skills v1')
    for match in _LOCAL_LINK_RE.finditer(body):
        target = _parse_local_link(match.group(1))
        if target is None:
            continue
        if '\\' in target or ':' in target:
            raise SkillError(f'unsafe local SKILL.md reference: {target}')
        pure = PurePosixPath(target)
        if pure.is_absolute() or any(part in {'', '.', '..'} for part in pure.parts):
            raise SkillError(f'unsafe local SKILL.md reference: {target}')
        candidate = skill_dir.joinpath(*pure.parts)
        _safe_existing_path(skill_dir, candidate, 'SKILL.md reference')
        if not candidate.is_file():
            raise SkillError(f'missing local SKILL.md reference: {target}')


def _inspect_registered_skills(root, registry, issues):
    root = Path(root).resolve()
    skills_root = root / '.agents' / 'skills'
    records = {}
    if not skills_root.is_dir():
        issues.append(('ERROR', '.agents/skills', 'activated Skills layer is missing its Skill directory'))
        return records
    try:
        _safe_existing_path(root, root / '.agents', '.agents')
        _safe_existing_path(root, skills_root, '.agents/skills')
    except SkillError as exc:
        issues.append(('ERROR', '.agents/skills', str(exc)))
        return records

    registry_names = list(registry.get('skills', {}))
    lower_registry = [name.casefold() for name in registry_names]
    if len(lower_registry) != len(set(lower_registry)):
        issues.append(('ERROR', '.project/skills.yaml', 'case-insensitive duplicate Skill names'))

    physical = []
    for candidate in skills_root.iterdir():
        if candidate.name == '.gitkeep':
            continue
        if not candidate.is_dir():
            issues.append(('ERROR', str(candidate.relative_to(root)), 'unexpected file in Skills root'))
            continue
        physical.append(candidate.name)
    if len({name.casefold() for name in physical}) != len(physical):
        issues.append(('ERROR', '.agents/skills', 'case-insensitive duplicate Skill directories'))
    for name in sorted(set(physical) - set(registry_names)):
        issues.append(('ERROR', f'.agents/skills/{name}', 'unregistered project Skill'))
    for name in sorted(set(registry_names) - set(physical)):
        issues.append(('ERROR', f'.agents/skills/{name}', 'registered Skill directory is missing'))

    for name, entry in registry.get('skills', {}).items():
        location = f'.agents/skills/{name}/SKILL.md'
        if not SKILL_NAME_RE.fullmatch(name):
            issues.append(('ERROR', location, 'invalid Skill name'))
            continue
        skill_dir = skills_root / name
        skill_path = skill_dir / 'SKILL.md'
        if not skill_path.is_file():
            continue
        try:
            _safe_existing_path(skills_root, skill_dir, f'Skill {name}')
            _safe_existing_path(skill_dir, skill_path, f'Skill {name} entrypoint')
            fm_name, description, body, content, digest = _read_skill_document(skill_path)
            if fm_name != name or skill_dir.name != name:
                raise SkillError('Skill frontmatter name, folder and registry key must match')
            max_writes = tuple(_normalize_write_pattern(value) for value in entry['max_writes'])
            for pattern in max_writes:
                base = pattern[:-3] if pattern.endswith('/**') else pattern
                candidate = root.joinpath(*PurePosixPath(base).parts)
                _safe_existing_path(root, candidate, f'max_writes path {pattern}')
            capabilities = tuple(entry['capabilities'])
            unknown = sorted(set(capabilities) - KNOWN_CAPABILITIES)
            if unknown:
                raise SkillError(f'unknown capabilities: {unknown}')
            _validate_skill_resources(skill_dir, body)
            records[name] = SkillRecord(
                name=name,
                path=skill_path,
                description=description,
                body=body,
                content=content,
                sha256=digest,
                max_writes=max_writes,
                capabilities=capabilities,
            )
        except (OSError, KeyError, SkillError) as exc:
            issues.append(('ERROR', location, str(exc)))
    return records


def _version_at_least_012(value):
    if not isinstance(value, str):
        return False
    match = re.match(r'^(\d+)\.(\d+)\.(\d+)', value)
    return bool(match and tuple(int(part) for part in match.groups()) >= (0, 12, 0))


def skills_era_project_cli_version(value):
    """Return the durable tooling target for a Skills-era project."""
    return value if _version_at_least_012(value) else SKILLS_ERA_PROJECT_CLI_VERSION


def project_is_skills_era(config):
    """Classify a project from durable config, including malformed marker presence."""
    if not isinstance(config, dict):
        return False
    tooling = config.get('tooling')
    if not isinstance(tooling, dict):
        return False
    return (
        'skills_schema_version' in tooling
        or _version_at_least_012(tooling.get('project_cli'))
    )


def inspect_skill_layer(root, config=None, *, check_package=True):
    root = Path(root).resolve()
    config = load_yaml(root / 'project.yaml') if config is None else config
    issues = []
    if check_package:
        try:
            catalog = load_packaged_catalog()
            for name in PACKAGED_SKILLS:
                path = distribution_root() / 'skills' / name / 'SKILL.md'
                if not path.is_file():
                    raise SkillError(f'packaged Skill template is missing: {name}')
                fm_name, _, body, _, _ = _read_skill_document(path)
                if fm_name != name:
                    raise SkillError(f'packaged Skill identity mismatch: {name}')
                _validate_skill_resources(path.parent, body)
                for value in catalog['skills'][name]['max_writes']:
                    _normalize_write_pattern(value)
        except (OSError, SkillError) as exc:
            issues.append(('BLOCKING', 'project_system_assets/skills', str(exc)))

    tooling = config.get('tooling', {}) if isinstance(config, dict) else {}
    if not isinstance(tooling, dict):
        tooling = {}
    marker_present = 'skills_schema_version' in tooling
    marker = tooling.get('skills_schema_version')
    registry_path = root / '.project' / 'skills.yaml'
    skills_root = root / '.agents' / 'skills'
    physical_present = registry_path.exists() or skills_root.exists()
    if not marker_present:
        if physical_present:
            issues.append(('ERROR', '.project/skills.yaml', 'Skills files exist without tooling.skills_schema_version activation'))
        elif _version_at_least_012(tooling.get('project_cli')):
            issues.append(('ERROR', 'project.yaml', 'CLI 0.12+ project is missing tooling.skills_schema_version'))
        else:
            issues.append(('WARNING', 'project.yaml', 'legacy project has no Skills v1 layer; run project skills install'))
        return SkillLayer(False, None, None, {}, issues)
    if type(marker) is not int or marker != SKILLS_SCHEMA_VERSION:
        issues.append(('BLOCKING', 'project.yaml', f'unsupported skills_schema_version: {marker!r}'))
        return SkillLayer(True, None, None, {}, issues)
    if not registry_path.is_file():
        issues.append(('BLOCKING', '.project/skills.yaml', 'activated Skills layer is missing its registry'))
        return SkillLayer(True, None, None, {}, issues)
    try:
        _safe_existing_path(root, root / '.project', '.project')
        _safe_existing_path(root, registry_path, 'Skills registry')
        raw = registry_path.read_bytes()
        registry = _strict_yaml_bytes(raw, 'Skills registry')
    except (OSError, SkillError) as exc:
        issues.append(('BLOCKING', '.project/skills.yaml', str(exc)))
        return SkillLayer(True, None, None, {}, issues)
    schema_messages = _schema_messages(registry)
    if schema_messages:
        issues.extend(('BLOCKING', '.project/skills.yaml', message) for message in schema_messages)
        return SkillLayer(True, registry, sha256(raw).hexdigest(), {}, issues)
    required = set(required_skill_names(config))
    registered = set(registry['skills'])
    for name in sorted(required - registered):
        issues.append(('ERROR', '.project/skills.yaml', f'missing required Skill: {name}'))
    records = _inspect_registered_skills(root, registry, issues)
    return SkillLayer(True, registry, sha256(raw).hexdigest(), records, issues)


def load_selected_skills(root, names):
    names = list(dict.fromkeys(names or []))
    config = load_yaml(Path(root) / 'project.yaml')
    layer = inspect_skill_layer(root, config)
    fatal = [issue for issue in layer.issues if issue[0] in {'BLOCKING', 'ERROR'}]
    if fatal:
        rendered = '; '.join(f'{location}: {message}' for _, location, message in fatal)
        raise SkillError(f'Skills layer is invalid: {rendered}')
    if not layer.active:
        if names:
            raise SkillError('legacy project has no active Skills layer')
        return layer, {}
    missing = sorted(set(names) - set(layer.records))
    if missing:
        raise SkillError(f'unknown or unavailable project Skills: {missing}')
    return layer, {name: layer.records[name] for name in names}


def skill_evidence(root, names, task_paths, governance_paths=None):
    layer, records = load_selected_skills(root, names)
    effective, authorizations = effective_write_scope(records, task_paths, governance_paths)
    selected = [
        {
            'name': record.name,
            'path': record.path.relative_to(Path(root).resolve()).as_posix(),
            'sha256': record.sha256,
        }
        for record in records.values()
    ]
    return {
        'selected_skills': selected,
        'skills_registry_sha256': layer.registry_sha256,
        'effective_write_scope': effective,
        'skill_write_authorizations': authorizations,
    }, records


def _validated_evidence_scope(value, label):
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise SkillError(f'{label} must be a list of strings')
    if len(value) != len(set(value)) or value != sorted(value):
        raise SkillError(f'{label} must be sorted and contain unique paths')
    for item in value:
        try:
            _normalize_write_pattern(item)
        except SkillError as exc:
            raise SkillError(f'{label} contains an invalid path: {item!r}') from exc
    return value


def _verify_skill_evidence(root, document, *, skills_required=False):
    if not isinstance(document, dict):
        raise SkillError('Skills evidence container must be a mapping/object')
    fields = {
        'selected_skills', 'skills_registry_sha256',
        'task_write_scope', 'effective_write_scope', 'skill_write_authorizations',
    }
    present = fields & set(document)
    if not present:
        if skills_required:
            raise SkillError('Skills-era base project is missing Skills evidence')
        return  # Genuinely legacy v0.11 artifact.
    if present != fields:
        raise SkillError('Skills evidence is incomplete')
    allowed_write_set = _validated_evidence_scope(
        document.get('allowed_write_set'), 'allowed_write_set',
    )
    pack_id = document.get('pack_id')
    if not isinstance(pack_id, str) or not pack_id:
        raise SkillError('pack_id is invalid for Skills evidence')
    task_scope = document.get('task_write_scope')
    if not isinstance(task_scope, dict) or set(task_scope) != {'canonical', 'derived'}:
        raise SkillError('task_write_scope must contain only canonical and derived')
    task_canonical = _validated_evidence_scope(
        task_scope.get('canonical'), 'task_write_scope.canonical',
    )
    expected_derived = [f'.generated/sync/{pack_id}/**']
    if task_canonical != allowed_write_set or task_scope.get('derived') != expected_derived:
        raise SkillError('task_write_scope is inconsistent with the SYNC plan')

    selected = document.get('selected_skills')
    if not isinstance(selected, list) or not selected:
        raise SkillError('selected_skills must be a non-empty list')
    names = []
    for item in selected:
        if not isinstance(item, dict) or set(item) != {'name', 'path', 'sha256'}:
            raise SkillError('selected_skills entry is malformed')
        name = item.get('name')
        path = item.get('path')
        digest = item.get('sha256')
        if not isinstance(name, str) or not SKILL_NAME_RE.fullmatch(name):
            raise SkillError('selected_skills entry name is invalid')
        if not isinstance(path, str) or path != f'.agents/skills/{name}/SKILL.md':
            raise SkillError('selected_skills entry path is invalid')
        if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
            raise SkillError('selected_skills entry sha256 is invalid')
        names.append(name)
    if len(names) != len(set(names)):
        raise SkillError('selected_skills contains duplicate names')

    registry_digest = document.get('skills_registry_sha256')
    if not isinstance(registry_digest, str) or not SHA256_RE.fullmatch(registry_digest):
        raise SkillError('skills_registry_sha256 is invalid')
    effective_scope = _validated_evidence_scope(
        document.get('effective_write_scope'), 'effective_write_scope',
    )
    authorizations = document.get('skill_write_authorizations')
    if not isinstance(authorizations, dict):
        raise SkillError('skill_write_authorizations must be a mapping/object')
    if set(authorizations) != set(effective_scope):
        raise SkillError('skill_write_authorizations paths do not match effective_write_scope')
    selected_names = set(names)
    for path, authorized_names in authorizations.items():
        if not isinstance(path, str):
            raise SkillError('skill_write_authorizations path is invalid')
        if (
            not isinstance(authorized_names, list)
            or not authorized_names
            or any(not isinstance(name, str) for name in authorized_names)
            or len(authorized_names) != len(set(authorized_names))
            or authorized_names != sorted(authorized_names)
            or not set(authorized_names) <= selected_names
        ):
            raise SkillError(f'skill_write_authorizations entry is invalid: {path!r}')

    evidence, _ = skill_evidence(root, names, allowed_write_set)
    if evidence['selected_skills'] != selected:
        raise SkillError('project Skill content or identity changed after planning')
    if evidence['skills_registry_sha256'] != registry_digest:
        raise SkillError('Skills registry changed after planning')
    if evidence['effective_write_scope'] != effective_scope:
        raise SkillError('effective_write_scope is inconsistent with current Skills')
    if evidence['skill_write_authorizations'] != authorizations:
        raise SkillError('skill_write_authorizations are inconsistent with current Skills')
    uncovered = sorted(
        set(allowed_write_set) - set(effective_scope)
    )
    if uncovered:
        raise SkillError('canonical allowed paths lack Skill authorization: ' + ', '.join(uncovered))


def verify_skill_evidence(root, document, *, skills_required=False):
    try:
        _verify_skill_evidence(root, document, skills_required=skills_required)
    except SkillError:
        raise
    except (TypeError, KeyError, AttributeError) as exc:
        raise SkillError('Skills evidence is malformed') from exc


def default_skill_names_for_target(data, *, mode='review', sync=False, config=None):
    names = ['knowledge-sync']
    object_type = data.get('type') if isinstance(data, dict) else None
    if object_type in {'decision', 'question'}:
        names.append('decision-management')
    if object_type in {'requirement', 'feature'}:
        names.append('requirements-management')
    if object_type in {'screen', 'flow', 'design_change'} and design_integration_enabled(config):
        names.append('design-handoff')
    if isinstance(data, dict) and data.get('domain') in {'architecture', 'technical', 'security'}:
        names.append('architecture-impact')
    if mode == 'implement':
        names.append('implementation-plan')
    return list(dict.fromkeys(names))


def sync_skill_names(planned_changes, project_config):
    names = ['knowledge-sync']
    for change in planned_changes:
        object_data = change.get('object') or {}
        object_type = object_data.get('type') or change.get('object_type')
        if object_type in {'decision', 'question'}:
            names.append('decision-management')
        if object_type in {'requirement', 'feature'}:
            names.append('requirements-management')
        if object_type in {'screen', 'flow', 'design_change'}:
            names.append('design-handoff')
        domain = object_data.get('domain') or change.get('domain')
        if domain in {'architecture', 'technical', 'security'}:
            names.append('architecture-impact')
        for path in change.get('narrative_paths', change.get('paths', [])):
            if path == 'docs/04_ARCHITECTURE.md' or path.startswith('docs/technical/'):
                names.append('architecture-impact')
            if path.startswith('docs/design/'):
                names.append('design-handoff')
    if DESIGN_SKILL in names and not design_integration_enabled(project_config):
        names.remove(DESIGN_SKILL)
    return list(dict.fromkeys(names))


def _git_clean(root):
    from .process_runner import run_process
    try:
        result = run_process(
            ['git', 'status', '--porcelain=v1', '--untracked-files=all'],
            cwd=root, text=True, capture_output=True, check=False,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    dirty = [
        line[3:].replace('\\', '/') for line in result.stdout.splitlines()
        if len(line) >= 4 and not (
            line[3:] == '.generated' or line[3:].replace('\\', '/').startswith('.generated/')
        )
    ]
    return not dirty


def _dump_registry(document):
    return yaml.safe_dump(document, sort_keys=False, allow_unicode=True)


def _desired_registry(config, existing=None):
    catalog = load_packaged_catalog()
    selected = required_skill_names(config)
    skills = {}
    if isinstance(existing, dict):
        skills.update(existing.get('skills') or {})
    for name in selected:
        skills.setdefault(name, catalog['skills'][name])
    return {
        'schema_version': SKILLS_SCHEMA_VERSION,
        'profile': SKILLS_PROFILE,
        'skills': skills,
    }


def materialize_skill_layer(root, config):
    """Materialize a fresh project layer exclusively from packaged assets."""
    root = Path(root).resolve()
    registry = _desired_registry(config)
    for name in required_skill_names(config):
        source = distribution_root() / 'skills' / name / 'SKILL.md'
        target = root / '.agents' / 'skills' / name / 'SKILL.md'
        _safe_existing_path(root, target, f'new project Skill {name}')
        target.parent.mkdir(parents=True, exist_ok=True)
        _safe_existing_path(root, target.parent, f'new project Skill directory {name}')
        atomic_write_text(target, source.read_text(encoding='utf-8'))
    registry_path = root / '.project' / 'skills.yaml'
    _safe_existing_path(root, registry_path, 'new project Skills registry')
    atomic_write_text(registry_path, _dump_registry(registry))


def _planned_template_update(root, relative, packaged_relative, legacy_hash, changes, conflicts):
    target = root / relative
    source = distribution_root() / packaged_relative
    desired = source.read_bytes()
    if not target.exists():
        changes[relative] = desired
        return
    if not target.is_file() or _path_has_reparse(target):
        conflicts.append(f'{relative}: target is not a safe regular file')
        return
    current = target.read_bytes()
    if current == desired:
        return
    if sha256(current).hexdigest() == legacy_hash:
        changes[relative] = desired
        return
    conflicts.append(f'{relative}: custom content requires manual merge')


def plan_skill_install(root):
    root = Path(root).resolve()
    config_path = root / 'project.yaml'
    _safe_existing_path(root, config_path, 'project.yaml')
    if not config_path.is_file():
        raise SkillError('project.yaml is missing or is not a regular file')
    config = load_yaml(config_path)
    if not isinstance(config, dict):
        raise SkillError('project.yaml top-level must be a mapping/object')
    existing_registry = None
    registry_path = root / '.project' / 'skills.yaml'
    if registry_path.exists():
        try:
            _safe_existing_path(root, registry_path, 'Skills registry')
            existing_registry = _strict_yaml_bytes(registry_path.read_bytes(), 'Skills registry')
        except (OSError, SkillError) as exc:
            raise SkillError(f'cannot migrate invalid Skills registry: {exc}') from exc
        messages = _schema_messages(existing_registry)
        if messages:
            raise SkillError('cannot migrate invalid Skills registry: ' + '; '.join(messages))
    desired_registry = _desired_registry(config, existing_registry)
    required = required_skill_names(config)
    changes = {}
    conflicts = []
    catalog_root = distribution_root() / 'skills'
    for name in required:
        target = root / '.agents' / 'skills' / name / 'SKILL.md'
        source = catalog_root / name / 'SKILL.md'
        relative = target.relative_to(root).as_posix()
        if target.exists():
            if not target.is_file() or _path_has_reparse(target):
                conflicts.append(f'{relative}: target is not a safe regular file')
            continue  # Installed/custom Skills are validated, never overwritten.
        changes[relative] = source.read_bytes()
    desired_registry_bytes = _dump_registry(desired_registry).encode('utf-8')
    if not registry_path.exists() or registry_path.read_bytes() != desired_registry_bytes:
        changes['.project/skills.yaml'] = desired_registry_bytes
    tooling = config.setdefault('tooling', {})
    if not isinstance(tooling, dict):
        raise SkillError('project.yaml tooling must be a mapping/object')
    tooling_changed = False
    target_cli_version = skills_era_project_cli_version(tooling.get('project_cli'))
    if tooling.get('project_cli') != target_cli_version:
        tooling['project_cli'] = target_cli_version
        tooling_changed = True
    if type(tooling.get('skills_schema_version')) is not int or tooling.get('skills_schema_version') != SKILLS_SCHEMA_VERSION:
        tooling['skills_schema_version'] = SKILLS_SCHEMA_VERSION
        tooling_changed = True
    if tooling_changed:
        changes['project.yaml'] = yaml.safe_dump(
            config, sort_keys=False, allow_unicode=True,
        ).encode('utf-8')
    _planned_template_update(
        root, 'AGENTS.md', 'templates/AGENTS.md', _V011_AGENTS_SHA256, changes, conflicts,
    )
    _planned_template_update(
        root, 'PROJECT_RULES.md', 'templates/PROJECT_RULES.md', _V011_RULES_SHA256, changes, conflicts,
    )
    clean = _git_clean(root)
    return {
        'schema_version': 1,
        'mode': 'plan',
        'required_skills': list(required),
        'writes': sorted(changes),
        'conflicts': conflicts,
        'git_clean': clean,
        '_payloads': changes,
    }


def install_skills(root, *, apply=False):
    root = Path(root).resolve()
    report = plan_skill_install(root)
    payloads = report.pop('_payloads')
    if not apply:
        report['status'] = 'conflict' if report['conflicts'] else 'ready'
        return report
    if report['conflicts']:
        raise SkillError('Skills install requires manual merge: ' + '; '.join(report['conflicts']))
    if report['git_clean'] is False:
        raise SkillError('Skills install --apply requires a clean Git worktree outside .generated/**')
    if report['git_clean'] is not True:
        raise SkillError('Skills install --apply cannot prove Git worktree cleanliness')
    snapshots = {}
    written = []
    created_dirs = set()
    try:
        # Publish the durable Skills-era config only after every layer file.
        ordered_payloads = sorted(payloads, key=lambda item: (item == 'project.yaml', item))
        for relative in ordered_payloads:
            target = root.joinpath(*PurePosixPath(relative).parts)
            _safe_existing_path(root, target, f'install target {relative}')
            snapshots[relative] = target.read_bytes() if target.exists() else None
            current = target.parent
            while current != root and not current.exists():
                created_dirs.add(current)
                current = current.parent
            target.parent.mkdir(parents=True, exist_ok=True)
            _safe_existing_path(root, target.parent, f'install parent {relative}')
            atomic_write_text(target, payloads[relative].decode('utf-8'))
            written.append(relative)
        final = inspect_skill_layer(root, load_yaml(root / 'project.yaml'))
        fatal = [issue for issue in final.issues if issue[0] in {'BLOCKING', 'ERROR'}]
        if fatal:
            raise SkillError('installed Skills layer failed validation: ' + '; '.join(
                f'{location}: {message}' for _, location, message in fatal
            ))
    except Exception:
        for relative in reversed(written):
            target = root.joinpath(*PurePosixPath(relative).parts)
            previous = snapshots[relative]
            if previous is None:
                try:
                    target.unlink()
                except FileNotFoundError:
                    pass
            else:
                atomic_write_text(target, previous.decode('utf-8'))
        for directory in sorted(created_dirs, key=lambda item: len(item.parts), reverse=True):
            try:
                directory.rmdir()
            except (FileNotFoundError, OSError):
                pass
        raise
    report['mode'] = 'apply'
    report['status'] = 'installed' if written else 'already_installed'
    report['writes'] = written
    return report
