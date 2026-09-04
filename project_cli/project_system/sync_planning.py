from collections import Counter
from hashlib import sha256
import json
from pathlib import Path, PurePosixPath
import subprocess

from jsonschema import Draft202012Validator, FormatChecker
import yaml

from .frontmatter import StrictSafeLoader
from .graph import extract_refs
from .ids import PREFIX
from .impact import impact, impact_docs_for_data
from .object_loader import TYPE_DIRECTORIES, load_object_layer
from .schemas import validate_object_schema
from .utils import ID_RE, atomic_write_text, distribution_root, load_yaml
from .validation import validate


MAX_SYNC_PACK_BYTES = 2 * 1024 * 1024
PACK_SUFFIXES = {'.yaml', '.yml', '.json'}
CANONICAL_CHANGE_KINDS = {
    'create_object',
    'update_object',
    'retire_object',
    'narrative_impact',
}
NON_CANONICAL_CHANGE_KINDS = {'proposal', 'unresolved'}
PROTECTED_PATHS = [
    '.git/**',
    '.github/**',
    '.project/**',
    '.generated/** (except this plan output)',
    'history/**',
    'project.yaml',
]
OUT_OF_SCOPE_PATHS = [
    '**/* except allowed_write_set',
    'knowledge/** except resolved object targets',
    'docs/** except resolved narrative targets',
]
_COMMON_CHANGE_FIELDS = {'change_id', 'kind', 'summary', 'related_ids'}
_CHANGE_FIELDS = {
    'create_object': _COMMON_CHANGE_FIELDS | {'object'},
    'update_object': _COMMON_CHANGE_FIELDS | {'target_id', 'patch'},
    'retire_object': _COMMON_CHANGE_FIELDS | {'target_id', 'new_status', 'replacement_id'},
    'narrative_impact': _COMMON_CHANGE_FIELDS | {'narrative_paths'},
    'proposal': _COMMON_CHANGE_FIELDS | {'proposal'},
    'unresolved': _COMMON_CHANGE_FIELDS | {'proposal'},
}


class SyncPlanError(RuntimeError):
    pass


class SyncPackLoader(StrictSafeLoader):
    pass


# YAML timestamps are data in the versioned contract. Keep them as strings so
# JSON Schema format validation is identical for YAML and JSON packs.
SyncPackLoader.add_constructor(
    'tag:yaml.org,2002:timestamp',
    lambda loader, node: loader.construct_scalar(node),
)


def _reject_yaml_aliases(text):
    for token in yaml.scan(text, Loader=yaml.SafeLoader):
        if isinstance(token, (yaml.tokens.AnchorToken, yaml.tokens.AliasToken)):
            raise SyncPlanError('YAML anchors and aliases are not allowed in a SYNC PACK')


def _load_pack_text(path):
    path = Path(path)
    if not path.is_file():
        raise SyncPlanError(f'SYNC PACK not found: {path}')
    if path.suffix.lower() not in PACK_SUFFIXES:
        raise SyncPlanError('SYNC PACK must use .yaml, .yml, or .json')
    size = path.stat().st_size
    if size > MAX_SYNC_PACK_BYTES:
        raise SyncPlanError(f'SYNC PACK exceeds {MAX_SYNC_PACK_BYTES} bytes')
    try:
        raw = path.read_bytes()
        text = raw.decode('utf-8')
    except UnicodeDecodeError as exc:
        raise SyncPlanError('SYNC PACK must be UTF-8') from exc
    try:
        _reject_yaml_aliases(text)
        pack = yaml.load(text, Loader=SyncPackLoader)
    except SyncPlanError:
        raise
    except Exception as exc:
        raise SyncPlanError(f'cannot parse SYNC PACK: {exc}') from exc
    if not isinstance(pack, dict):
        raise SyncPlanError('SYNC PACK root must be a mapping/object')
    return pack, text, raw


def _validate_pack_schema(pack):
    schema = json.loads(
        (distribution_root() / 'schemas' / 'sync-pack.schema.json').read_text(encoding='utf-8')
    )
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(pack), key=lambda error: list(error.path))
    if errors:
        rendered = []
        for error in errors:
            location = '.'.join(str(part) for part in error.path) or '<root>'
            rendered.append(f'{location}: {error.message}')
        raise SyncPlanError('schema validation failed: ' + '; '.join(rendered))


def _git_head(root):
    result = subprocess.run(
        ['git', 'rev-parse', 'HEAD'],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise SyncPlanError('cannot resolve Git HEAD')
    return result.stdout.strip().lower()


def _display_path(root, path):
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def _safe_repo_path(root, value, allowed_roots):
    if not isinstance(value, str) or not value.strip():
        raise SyncPlanError('target path must be a non-empty string')
    normalized_input = value.replace('\\', '/')
    pure = PurePosixPath(normalized_input)
    if pure.is_absolute() or not pure.parts or any(part in {'', '.', '..'} for part in pure.parts):
        raise SyncPlanError(f'unsafe target path: {value}')
    if ':' in pure.parts[0] or pure.parts[0] not in allowed_roots:
        raise SyncPlanError(f'target path is outside allowed roots {sorted(allowed_roots)}: {value}')
    candidate = (root / Path(*pure.parts)).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as exc:
        raise SyncPlanError(f'target path escapes project root: {value}') from exc
    return pure.as_posix(), candidate


def _narrative_path(root, value, must_exist=True):
    normalized, candidate = _safe_repo_path(root, value, {'docs'})
    if candidate.suffix.lower() != '.md':
        raise SyncPlanError(f'narrative target must be a Markdown file: {value}')
    if must_exist and not candidate.is_file():
        raise SyncPlanError(f'narrative target does not exist: {normalized}')
    return normalized, candidate


def _change_shape(change):
    kind = change['kind']
    allowed = _CHANGE_FIELDS[kind]
    extra = sorted(set(change) - allowed)
    if extra:
        raise SyncPlanError(
            f'change {change["change_id"]} has fields not valid for {kind}: {extra}'
        )


def _scan_duplicate_pack_id(root, pack_path, pack_id):
    inbox = root / 'inbox' / 'sync'
    if not inbox.exists():
        return
    current = pack_path.resolve()
    duplicates = []
    for candidate in sorted(path for path in inbox.rglob('*') if path.is_file()):
        if candidate.suffix.lower() not in PACK_SUFFIXES or candidate.resolve() == current:
            continue
        other, _, _ = _load_pack_text(candidate)
        if other.get('pack_id') == pack_id:
            duplicates.append(candidate.relative_to(root).as_posix())
    if duplicates:
        raise SyncPlanError(f'duplicate pack_id {pack_id}: {duplicates}')


def _safe_output_dir(root, pack_id):
    generated = root / '.generated'
    if generated.is_symlink():
        raise SyncPlanError('refusing to write through a .generated symlink')
    sync_root = generated / 'sync'
    if sync_root.is_symlink():
        raise SyncPlanError('refusing to write through a .generated/sync symlink')
    output = sync_root / pack_id
    if output.is_symlink():
        raise SyncPlanError(f'refusing to write through output symlink: {output}')
    if output.parent.resolve() != sync_root.resolve():
        raise SyncPlanError('unsafe SYNC plan output path')
    return output


def _write_output(output, name, text):
    target = output / name
    if target.parent.resolve() != output.resolve():
        raise SyncPlanError(f'unsafe generated output path: {target}')
    atomic_write_text(target, text)


def _context_text(pack, pack_text, manifest, root, object_paths, narrative_paths):
    lines = [
        '# SYNC Plan Context',
        '',
        '> Generated planning context. This file is not canonical product truth.',
        '',
        f"- Pack: `{pack['pack_id']}`",
        f"- Project: `{pack['project_id']}`",
        f"- Base commit: `{pack['base_commit']}`",
        f"- Pack SHA-256: `{manifest['pack_content_sha256']}`",
        f"- Change class: `{pack['change_class']}`",
        '',
        '## Allowed Write Set',
        '',
    ]
    lines.extend(f'- `{path}`' for path in manifest['allowed_write_set'])
    if not manifest['allowed_write_set']:
        lines.append('- _None. Proposals and unresolved items do not authorize canonical writes._')
    lines.extend(['', '## Unresolved / Proposal Items', ''])
    if manifest['unresolved_proposal_items']:
        for item in manifest['unresolved_proposal_items']:
            lines.append(f"- `{item['change_id']}` ({item['kind']}): {item['proposal']}")
    else:
        lines.append('- None.')
    lines.extend(['', '## Input SYNC PACK', '', '```yaml', pack_text.rstrip(), '```'])
    for object_id, path in sorted(object_paths.items()):
        lines.extend([
            '',
            f'## Existing Object `{object_id}`',
            '',
            f'Path: `{path.relative_to(root).as_posix()}`',
            '',
            path.read_text(encoding='utf-8').rstrip(),
        ])
    for path in sorted(narrative_paths):
        lines.extend([
            '',
            f'## Narrative Context `{path.relative_to(root).as_posix()}`',
            '',
            path.read_text(encoding='utf-8').rstrip(),
        ])
    return '\n'.join(lines).rstrip() + '\n'


def plan_sync(root, pack_path):
    """Validate a SYNC PACK and emit a deterministic, non-mutating plan."""
    root = Path(root).resolve()
    supplied_path = Path(pack_path)
    pack_path = (Path.cwd() / supplied_path).resolve() if not supplied_path.is_absolute() else supplied_path.resolve()
    pack, pack_text, pack_bytes = _load_pack_text(pack_path)
    _validate_pack_schema(pack)

    change_ids = [change['change_id'] for change in pack['changes']]
    duplicates = sorted(change_id for change_id, count in Counter(change_ids).items() if count > 1)
    if duplicates:
        raise SyncPlanError(f'duplicate change_id values: {duplicates}')
    for change in pack['changes']:
        _change_shape(change)

    config = load_yaml(root / 'project.yaml')
    actual_project_id = config.get('project', {}).get('id')
    if pack['project_id'] != actual_project_id:
        raise SyncPlanError(
            f'wrong project: pack targets {pack["project_id"]}, current project is {actual_project_id}'
        )
    head = _git_head(root)
    if pack['base_commit'].lower() != head:
        raise SyncPlanError(f'stale base_commit: pack has {pack["base_commit"]}, HEAD is {head}')
    _scan_duplicate_pack_id(root, pack_path, pack['pack_id'])

    project_issues = validate(root)
    fatal_project_issues = [
        issue for issue in project_issues if issue[0] in {'BLOCKING', 'ERROR'}
    ]
    if fatal_project_issues:
        rendered = '; '.join(f'{severity} {location}: {message}' for severity, location, message in fatal_project_issues)
        raise SyncPlanError(f'canonical project validation failed: {rendered}')

    layer = load_object_layer(root)
    if layer.errors or layer.unsupported_paths:
        raise SyncPlanError('canonical object layer has unreadable or unsupported objects')
    internal_counts = Counter(record.data.get('id') for record in layer.records)
    duplicate_object_ids = sorted(object_id for object_id, count in internal_counts.items() if object_id and count > 1)
    if duplicate_object_ids:
        raise SyncPlanError(f'canonical object layer has duplicate IDs: {duplicate_object_ids}')
    objects = layer.objects

    create_ids = [
        change['object']['id'] for change in pack['changes'] if change['kind'] == 'create_object'
    ]
    duplicate_create_ids = sorted(object_id for object_id, count in Counter(create_ids).items() if count > 1)
    if duplicate_create_ids:
        raise SyncPlanError(f'duplicate create object IDs: {duplicate_create_ids}')
    create_id_set = set(create_ids)

    resolved_targets = []
    planned_changes = []
    unresolved_items = []
    direct_targets = set()
    allowed_write_set = set()
    impacted_docs = set()
    object_context_paths = {}
    narrative_context_paths = set()
    warnings = [
        'Approval metadata is structurally validated; the local CLI does not prove approver identity.'
    ]
    warnings.extend(
        f'canonical project warning at {location}: {message}'
        for severity, location, message in project_issues
        if severity == 'WARNING'
    )

    def require_resolved_refs(change_id, data):
        for referenced_id in extract_refs(data):
            if referenced_id not in objects and referenced_id not in create_id_set:
                raise SyncPlanError(
                    f'change {change_id} references missing object ID: {referenced_id}'
                )

    def add_impact_docs(data):
        for candidate in impact_docs_for_data(root, data):
            normalized, path = _narrative_path(root, candidate, must_exist=False)
            if path.is_file():
                impacted_docs.add(normalized)
                allowed_write_set.add(normalized)
                narrative_context_paths.add(path)
            else:
                warnings.append(f'impact policy references missing narrative doc: {normalized}')

    for change in pack['changes']:
        change_id = change['change_id']
        kind = change['kind']
        for related_id in change.get('related_ids', []):
            if related_id not in objects:
                raise SyncPlanError(f'change {change_id} references missing related ID: {related_id}')

        planned = {
            'change_id': change_id,
            'kind': kind,
            'summary': change['summary'],
            'canonical_write': kind in CANONICAL_CHANGE_KINDS,
        }
        if change.get('related_ids'):
            planned['related_ids'] = change['related_ids']

        if kind == 'create_object':
            new_object = change['object']
            object_id = new_object['id']
            object_type = new_object['type']
            if object_id in objects:
                raise SyncPlanError(f'create target already exists: {object_id}')
            expected_prefix = PREFIX[object_type] + '-'
            if not object_id.startswith(expected_prefix):
                raise SyncPlanError(
                    f'create object type/ID mismatch: {object_type} cannot use {object_id}'
                )
            slug = new_object.get('slug')
            filename = f'{object_id}-{slug}.md' if slug else f'{object_id}.md'
            relative = f'knowledge/{TYPE_DIRECTORIES[object_type]}/{filename}'
            normalized, target_path = _safe_repo_path(root, relative, {'knowledge'})
            if target_path.exists():
                raise SyncPlanError(f'create target path already exists: {normalized}')
            direct_targets.add(object_id)
            allowed_write_set.add(normalized)
            resolved_targets.append({
                'change_id': change_id,
                'kind': kind,
                'object_id': object_id,
                'path': normalized,
                'exists': False,
            })
            planned.update({'object_id': object_id, 'path': normalized, 'object': new_object})
            proposed_data = dict(new_object.get('frontmatter', {}))
            for field in ('id', 'type', 'title', 'domain', 'status'):
                if field in proposed_data and field in new_object and proposed_data[field] != new_object[field]:
                    raise SyncPlanError(
                        f'change {change_id} has conflicting object.{field} and object.frontmatter.{field}'
                    )
            proposed_data.update({
                'id': object_id,
                'type': object_type,
                'title': new_object['title'],
            })
            if 'domain' in new_object:
                proposed_data['domain'] = new_object['domain']
            if 'status' in new_object:
                proposed_data['status'] = new_object['status']
            require_resolved_refs(change_id, proposed_data)
            add_impact_docs(proposed_data)

        elif kind in {'update_object', 'retire_object'}:
            target_id = change['target_id']
            if target_id not in objects:
                raise SyncPlanError(f'change {change_id} targets missing object ID: {target_id}')
            if kind == 'retire_object' and change.get('replacement_id'):
                replacement_id = change['replacement_id']
                if replacement_id not in objects and replacement_id not in create_id_set:
                    raise SyncPlanError(
                        f'change {change_id} references missing replacement ID: {replacement_id}'
                    )
            target_path = objects[target_id]['path']
            candidate_data = dict(objects[target_id]['data'])
            if kind == 'update_object':
                patch_frontmatter = change['patch'].get('frontmatter', {})
                if patch_frontmatter.get('id', target_id) != target_id:
                    raise SyncPlanError(f'change {change_id} cannot change canonical object identity')
                candidate_data.update(patch_frontmatter)
            else:
                candidate_data['status'] = change['new_status']
                if candidate_data.get('type') == 'requirement' and change['new_status'] == 'deprecated':
                    candidate_data['deprecation_reason'] = change['summary']
                    if change.get('replacement_id'):
                        candidate_data['deprecated_by'] = change['replacement_id']
            require_resolved_refs(change_id, candidate_data)
            schema_errors = validate_object_schema(candidate_data)
            if schema_errors:
                raise SyncPlanError(
                    f'change {change_id} would violate the target object schema: {schema_errors}'
                )
            normalized = target_path.relative_to(root).as_posix()
            direct_targets.add(target_id)
            allowed_write_set.add(normalized)
            object_context_paths[target_id] = target_path
            resolved_targets.append({
                'change_id': change_id,
                'kind': kind,
                'object_id': target_id,
                'path': normalized,
                'exists': True,
            })
            planned.update({'object_id': target_id, 'path': normalized})
            if kind == 'update_object':
                planned['patch'] = change['patch']
            else:
                planned['new_status'] = change['new_status']
                if change.get('replacement_id'):
                    planned['replacement_id'] = change['replacement_id']
            target_impact = impact(root, target_id)
            add_impact_docs(objects[target_id]['data'])
            if target_impact['affected_objects']:
                planned['affected_objects'] = target_impact['affected_objects']

        elif kind == 'narrative_impact':
            paths = []
            for value in change['narrative_paths']:
                normalized, target_path = _narrative_path(root, value)
                direct_targets.add(normalized)
                allowed_write_set.add(normalized)
                narrative_context_paths.add(target_path)
                paths.append(normalized)
                resolved_targets.append({
                    'change_id': change_id,
                    'kind': kind,
                    'path': normalized,
                    'exists': True,
                })
            planned['paths'] = paths

        elif kind in NON_CANONICAL_CHANGE_KINDS:
            unresolved = {
                'change_id': change_id,
                'kind': kind,
                'summary': change['summary'],
                'proposal': change['proposal'],
                'related_ids': change.get('related_ids', []),
            }
            unresolved_items.append(unresolved)
            planned['proposal'] = change['proposal']

        planned_changes.append(planned)

    normalized_expected = set()
    for value in pack['expected_targets']:
        if ID_RE.fullmatch(value):
            normalized_expected.add(value)
        else:
            normalized, _ = _narrative_path(root, value)
            normalized_expected.add(normalized)
    if len(normalized_expected) != len(pack['expected_targets']):
        raise SyncPlanError('expected_targets contains duplicate normalized targets')
    if normalized_expected != direct_targets:
        missing = sorted(direct_targets - normalized_expected)
        unexpected = sorted(normalized_expected - direct_targets)
        raise SyncPlanError(
            f'expected_targets mismatch: missing={missing}, unexpected={unexpected}'
        )

    content_hash = sha256(pack_bytes).hexdigest()
    output = _safe_output_dir(root, pack['pack_id'])
    existing_manifest_path = output / 'manifest.json'
    if existing_manifest_path.exists():
        try:
            existing_manifest = json.loads(existing_manifest_path.read_text(encoding='utf-8'))
        except Exception as exc:
            raise SyncPlanError(f'cannot read existing plan manifest: {exc}') from exc
        if existing_manifest.get('pack_content_sha256') != content_hash:
            raise SyncPlanError(
                f'pack_id {pack["pack_id"]} was already planned with different content'
            )

    manifest = {
        'schema_version': 1,
        'pack_id': pack['pack_id'],
        'project_id': pack['project_id'],
        'pack_path': _display_path(root, pack_path),
        'pack_content_sha256': content_hash,
        'base_commit': head,
        'approval': pack['approval'],
        'change_class': pack['change_class'],
        'resolved_targets': resolved_targets,
        'allowed_write_set': sorted(allowed_write_set),
        'protected_paths': PROTECTED_PATHS,
        'out_of_scope_paths': OUT_OF_SCOPE_PATHS,
        'impacted_narrative_docs': sorted(impacted_docs),
        'unresolved_proposal_items': unresolved_items,
        'warnings': list(dict.fromkeys(warnings)),
        'errors': [],
    }
    plan = {
        'schema_version': 1,
        'pack_id': pack['pack_id'],
        'project_id': pack['project_id'],
        'base_commit': head,
        'pack_content_sha256': content_hash,
        'changes': planned_changes,
        'resolved_targets': resolved_targets,
        'impacted_narrative_docs': sorted(impacted_docs),
        'allowed_write_set': sorted(allowed_write_set),
        'unresolved_proposal_items': unresolved_items,
    }
    context = _context_text(
        pack,
        pack_text,
        manifest,
        root,
        object_context_paths,
        narrative_context_paths,
    )
    output.mkdir(parents=True, exist_ok=True)
    _write_output(output, 'plan.json', json.dumps(plan, indent=2, sort_keys=True, ensure_ascii=False) + '\n')
    _write_output(output, 'manifest.json', json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + '\n')
    _write_output(output, 'context.md', context)
    return output, manifest
