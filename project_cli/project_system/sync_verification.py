from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import subprocess

import yaml

from .frontmatter import StrictSafeLoader, _reject_aliases, read_object
from .generation import GenerationBlockedError, generate
from .graph import extract_refs
from .object_loader import load_object_layer
from .sync_planning import (
    PACK_ID_RE,
    PACK_SUFFIXES,
    SyncPlanError,
    _git_head,
    _load_pack_text,
    _safe_output_dir,
    _safe_repo_path,
    _validate_pack_schema,
    _write_output,
    artifact_payload_sha256,
)
from .utils import load_yaml
from .validation import counts, validate


INTEGRITY_EXIT = 3
SCOPE_EXIT = 4
VALIDATION_EXIT = 5
MAX_PLAN_ARTIFACT_BYTES = 4 * 1024 * 1024
TERMINAL_OBJECT_STATUSES = {'superseded', 'deprecated', 'rejected', 'removed'}


class SyncVerifyError(RuntimeError):
    exit_code = 2
    category = 'verification'


class SyncIntegrityError(SyncVerifyError):
    exit_code = INTEGRITY_EXIT
    category = 'integrity'


class SyncScopeError(SyncVerifyError):
    exit_code = SCOPE_EXIT
    category = 'scope'


class SyncValidationError(SyncVerifyError):
    exit_code = VALIDATION_EXIT
    category = 'validation'


def _git(root, args, *, text=False, allow_failure=False):
    result = subprocess.run(
        ['git', *args],
        cwd=root,
        capture_output=True,
        text=text,
        check=False,
    )
    if result.returncode and not allow_failure:
        stderr = result.stderr.strip() if text else result.stderr.decode('utf-8', errors='replace').strip()
        raise SyncIntegrityError(f'Git command failed: {stderr or "unknown Git error"}')
    return result


def _safe_git_path(root, value):
    if not isinstance(value, str) or not value:
        raise SyncScopeError('Git reported an empty or non-string path')
    normalized = value.replace('\\', '/')
    pure = PurePosixPath(normalized)
    if pure.is_absolute() or not pure.parts or any(part in {'', '.', '..'} for part in pure.parts):
        raise SyncScopeError(f'unsafe changed path: {value}')
    if ':' in pure.parts[0]:
        raise SyncScopeError(f'unsafe changed path: {value}')
    candidate = root.joinpath(*pure.parts)
    try:
        candidate.resolve(strict=False).relative_to(root.resolve())
    except ValueError as exc:
        raise SyncScopeError(f'changed path escapes project root: {value}') from exc
    if candidate.is_symlink():
        raise SyncScopeError(f'changed path is a symlink: {normalized}')
    return pure.as_posix()


def _parse_name_status(root, args):
    result = _git(root, args)
    tokens = result.stdout.decode('utf-8', errors='strict').split('\0')
    records = []
    index = 0
    while index < len(tokens):
        status = tokens[index]
        index += 1
        if not status:
            continue
        code = status[0]
        if code in {'R', 'C'}:
            if index + 1 >= len(tokens):
                raise SyncIntegrityError('cannot parse renamed/copied Git record')
            old_path = _safe_git_path(root, tokens[index])
            new_path = _safe_git_path(root, tokens[index + 1])
            index += 2
            records.append({
                'status': code,
                'status_detail': status,
                'old_path': old_path,
                'new_path': new_path,
            })
        else:
            if index >= len(tokens):
                raise SyncIntegrityError('cannot parse Git change record')
            path = _safe_git_path(root, tokens[index])
            index += 1
            records.append({'status': code, 'status_detail': status, 'path': path})
    return records


def _record_paths(record):
    if record['status'] in {'R', 'C'}:
        return [record['old_path'], record['new_path']]
    return [record['path']]


def collect_git_changes(root, base_commit):
    root = Path(root).resolve()
    tracked = _parse_name_status(
        root,
        ['diff', '--no-ext-diff', '--name-status', '-z', '--find-renames', base_commit, '--'],
    )
    staged = _parse_name_status(
        root,
        ['diff', '--cached', '--no-ext-diff', '--name-status', '-z', '--find-renames', base_commit, '--'],
    )
    unstaged = _parse_name_status(
        root,
        ['diff', '--no-ext-diff', '--name-status', '-z', '--find-renames', '--'],
    )
    untracked_result = _git(root, ['ls-files', '--others', '--exclude-standard', '-z'])
    untracked = sorted(
        _safe_git_path(root, value)
        for value in untracked_result.stdout.decode('utf-8', errors='strict').split('\0')
        if value
    )
    ignored_result = _git(
        root,
        ['ls-files', '--others', '--ignored', '--exclude-standard', '-z'],
    )
    ignored_untracked = sorted(
        _safe_git_path(root, value)
        for value in ignored_result.stdout.decode('utf-8', errors='strict').split('\0')
        if value
    )
    all_records = tracked + staged + unstaged
    deleted = sorted({
        path
        for record in all_records
        if record['status'] == 'D'
        for path in _record_paths(record)
    })
    renamed = []
    seen_renames = set()
    for record in all_records:
        if record['status'] != 'R':
            continue
        key = (record['old_path'], record['new_path'])
        if key not in seen_renames:
            seen_renames.add(key)
            renamed.append({
                'status': 'R',
                'old_path': record['old_path'],
                'new_path': record['new_path'],
                'status_detail': record['status_detail'],
            })
    return {
        'tracked': tracked,
        'staged': staged,
        'unstaged': unstaged,
        'untracked': untracked,
        'ignored_untracked': ignored_untracked,
        'deleted': deleted,
        'renamed': renamed,
    }


def _all_changed_paths(changes):
    paths = set(changes['untracked'])
    for layer in ('tracked', 'staged', 'unstaged'):
        for record in changes[layer]:
            paths.update(_record_paths(record))
    return paths


def _is_generated(path):
    return path == '.generated' or path.startswith('.generated/')


def _repo_relative(root, path):
    try:
        return Path(path).resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return None


def _read_json_artifact(output, name):
    if output.is_symlink():
        raise SyncIntegrityError(f'refusing to read through SYNC output symlink: {output}')
    path = output / name
    if path.is_symlink():
        raise SyncIntegrityError(f'refusing to read through SYNC artifact symlink: {path}')
    if path.parent.resolve() != output.resolve() or not path.is_file():
        raise SyncIntegrityError(f'missing SYNC artifact: {path}')
    if path.stat().st_size > MAX_PLAN_ARTIFACT_BYTES:
        raise SyncIntegrityError(f'SYNC artifact exceeds {MAX_PLAN_ARTIFACT_BYTES} bytes: {name}')
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except Exception as exc:
        raise SyncIntegrityError(f'cannot parse {name}: {exc}') from exc
    if not isinstance(data, dict):
        raise SyncIntegrityError(f'{name} root must be an object')
    return data


def _verify_artifact_pair(plan, manifest, expected_pack_id):
    if plan.get('schema_version') != 1 or manifest.get('schema_version') != 1:
        raise SyncIntegrityError('unsupported plan/manifest schema version')
    plan_integrity = plan.get('artifact_integrity')
    manifest_integrity = manifest.get('artifact_integrity')
    if not isinstance(plan_integrity, dict) or plan_integrity != manifest_integrity:
        raise SyncIntegrityError('plan/manifest artifact integrity metadata is missing or inconsistent')
    if set(plan_integrity) != {
        'algorithm',
        'plan_payload_sha256',
        'manifest_payload_sha256',
    } or plan_integrity.get('algorithm') != 'sha256':
        raise SyncIntegrityError('unsupported plan/manifest integrity contract')
    expected_plan_hash = artifact_payload_sha256(plan)
    expected_manifest_hash = artifact_payload_sha256(manifest)
    if plan_integrity.get('plan_payload_sha256') != expected_plan_hash:
        raise SyncIntegrityError('plan.json fingerprint mismatch; artifact may be tampered')
    if manifest_integrity.get('manifest_payload_sha256') != expected_manifest_hash:
        raise SyncIntegrityError('manifest.json fingerprint mismatch; artifact may be tampered')

    shared_fields = [
        'schema_version',
        'pack_id',
        'project_id',
        'pack_content_sha256',
        'base_commit',
        'resolved_targets',
        'allowed_write_set',
        'impacted_narrative_docs',
        'unresolved_proposal_items',
        'ignored_untracked_baseline',
    ]
    for field in shared_fields:
        if plan.get(field) != manifest.get(field):
            raise SyncIntegrityError(f'plan/manifest mismatch for {field}')
    if plan.get('pack_id') != expected_pack_id:
        raise SyncIntegrityError(
            f'plan/manifest pack ID mismatch: expected {expected_pack_id}, found {plan.get("pack_id")}'
        )
    allowed = plan.get('allowed_write_set')
    if not isinstance(allowed, list) or not all(isinstance(path, str) for path in allowed):
        raise SyncIntegrityError('allowed_write_set must be a list of paths')
    if len(allowed) != len(set(allowed)):
        raise SyncIntegrityError('allowed_write_set contains duplicate paths')
    ignored_baseline = plan.get('ignored_untracked_baseline')
    if not isinstance(ignored_baseline, list):
        raise SyncIntegrityError('ignored_untracked_baseline must be a list')
    ignored_paths = []
    for item in ignored_baseline:
        if not isinstance(item, dict) or set(item) != {'path', 'sha256', 'bytes'}:
            raise SyncIntegrityError('ignored_untracked_baseline entry is malformed')
        digest = item.get('sha256')
        if (
            not isinstance(item.get('path'), str)
            or not isinstance(item.get('bytes'), int)
            or item['bytes'] < 0
            or not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in '0123456789abcdef' for character in digest)
        ):
            raise SyncIntegrityError('ignored_untracked_baseline entry is malformed')
        ignored_paths.append(item['path'])
    if len(ignored_paths) != len(set(ignored_paths)) or ignored_paths != sorted(ignored_paths):
        raise SyncIntegrityError('ignored_untracked_baseline paths must be unique and sorted')
    resolved = plan.get('resolved_targets')
    impacted = plan.get('impacted_narrative_docs')
    if not isinstance(resolved, list) or not isinstance(impacted, list):
        raise SyncIntegrityError('plan target metadata is malformed')
    derived_allowed = {
        item.get('path')
        for item in resolved
        if isinstance(item, dict) and isinstance(item.get('path'), str)
    }
    derived_allowed.update(path for path in impacted if isinstance(path, str))
    if set(allowed) != derived_allowed:
        raise SyncIntegrityError('allowed_write_set is inconsistent with the validated plan targets')


def _selector_is_path(selector):
    value = str(selector)
    path = Path(value)
    return (
        path.is_absolute()
        or path.suffix.lower() in PACK_SUFFIXES
        or '/' in value
        or '\\' in value
    )


def _resolve_integrity_inputs(root, selector):
    root = Path(root).resolve()
    selector_value = str(selector)
    pack = None
    pack_text = None
    pack_bytes = None
    pack_path = None
    if _selector_is_path(selector_value):
        supplied = Path(selector_value)
        pack_path = (Path.cwd() / supplied).resolve() if not supplied.is_absolute() else supplied.resolve()
        try:
            pack, pack_text, pack_bytes = _load_pack_text(pack_path)
            _validate_pack_schema(pack)
        except SyncPlanError as exc:
            raise SyncIntegrityError(str(exc)) from exc
        pack_id = pack['pack_id']
    else:
        if not PACK_ID_RE.fullmatch(selector_value):
            raise SyncIntegrityError('verify selector must be a SYNC PACK path or a valid pack_id')
        pack_id = selector_value

    try:
        output = _safe_output_dir(root, pack_id)
    except SyncPlanError as exc:
        raise SyncIntegrityError(str(exc)) from exc
    plan = _read_json_artifact(output, 'plan.json')
    manifest = _read_json_artifact(output, 'manifest.json')
    _verify_artifact_pair(plan, manifest, pack_id)

    if pack_path is None:
        recorded_path = manifest.get('pack_path')
        if not isinstance(recorded_path, str) or not recorded_path:
            raise SyncIntegrityError('manifest does not contain the source pack path')
        recorded = Path(recorded_path)
        pack_path = (root / recorded).resolve() if not recorded.is_absolute() else recorded.resolve()
        try:
            pack, pack_text, pack_bytes = _load_pack_text(pack_path)
            _validate_pack_schema(pack)
        except SyncPlanError as exc:
            raise SyncIntegrityError(str(exc)) from exc

    pack_hash = sha256(pack_bytes).hexdigest()
    if pack_hash != plan.get('pack_content_sha256'):
        raise SyncIntegrityError('pack SHA-256 mismatch; source pack may be tampered')
    comparisons = {
        'pack_id': pack.get('pack_id'),
        'project_id': pack.get('project_id'),
        'base_commit': pack.get('base_commit'),
        'approval': pack.get('approval'),
    }
    for field, value in comparisons.items():
        planned_value = manifest.get(field)
        if field == 'approval':
            if value != planned_value:
                raise SyncIntegrityError('pack approval metadata differs from the planned manifest')
        elif value != planned_value:
            raise SyncIntegrityError(f'pack {field} differs from the planned manifest')

    try:
        config = load_yaml(root / 'project.yaml')
    except Exception as exc:
        raise SyncIntegrityError(f'cannot read project.yaml: {exc}') from exc
    project_id = config.get('project', {}).get('id')
    if pack['project_id'] != project_id:
        raise SyncIntegrityError(
            f'wrong project: pack targets {pack["project_id"]}, current project is {project_id}'
        )
    try:
        head = _git_head(root)
    except SyncPlanError as exc:
        raise SyncIntegrityError(str(exc)) from exc
    if head != plan['base_commit']:
        raise SyncIntegrityError(
            f'stale HEAD: plan base is {plan["base_commit"]}, current HEAD is {head}'
        )
    base_exists = _git(root, ['cat-file', '-e', f'{head}^{{commit}}'], allow_failure=True)
    if base_exists.returncode:
        raise SyncIntegrityError(f'base_commit is not a readable Git commit: {head}')

    allowed_paths = []
    for value in plan['allowed_write_set']:
        try:
            normalized, candidate = _safe_repo_path(root, value, {'knowledge', 'docs'})
        except SyncPlanError as exc:
            raise SyncIntegrityError(f'unsafe allowed write path: {exc}') from exc
        if candidate.is_symlink():
            raise SyncIntegrityError(f'allowed write path is a symlink: {normalized}')
        allowed_paths.append(normalized)
    if allowed_paths != plan['allowed_write_set']:
        raise SyncIntegrityError('allowed_write_set paths are not normalized')
    for item in plan['ignored_untracked_baseline']:
        try:
            normalized = _safe_git_path(root, item['path'])
        except SyncScopeError as exc:
            raise SyncIntegrityError(f'unsafe ignored baseline path: {exc}') from exc
        if normalized != item['path'] or _is_generated(normalized):
            raise SyncIntegrityError(f'unsafe ignored baseline path: {item["path"]}')

    return {
        'output': output,
        'pack': pack,
        'pack_text': pack_text,
        'pack_path': pack_path,
        'plan': plan,
        'manifest': manifest,
        'head': head,
    }


def _ignored_inventory(root, paths, pack_path):
    input_pack_path = _repo_relative(root, pack_path)
    inventory = {}
    for path in paths:
        if _is_generated(path) or path == input_pack_path:
            continue
        candidate = root.joinpath(*PurePosixPath(path).parts)
        if candidate.is_symlink():
            raise SyncScopeError(f'gitignored path is a symlink: {path}')
        if not candidate.is_file():
            raise SyncScopeError(f'gitignored path is not a regular file: {path}')
        content = candidate.read_bytes()
        inventory[path] = {
            'path': path,
            'sha256': sha256(content).hexdigest(),
            'bytes': len(content),
        }
    return inventory


def _scope_analysis(root, changes, allowed_write_set, pack_path, ignored_baseline):
    input_pack_path = _repo_relative(root, pack_path)
    changed_paths = _all_changed_paths(changes)
    generated_paths = sorted(
        {path for path in changed_paths if _is_generated(path)}
        | {path for path in changes['ignored_untracked'] if _is_generated(path)}
    )
    baseline_by_path = {
        item['path']: item
        for item in ignored_baseline
        if isinstance(item, dict) and isinstance(item.get('path'), str)
    }
    current_ignored = _ignored_inventory(root, changes['ignored_untracked'], pack_path)
    ignored_drift = sorted(
        path for path in set(baseline_by_path) | set(current_ignored)
        if baseline_by_path.get(path) != current_ignored.get(path)
    )
    changed_paths.update(ignored_drift)
    staged_paths = {
        path for record in changes['staged'] for path in _record_paths(record)
    }
    unstaged_paths = {
        path for record in changes['unstaged'] for path in _record_paths(record)
    }
    partially_staged = sorted(
        path for path in staged_paths & unstaged_paths
        if not _is_generated(path) and path != input_pack_path
    )
    input_paths = sorted(path for path in changed_paths if path == input_pack_path)
    actual_paths = sorted(
        path for path in changed_paths
        if not _is_generated(path) and path != input_pack_path
    )
    outside = sorted(set(actual_paths) - set(allowed_write_set))
    canonical = sorted(
        path for path in actual_paths
        if path == 'project.yaml'
        or path.startswith('knowledge/')
        or path.startswith('docs/')
        or path.startswith('.project/')
        or path.startswith('.github/')
    )
    return {
        'actual_changed_paths': actual_paths,
        'actual_changed_canonical_paths': canonical,
        'changes_outside_scope': outside,
        'generated_paths_ignored': ['.generated/**'] if generated_paths else [],
        'input_pack_paths_ignored': input_paths,
        'ignored_untracked_drift': ignored_drift,
        'partially_staged_paths': partially_staged,
    }


def _reportable_git_changes(changes, ignored_drift):
    def reportable_record(record):
        return not all(_is_generated(path) for path in _record_paths(record))

    return {
        'tracked': [record for record in changes['tracked'] if reportable_record(record)],
        'staged': [record for record in changes['staged'] if reportable_record(record)],
        'unstaged': [record for record in changes['unstaged'] if reportable_record(record)],
        'untracked': [path for path in changes['untracked'] if not _is_generated(path)],
        'ignored_untracked': [
            path for path in changes['ignored_untracked'] if path in ignored_drift
        ],
        'deleted': [path for path in changes['deleted'] if not _is_generated(path)],
        'renamed': [record for record in changes['renamed'] if reportable_record(record)],
    }


def _validation_summary(issues):
    return {
        'counts': counts(issues),
        'issues': [
            {'severity': severity, 'location': location, 'message': message}
            for severity, location, message in issues
        ],
    }


def _object_counts(root):
    by_type = dict(sorted(load_object_layer(root).counts_by_type().items()))
    return {'total': sum(by_type.values()), 'by_type': by_type}


def _snapshot_non_generated(root):
    root = Path(root).resolve()
    snapshot = {}
    for current, directories, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        relative_dir = current_path.relative_to(root)
        directories[:] = [
            name for name in directories
            if not (
                relative_dir == Path('.') and name in {'.git', '.generated'}
            )
        ]
        for name in sorted(files):
            path = current_path / name
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                snapshot[relative] = {'kind': 'symlink', 'target': os.readlink(path)}
            else:
                snapshot[relative] = {
                    'kind': 'file',
                    'sha256': sha256(path.read_bytes()).hexdigest(),
                }
    return snapshot


def _parse_object_text(text):
    normalized = text.replace('\r\n', '\n')
    if not normalized.startswith('---\n'):
        raise ValueError('missing YAML frontmatter')
    parts = normalized.split('---\n', 2)
    if len(parts) < 3:
        raise ValueError('unterminated YAML frontmatter')
    _reject_aliases(parts[1])
    data = yaml.load(parts[1], Loader=StrictSafeLoader) or {}
    if not isinstance(data, dict):
        raise ValueError('YAML frontmatter must be an object')
    return data


def _base_object_data(root, base_commit, path):
    result = _git(root, ['show', f'{base_commit}:{path}'], allow_failure=True)
    if result.returncode:
        return None
    return _parse_object_text(result.stdout.decode('utf-8', errors='strict'))


def _current_object_data(root, path):
    candidate = root.joinpath(*PurePosixPath(path).parts)
    if not candidate.is_file():
        return None
    data, _ = read_object(candidate)
    return data


def _atomic_lifecycle(root, base_commit, actual_paths):
    lifecycle = []
    warnings = []
    atomic_paths = sorted({
        path for path in actual_paths
        if path.startswith('knowledge/') and path.endswith('.md')
    })
    for path in atomic_paths:
        try:
            before = _base_object_data(root, base_commit, path)
            after = _current_object_data(root, path)
            if before is None and after is None:
                continue
            if before is None:
                action = 'created'
            elif after is None:
                action = 'deleted'
            elif before.get('status') != after.get('status') and after.get('status') in TERMINAL_OBJECT_STATUSES:
                action = 'retired'
            else:
                action = 'updated'
            before_refs = sorted(extract_refs(before)) if before else []
            after_refs = sorted(extract_refs(after)) if after else []
            lifecycle.append({
                'path': path,
                'action': action,
                'id': (after or before).get('id'),
                'type': (after or before).get('type'),
                'status_before': before.get('status') if before else None,
                'status_after': after.get('status') if after else None,
                'title_before': before.get('title') if before else None,
                'title_after': after.get('title') if after else None,
                'depends_on_before': sorted(before.get('depends_on') or []) if before else [],
                'depends_on_after': sorted(after.get('depends_on') or []) if after else [],
                'references_added': sorted(set(after_refs) - set(before_refs)),
                'references_removed': sorted(set(before_refs) - set(after_refs)),
            })
        except Exception as exc:
            warnings.append(f'cannot summarize atomic lifecycle for {path}: {exc}')
    return lifecycle, warnings


def _diff_stat(root, base_commit, changes, ignored_drift):
    working = _git(
        root,
        ['diff', '--no-ext-diff', '--no-color', '--stat', base_commit, '--'],
        text=True,
    )
    staged = _git(
        root,
        ['diff', '--cached', '--no-ext-diff', '--no-color', '--stat', base_commit, '--'],
        text=True,
    )
    unstaged = _git(
        root,
        ['diff', '--no-ext-diff', '--no-color', '--stat', '--'],
        text=True,
    )
    untracked = []
    for path in changes['untracked']:
        if _is_generated(path):
            continue
        candidate = root.joinpath(*PurePosixPath(path).parts)
        untracked.append({
            'path': path,
            'bytes': candidate.stat().st_size if candidate.is_file() else None,
            'ignored': False,
        })
    for path in ignored_drift:
        if _is_generated(path):
            continue
        candidate = root.joinpath(*PurePosixPath(path).parts)
        untracked.append({
            'path': path,
            'bytes': candidate.stat().st_size if candidate.is_file() else None,
            'ignored': True,
        })
    return {
        'tracked': working.stdout.strip(),
        'staged': staged.stdout.strip(),
        'unstaged': unstaged.stdout.strip(),
        'untracked': untracked,
    }


def _base_report(integrity, changes, scope):
    plan = integrity['plan']
    actual = scope['actual_changed_paths']
    lifecycle, lifecycle_warnings = _atomic_lifecycle(
        integrity['output'].parents[2],
        plan['base_commit'],
        actual,
    )
    warnings = list(lifecycle_warnings)
    unchanged_allowed = sorted(set(plan['allowed_write_set']) - set(actual))
    if unchanged_allowed:
        warnings.append(
            'allowed paths without a detected change: ' + ', '.join(unchanged_allowed)
        )
    return {
        'schema_version': 1,
        'pack_id': plan['pack_id'],
        'pack_content_sha256': plan['pack_content_sha256'],
        'base_commit': plan['base_commit'],
        'current_head': integrity['head'],
        'verification_result': 'pending',
        'deterministic_verification': True,
        'semantic_meaning_verified': False,
        'human_semantic_review_required': True,
        'allowed_write_set': plan['allowed_write_set'],
        **scope,
        'git_changes': _reportable_git_changes(
            changes,
            scope['ignored_untracked_drift'],
        ),
        'validation': {
            'before_generation': None,
            'after_generation': None,
            'generation_ran': False,
        },
        'object_counts': None,
        'diff_stat': None,
        'atomic_object_lifecycle': lifecycle,
        'changed_narrative_docs': sorted(
            path for path in actual if path.startswith('docs/') and path.endswith('.md')
        ),
        'unresolved_proposal_items': plan['unresolved_proposal_items'],
        'warnings': warnings,
        'errors': [],
    }


def _markdown_report(report):
    lines = [
        '# SYNC Verification',
        '',
        '> Deterministic integrity, Git scope, and schema verification only. Semantic meaning still requires human review.',
        '',
        f"- Pack: `{report['pack_id']}`",
        f"- Result: **{report['verification_result']}**",
        f"- Pack SHA-256: `{report['pack_content_sha256']}`",
        f"- Base commit: `{report['base_commit']}`",
        f"- Current HEAD: `{report['current_head']}`",
        '',
        '## Allowed Write Set',
        '',
    ]
    lines.extend(f'- `{path}`' for path in report['allowed_write_set'])
    if not report['allowed_write_set']:
        lines.append('- None.')
    lines.extend(['', '## Actual Canonical Changes', ''])
    lines.extend(f'- `{path}`' for path in report['actual_changed_canonical_paths'])
    if not report['actual_changed_canonical_paths']:
        lines.append('- None.')
    lines.extend(['', '## Outside Scope', ''])
    lines.extend(f'- `{path}`' for path in report['changes_outside_scope'])
    if not report['changes_outside_scope']:
        lines.append('- None.')
    lines.extend(['', '## Atomic Lifecycle', ''])
    for item in report['atomic_object_lifecycle']:
        lines.append(
            f"- `{item['id']}` — {item['action']}; status "
            f"`{item['status_before']}` → `{item['status_after']}`; "
            f"title `{item['title_before']}` → `{item['title_after']}`"
        )
    if not report['atomic_object_lifecycle']:
        lines.append('- None.')
    lines.extend(['', '## Proposal / Unresolved Reminders', ''])
    for item in report['unresolved_proposal_items']:
        lines.append(f"- `{item['change_id']}` ({item['kind']}): {item['proposal']}")
    if not report['unresolved_proposal_items']:
        lines.append('- None.')
    lines.extend(['', '## Validation', ''])
    for stage in ('before_generation', 'after_generation'):
        summary = report['validation'].get(stage)
        if summary is None:
            lines.append(f'- {stage}: not run')
        else:
            rendered = ', '.join(f'{key}={value}' for key, value in summary['counts'].items())
            lines.append(f'- {stage}: {rendered}')
    lines.extend(['', '## Warnings / Errors', ''])
    lines.extend(f'- Warning: {message}' for message in report['warnings'])
    lines.extend(f'- Error: {message}' for message in report['errors'])
    if not report['warnings'] and not report['errors']:
        lines.append('- None.')
    return '\n'.join(lines).rstrip() + '\n'


def _diff_markdown(report):
    lines = [
        '# SYNC Diff Summary',
        '',
        '> Generated deterministic Git summary. No semantic interpretation is performed.',
        '',
        '## Tracked Diff Stat',
        '',
        '```text',
        report['diff_stat']['tracked'] or '(no tracked changes)',
        '```',
        '',
        '## Staged Diff Stat',
        '',
        '```text',
        report['diff_stat']['staged'] or '(no staged changes)',
        '```',
        '',
        '## Unstaged Diff Stat',
        '',
        '```text',
        report['diff_stat']['unstaged'] or '(no unstaged changes)',
        '```',
        '',
        '## Untracked',
        '',
    ]
    for item in report['diff_stat']['untracked']:
        suffix = ' (gitignored)' if item['ignored'] else ''
        lines.append(f"- `{item['path']}` — {item['bytes']} bytes{suffix}")
    if not report['diff_stat']['untracked']:
        lines.append('- None.')
    lines.extend(['', '## Renamed', ''])
    for item in report['git_changes']['renamed']:
        lines.append(f"- `{item['old_path']}` → `{item['new_path']}` ({item['status_detail']})")
    if not report['git_changes']['renamed']:
        lines.append('- None.')
    lines.extend(['', '## Deleted', ''])
    lines.extend(f'- `{path}`' for path in report['git_changes']['deleted'])
    if not report['git_changes']['deleted']:
        lines.append('- None.')
    return '\n'.join(lines).rstrip() + '\n'


def _write_reports(output, report):
    try:
        _write_output(
            output,
            'verification.json',
            json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + '\n',
        )
        _write_output(output, 'verification.md', _markdown_report(report))
        _write_output(output, 'diff-summary.md', _diff_markdown(report))
    except SyncPlanError as exc:
        raise SyncIntegrityError(str(exc)) from exc


def _fatal_validation(issues):
    return [issue for issue in issues if issue[0] in {'BLOCKING', 'ERROR'}]


def verify_sync(root, selector):
    """Verify externally applied SYNC edits without making semantic decisions."""
    root = Path(root).resolve()
    integrity = _resolve_integrity_inputs(root, selector)
    plan = integrity['plan']

    changes_before = collect_git_changes(root, plan['base_commit'])
    scope_before = _scope_analysis(
        root,
        changes_before,
        plan['allowed_write_set'],
        integrity['pack_path'],
        plan['ignored_untracked_baseline'],
    )
    report = _base_report(integrity, changes_before, scope_before)
    report['object_counts'] = _object_counts(root)
    report['diff_stat'] = _diff_stat(
        root,
        plan['base_commit'],
        changes_before,
        scope_before['ignored_untracked_drift'],
    )
    if scope_before['changes_outside_scope'] or scope_before['partially_staged_paths']:
        report['verification_result'] = 'failed_scope'
        if scope_before['changes_outside_scope']:
            report['errors'].append(
                'changes outside allowed_write_set: '
                + ', '.join(scope_before['changes_outside_scope'])
            )
        if scope_before['partially_staged_paths']:
            report['errors'].append(
                'paths have divergent staged and unstaged content: '
                + ', '.join(scope_before['partially_staged_paths'])
            )
        _write_reports(integrity['output'], report)
        raise SyncScopeError(
            f'scope violation; see {integrity["output"] / "verification.json"}'
        )

    first_issues = validate(root)
    report['validation']['before_generation'] = _validation_summary(first_issues)
    if _fatal_validation(first_issues):
        report['verification_result'] = 'failed_validation'
        report['errors'].append('initial canonical validation contains BLOCKING/ERROR issues')
        _write_reports(integrity['output'], report)
        raise SyncValidationError(
            f'initial validation failed; see {integrity["output"] / "verification.json"}'
        )

    canonical_snapshot = _snapshot_non_generated(root)
    try:
        generate(root)
    except GenerationBlockedError as exc:
        report['verification_result'] = 'failed_validation'
        report['errors'].append(str(exc))
        _write_reports(integrity['output'], report)
        raise SyncValidationError(str(exc)) from exc
    report['validation']['generation_ran'] = True
    if canonical_snapshot != _snapshot_non_generated(root):
        report['verification_result'] = 'failed_scope'
        report['errors'].append('generation changed files outside .generated/**')
        _write_reports(integrity['output'], report)
        raise SyncScopeError('generation changed non-generated project files')

    second_issues = validate(root)
    report['validation']['after_generation'] = _validation_summary(second_issues)
    changes_after = collect_git_changes(root, plan['base_commit'])
    scope_after = _scope_analysis(
        root,
        changes_after,
        plan['allowed_write_set'],
        integrity['pack_path'],
        plan['ignored_untracked_baseline'],
    )
    if scope_after['changes_outside_scope'] or scope_after['partially_staged_paths']:
        report.update(scope_after)
        report['git_changes'] = _reportable_git_changes(
            changes_after,
            scope_after['ignored_untracked_drift'],
        )
        report['verification_result'] = 'failed_scope'
        if scope_after['changes_outside_scope']:
            report['errors'].append(
                'post-generation changes outside allowed_write_set: '
                + ', '.join(scope_after['changes_outside_scope'])
            )
        if scope_after['partially_staged_paths']:
            report['errors'].append(
                'post-generation paths have divergent staged and unstaged content: '
                + ', '.join(scope_after['partially_staged_paths'])
            )
        report['diff_stat'] = _diff_stat(
            root,
            plan['base_commit'],
            changes_after,
            scope_after['ignored_untracked_drift'],
        )
        _write_reports(integrity['output'], report)
        raise SyncScopeError('scope violation after generation')
    if _fatal_validation(second_issues):
        report['verification_result'] = 'failed_validation'
        report['errors'].append('post-generation validation contains BLOCKING/ERROR issues')
        _write_reports(integrity['output'], report)
        raise SyncValidationError('post-generation validation failed')

    report['verification_result'] = 'passed'
    report['object_counts'] = _object_counts(root)
    report['diff_stat'] = _diff_stat(
        root,
        plan['base_commit'],
        changes_after,
        scope_after['ignored_untracked_drift'],
    )
    _write_reports(integrity['output'], report)
    return integrity['output'], report
