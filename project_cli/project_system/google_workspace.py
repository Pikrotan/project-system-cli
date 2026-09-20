"""Universal Google Workspace / Designer Bridge orchestration."""
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path

from . import __version__
from .google_api import (
    DOC_MIME, FOLDER_MIME, SHEET_MIME, GoogleApiError, GoogleApiGateway,
)
from .google_bindings import (
    GoogleBindingError, design_imports, load_workspace_binding,
    write_workspace_binding,
)
from .google_credentials import (
    GoogleAuthRequired, GoogleCredentialManager, GoogleError,
)
from .google_design import (
    DESIGNER_COLUMNS, DESIGN_CHANGE_HEADERS, process_design_changes,
)
from .google_projection import (
    GoogleProjectionError, build_projection, content_sha256,
)
from .sync_bindings import validate_transactions
from .sync_intake import SyncIntakeError, _safe_local_path, intake_bindings
from .sync_planning import _git_head, _git_status_paths
from .sync_pull import github_repository
from .utils import atomic_write_text, load_yaml


RESOURCE_SPECS = {
    'project_overview': (DOC_MIME, 'Обзор проекта'),
    'design_knowledge': (DOC_MIME, 'Design Knowledge'),
    'design_changes': (SHEET_MIME, 'Design Changes'),
}


class GoogleWorkspaceError(GoogleError):
    category = 'google_workspace'


class GoogleWorkspaceConfigError(GoogleWorkspaceError):
    category = 'google_config'


class GoogleWorkspaceConflictError(GoogleWorkspaceError):
    category = 'google_binding_integrity'


def _iso(value=None):
    value = value or datetime.now(timezone.utc)
    if value.tzinfo is None or value.utcoffset() is None:
        raise GoogleWorkspaceError('Google workspace clock must include a timezone')
    return value.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def google_policy(root, *, require_enabled=True):
    config = load_yaml(Path(root) / 'project.yaml')
    project = config.get('project') or {}
    policy = ((config.get('external_systems') or {}).get('google_workspace'))
    if not isinstance(policy, dict):
        if require_enabled:
            raise GoogleWorkspaceConfigError(
                'Google Workspace is not configured in external_systems.google_workspace'
            )
        return project, None
    if require_enabled and policy.get('enabled') is not True:
        raise GoogleWorkspaceConfigError('Google Workspace is disabled for this project')
    if policy.get('enabled') not in {True, False}:
        raise GoogleWorkspaceConfigError('Google Workspace enabled must be boolean')
    allowed = {
        'enabled', 'project_overview', 'design_knowledge', 'design_changes',
        'projection_drift',
    }
    if set(policy) - allowed:
        raise GoogleWorkspaceConfigError('Google Workspace configuration has unsupported fields')
    normalized = {
        'enabled': policy['enabled'],
        'project_overview': policy.get('project_overview', True),
        'design_knowledge': policy.get('design_knowledge', True),
        'design_changes': policy.get('design_changes', True),
        'projection_drift': policy.get('projection_drift', 'restore'),
    }
    if any(type(normalized[key]) is not bool for key in (
        'project_overview', 'design_knowledge', 'design_changes',
    )):
        raise GoogleWorkspaceConfigError('Google Workspace resource flags must be boolean')
    if normalized['projection_drift'] != 'restore':
        raise GoogleWorkspaceConfigError('unsupported projection drift policy')
    return project, normalized


def _identity(root):
    project, policy = google_policy(root)
    project_id = project.get('id')
    project_name = project.get('name')
    if not isinstance(project_id, str) or not project_id:
        raise GoogleWorkspaceConfigError('project.id is required for Google Workspace')
    if not isinstance(project_name, str) or not project_name:
        raise GoogleWorkspaceConfigError('project.name is required for Google Workspace')
    return project_id, project_name, github_repository(root), policy


def _gateway(manager=None, gateway=None, *, background=False):
    if gateway is not None:
        return gateway
    manager = manager or GoogleCredentialManager()
    return GoogleApiGateway(manager.credentials(background=background))


def _metadata(resource):
    value = resource.get('appProperties')
    return value if isinstance(value, dict) else {}


def _verify_resource(resource, *, project_id, repository, role, mime_type, parent=None):
    if not isinstance(resource, dict) or resource.get('trashed') is True:
        raise GoogleWorkspaceConflictError(f'Google resource {role} is missing or trashed')
    if resource.get('mimeType') != mime_type:
        raise GoogleWorkspaceConflictError(f'Google resource {role} has the wrong MIME type')
    metadata = _metadata(resource)
    expected = {
        'project_system_project_id': project_id,
        'project_system_repository': repository.lower(),
        'project_system_resource_role': role,
    }
    if any(metadata.get(key) != value for key, value in expected.items()):
        raise GoogleWorkspaceConflictError(f'Google resource {role} metadata mismatch')
    if parent is not None and parent not in (resource.get('parents') or []):
        raise GoogleWorkspaceConflictError(f'Google resource {role} is outside the bound workspace folder')
    resource_id = resource.get('id')
    if not isinstance(resource_id, str) or not resource_id:
        raise GoogleWorkspaceConflictError(f'Google resource {role} has no stable ID')
    return resource


def _record(resource, role, fallback_time):
    return {
        'id': resource['id'],
        'role': role,
        'name': resource.get('name') or role,
        'mime_type': resource['mimeType'],
        'created_at': resource.get('createdTime') or fallback_time,
    }


def _validate_binding_identity(binding, project_id, repository):
    if binding is None:
        raise GoogleWorkspaceConflictError('Google workspace is not bound; run workspace init or rebind')
    if (binding.get('project_id') != project_id
            or binding.get('repository', '').lower() != repository.lower()):
        raise GoogleWorkspaceConflictError('Google workspace binding belongs to another project/repository')


def _validate_binding_policy(binding, policy):
    expected = {role for role in RESOURCE_SPECS if policy[role]}
    if set(binding.get('resources', {})) != expected:
        raise GoogleWorkspaceConflictError(
            'Google workspace binding resources differ from current project configuration; rebind required'
        )


def _remote_binding_check(binding, gateway):
    folder_record = binding['folder']
    folder = _verify_resource(
        gateway.get_resource(folder_record['id']),
        project_id=binding['project_id'], repository=binding['repository'],
        role='workspace_root', mime_type=FOLDER_MIME,
    )
    result = {'workspace_root': folder}
    for role, record in binding['resources'].items():
        mime_type = RESOURCE_SPECS[role][0]
        result[role] = _verify_resource(
            gateway.get_resource(record['id']),
            project_id=binding['project_id'], repository=binding['repository'],
            role=role, mime_type=mime_type, parent=folder_record['id'],
        )
    return result


def _new_binding(project_id, repository, folder, resources, timestamp):
    return {
        'schema_version': 1,
        'record_type': 'google_workspace_binding',
        'project_id': project_id,
        'repository': repository,
        'bound_at': timestamp,
        'cli_version': __version__,
        'folder': _record(folder, 'workspace_root', timestamp),
        'resources': {
            role: _record(resource, role, timestamp)
            for role, resource in sorted(resources.items())
        },
        'projections': {},
    }


def _projection_sync(root, binding, gateway, *, clock=None):
    clock = clock or (lambda: datetime.now(timezone.utc))
    result = {}
    updated = deepcopy(binding)
    head = _git_head(root)
    for role in ('project_overview', 'design_knowledge'):
        resource = binding['resources'].get(role)
        if resource is None:
            continue
        previous = binding.get('projections', {}).get(role)
        timestamp = previous['projected_at'] if previous else _iso(clock())
        desired = build_projection(
            root, role, projected_at=timestamp, source_commit=head,
        )
        source_changed = (
            previous is None
            or previous['source_sha256'] != desired['source_sha256']
            or previous['source_commit'] != head
        )
        if source_changed and previous is not None:
            desired = build_projection(
                root, role, projected_at=_iso(clock()), source_commit=head,
            )
        actual = gateway.read_document_text(resource['id'])
        actual_hash = content_sha256(actual)
        drift = previous is not None and actual_hash != previous['content_sha256']
        needs_write = actual_hash != desired['content_sha256']
        if needs_write:
            gateway.replace_document_text(resource['id'], desired['text'])
            verified = gateway.read_document_text(resource['id'])
            if content_sha256(verified) != desired['content_sha256']:
                raise GoogleWorkspaceConflictError(f'Google projection {role} failed reread verification')
        updated.setdefault('projections', {})[role] = {
            key: desired[key] for key in (
                'source_commit', 'source_sha256', 'content_sha256', 'projected_at',
            )
        }
        result[role] = {
            'status': (
                'drift_restored' if drift and not source_changed
                else 'updated' if needs_write else 'current'
            ),
            'source_changed': source_changed,
            'manual_drift': drift,
        }
    return updated, result


def _generated_paths(root):
    try:
        paths = tuple(
            _safe_local_path(Path(root).resolve(), relative)
            for relative in ('.generated/google/state.json', '.generated/google/events.jsonl')
        )
    except SyncIntakeError as exc:
        raise GoogleWorkspaceConflictError('Google generated output path is unsafe') from exc
    paths[0].parent.mkdir(parents=True, exist_ok=True)
    for path in paths:
        if path.exists() and (not path.is_file() or path.is_symlink()
                              or getattr(path, 'is_junction', lambda: False)()):
            raise GoogleWorkspaceConflictError('Google generated output target is unsafe')
    return paths


def _record_operation(root, report, *, clock=None):
    clock = clock or (lambda: datetime.now(timezone.utc))
    state_path, events_path = _generated_paths(root)
    safe = {
        'schema_version': 1,
        'updated_at': _iso(clock()),
        'status': report.get('status'),
        'projection_statuses': {
            key: value.get('status') for key, value in (report.get('projections') or {}).items()
        },
        'design_changes': report.get('design_changes'),
        'error_category': report.get('error_category'),
    }
    atomic_write_text(state_path, json.dumps(safe, indent=2, sort_keys=True) + '\n')
    event = dict(safe, event='google_workspace_cycle')
    with events_path.open('ab', buffering=0) as stream:
        raw = (json.dumps(event, sort_keys=True, separators=(',', ':')) + '\n').encode('utf-8')
        stream.write(raw)


def _clean_preflight(root):
    dirty = sorted(
        path for path in _git_status_paths(root)
        if path != '.generated' and not path.startswith('.generated/')
    )
    if dirty:
        raise GoogleWorkspaceConflictError('Google cycle requires a clean canonical worktree')
    if validate_transactions(root):
        raise GoogleWorkspaceConflictError('Google cycle is blocked by a terminal transaction')
    active = [binding for binding in intake_bindings(root) if binding.state == 'active']
    if active:
        raise GoogleWorkspaceConflictError('Google cycle is blocked by an active or awaiting-push SYNC pack')


def initialize_workspace(root, *, manager=None, gateway=None, clock=None):
    root = Path(root).resolve()
    clock = clock or (lambda: datetime.now(timezone.utc))
    project_id, name, repository, policy = _identity(root)
    _clean_preflight(root)
    if load_workspace_binding(root) is not None:
        raise GoogleWorkspaceConflictError('Google workspace is already bound')
    api = _gateway(manager, gateway)
    existing = api.find_project_resources(project_id, repository)
    if existing:
        raise GoogleWorkspaceConflictError('matching Google resources already exist; use workspace rebind')
    timestamp = _iso(clock())
    folder = api.create_resource(
        f'{name} — Project Workspace', FOLDER_MIME, 'workspace_root',
        project_id, repository,
    )
    resources = {}
    for role, (mime_type, suffix) in RESOURCE_SPECS.items():
        if policy[role]:
            resources[role] = api.create_resource(
                f'{name} — {suffix}', mime_type, role, project_id, repository,
                parent=folder['id'],
            )
    if 'design_changes' in resources:
        api.initialize_design_sheet(
            resources['design_changes']['id'], DESIGN_CHANGE_HEADERS,
            len(DESIGNER_COLUMNS),
        )
    binding = write_workspace_binding(
        root, _new_binding(project_id, repository, folder, resources, timestamp),
    )
    binding, projections = _projection_sync(root, binding, api, clock=clock)
    binding = write_workspace_binding(root, binding)
    report = {
        'status': 'initialized', 'project_id': project_id,
        'repository': repository, 'folder_id': folder['id'],
        'resources': {role: item['id'] for role, item in resources.items()},
        'projections': projections,
    }
    _record_operation(root, report, clock=clock)
    return report


def workspace_status(root, *, manager=None, gateway=None):
    root = Path(root).resolve()
    project_id, name, repository, policy = _identity(root)
    binding = load_workspace_binding(root)
    if binding is None:
        return {
            'status': 'not_bound', 'project_id': project_id,
            'repository': repository, 'resources': {}, 'warnings': [],
        }
    _validate_binding_identity(binding, project_id, repository)
    _validate_binding_policy(binding, policy)
    api = _gateway(manager, gateway)
    remote = _remote_binding_check(binding, api)
    projections = {}
    dirty = sorted(
        path for path in _git_status_paths(root)
        if path != '.generated' and not path.startswith('.generated/')
    )
    for role, metadata in binding.get('projections', {}).items():
        actual = api.read_document_text(binding['resources'][role]['id'])
        manual_drift = content_sha256(actual) != metadata['content_sha256']
        source_stale = None
        if not dirty:
            desired = build_projection(
                root, role, projected_at=metadata['projected_at'],
            )
            source_stale = (
                desired['source_commit'] != metadata['source_commit']
                or desired['source_sha256'] != metadata['source_sha256']
            )
        projections[role] = {
            'status': (
                'manual_drift_and_stale_source' if manual_drift and source_stale
                else 'manual_drift' if manual_drift
                else 'stale_source' if source_stale
                else 'current' if source_stale is False
                else 'source_freshness_unknown'
            ),
            'manual_drift': manual_drift,
            'source_stale': source_stale,
            'source_commit': metadata['source_commit'],
            'projected_at': metadata['projected_at'],
        }
    warnings = ([] if binding['cli_version'] == __version__ else [
        f'binding CLI {binding["cli_version"]} differs from runtime {__version__}',
    ])
    if dirty:
        warnings.append('canonical worktree is dirty; projection source freshness was not evaluated')
    return {
        'status': 'bound', 'project_id': project_id, 'repository': repository,
        'folder_id': binding['folder']['id'],
        'resources': {role: item['id'] for role, item in binding['resources'].items()},
        'projections': projections, 'import_count': len(design_imports(root)),
        'warnings': warnings,
    }


def rebind_workspace(root, *, manager=None, gateway=None, clock=None):
    root = Path(root).resolve()
    clock = clock or (lambda: datetime.now(timezone.utc))
    project_id, name, repository, policy = _identity(root)
    api = _gateway(manager, gateway)
    found = api.find_project_resources(project_id, repository)
    roles = {}
    for resource in found:
        role = _metadata(resource).get('project_system_resource_role')
        if role not in {'workspace_root', *RESOURCE_SPECS}:
            raise GoogleWorkspaceConflictError('Google rebind found a resource with an unexpected role')
        roles.setdefault(role, []).append(resource)
    required = ['workspace_root'] + [role for role in RESOURCE_SPECS if policy[role]]
    for role in required:
        if len(roles.get(role, [])) != 1:
            raise GoogleWorkspaceConflictError(
                f'Google rebind requires exactly one resource for role {role}'
            )
    if any(role not in required and values for role, values in roles.items()):
        raise GoogleWorkspaceConflictError('Google rebind found disabled or unexpected project resources')
    folder = _verify_resource(
        roles['workspace_root'][0], project_id=project_id, repository=repository,
        role='workspace_root', mime_type=FOLDER_MIME,
    )
    resources = {}
    for role in required[1:]:
        resources[role] = _verify_resource(
            roles[role][0], project_id=project_id, repository=repository,
            role=role, mime_type=RESOURCE_SPECS[role][0], parent=folder['id'],
        )
    old = load_workspace_binding(root)
    fresh = _new_binding(project_id, repository, folder, resources, _iso(clock()))
    if old is not None and old.get('resources') == fresh['resources'] and old.get('folder') == fresh['folder']:
        fresh['projections'] = deepcopy(old.get('projections', {}))
        fresh['bound_at'] = old['bound_at']
    write_workspace_binding(root, fresh)
    return {
        'status': 'rebound', 'project_id': project_id, 'repository': repository,
        'folder_id': folder['id'],
        'resources': {role: item['id'] for role, item in resources.items()},
    }


def sync_workspace(root, *, manager=None, gateway=None, clock=None, background=False):
    root = Path(root).resolve()
    clock = clock or (lambda: datetime.now(timezone.utc))
    project_id, name, repository, policy = _identity(root)
    _clean_preflight(root)
    binding = load_workspace_binding(root)
    _validate_binding_identity(binding, project_id, repository)
    _validate_binding_policy(binding, policy)
    api = _gateway(manager, gateway, background=background)
    _remote_binding_check(binding, api)
    updated, projections = _projection_sync(root, binding, api, clock=clock)
    if updated != binding:
        updated = write_workspace_binding(root, updated)
    design = process_design_changes(root, updated, api, clock=clock) if policy['design_changes'] else {
        'status': 'disabled', 'imported': 0, 'reused': 0, 'invalid': 0, 'conflicts': 0,
    }
    if design.get('conflicts'):
        raise GoogleWorkspaceConflictError('Design Changes contains an immutable import conflict')
    changed = any(item['status'] != 'current' for item in projections.values()) or any(
        design.get(key, 0) for key in ('imported', 'invalid', 'conflicts')
    )
    report = {
        'status': 'processed' if changed else 'no_changes',
        'project_id': project_id, 'repository': repository,
        'projections': projections, 'design_changes': design,
    }
    _record_operation(root, report, clock=clock)
    return report


def google_auto_cycle(root, *, manager=None, gateway=None, clock=None):
    """Headless Google branch for the existing one-shot automatic runtime."""
    try:
        return sync_workspace(
            root, manager=manager, gateway=gateway, clock=clock, background=True,
        )
    except GoogleAuthRequired:
        report = {'status': 'google_auth_required', 'error_category': 'google_auth_required'}
    except GoogleBindingError:
        report = {'status': 'google_binding_integrity', 'error_category': 'google_binding_integrity'}
    except GoogleApiError as exc:
        report = {'status': exc.category, 'error_category': exc.category}
    except (GoogleWorkspaceError, GoogleProjectionError) as exc:
        report = {'status': exc.category, 'error_category': exc.category}
    except GoogleError as exc:
        report = {'status': exc.category, 'error_category': exc.category}
    try:
        _record_operation(root, report, clock=clock)
    except Exception:
        pass
    return report
