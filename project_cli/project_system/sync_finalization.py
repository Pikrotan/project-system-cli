from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import uuid

from .process_runner import run_process
from .sync_planning import SyncPlanError, _write_output
from .sync_bindings import (
    DURABLE_PACK_ID,
    SyncBindingError,
    build_terminal_binding,
    load_sync_bindings,
    terminal_status,
    terminalize_binding,
)
from .sync_verification import (
    SyncVerifyError,
    _all_changed_paths,
    _is_generated,
    _parse_name_status,
    _read_json_artifact,
    _record_paths,
    _repo_relative,
    _resolve_integrity_inputs,
    _scope_analysis,
    collect_git_changes,
    verification_report_payload_sha256,
    verified_working_tree_state,
)


INTEGRITY_EXIT = 3
SCOPE_EXIT = 4
COMMIT_EXIT = 6
PUSH_EXIT = 7
COMMIT_SHA_RE = re.compile(r'^[0-9a-f]{40}$')
MAX_COMMIT_MESSAGE_CHARS = 4096
COMPLETE_OUTCOMES = {'reviewed-no-change', 'rejected', 'abandoned'}


class SyncFinalizeError(RuntimeError):
    exit_code = INTEGRITY_EXIT
    category = 'integrity'


class SyncFinalizeIntegrityError(SyncFinalizeError):
    pass


class SyncFinalizeScopeError(SyncFinalizeError):
    exit_code = SCOPE_EXIT
    category = 'scope'


class SyncCommitError(SyncFinalizeError):
    exit_code = COMMIT_EXIT
    category = 'commit'


class SyncPushError(SyncFinalizeError):
    exit_code = PUSH_EXIT
    category = 'push'


def _run_git(root, args, *, allow_failure=False, text=True):
    result = run_process(
        ['git', *args],
        cwd=root,
        capture_output=True,
        text=text,
        check=False,
    )
    if result.returncode and not allow_failure:
        stderr = result.stderr.strip() if text else result.stderr.decode('utf-8', errors='replace').strip()
        raise SyncFinalizeIntegrityError(
            f'Git command failed: {stderr or "unknown Git error"}'
        )
    return result


def _git_path(root, name):
    result = _run_git(root, ['rev-parse', '--git-path', name])
    value = result.stdout.strip()
    path = Path(value)
    return (root / path).resolve() if not path.is_absolute() else path.resolve()


def _operation_state(root):
    markers = {
        'merge': 'MERGE_HEAD',
        'rebase': 'rebase-merge',
        'rebase_apply': 'rebase-apply',
        'cherry_pick': 'CHERRY_PICK_HEAD',
    }
    return sorted(name for name, marker in markers.items() if _git_path(root, marker).exists())


def _git_preflight(root):
    inside = _run_git(root, ['rev-parse', '--is-inside-work-tree'], allow_failure=True)
    if inside.returncode or inside.stdout.strip() != 'true':
        raise SyncFinalizeIntegrityError('current project is not a Git working tree')
    branch_result = _run_git(
        root,
        ['symbolic-ref', '--quiet', '--short', 'HEAD'],
        allow_failure=True,
    )
    if branch_result.returncode or not branch_result.stdout.strip():
        raise SyncFinalizeScopeError('detached HEAD is not allowed for SYNC finalization')
    branch = branch_result.stdout.strip()
    operations = _operation_state(root)
    if operations:
        raise SyncFinalizeScopeError(
            'repository operation in progress: ' + ', '.join(operations)
        )
    conflicts = _run_git(
        root,
        ['diff', '--name-only', '--diff-filter=U', '-z'],
        text=False,
    ).stdout.decode('utf-8', errors='strict').split('\0')
    conflicts = sorted(path.replace('\\', '/') for path in conflicts if path)
    if conflicts:
        raise SyncFinalizeScopeError(
            'repository has unresolved conflicts: ' + ', '.join(conflicts)
        )

    upstream_result = _run_git(
        root,
        ['rev-parse', '--abbrev-ref', '--symbolic-full-name', '@{upstream}'],
        allow_failure=True,
    )
    upstream = upstream_result.stdout.strip() if not upstream_result.returncode else None
    remote_result = _run_git(
        root,
        ['config', '--get', f'branch.{branch}.remote'],
        allow_failure=True,
    )
    remote = remote_result.stdout.strip() if not remote_result.returncode else None
    remote_url = None
    if remote and remote != '.':
        url_result = _run_git(root, ['remote', 'get-url', remote], allow_failure=True)
        if not url_result.returncode:
            remote_url = url_result.stdout.strip() or None
    return {
        'branch': branch,
        'upstream': upstream,
        'remote': remote,
        'remote_url': remote_url,
        'operations_in_progress': [],
        'unresolved_conflicts': [],
    }


def _state_payload_sha256(payload):
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(',', ':'),
        ensure_ascii=False,
    ).encode('utf-8')
    return sha256(canonical).hexdigest()


def finalization_report_payload_sha256(report):
    payload = dict(report)
    payload.pop('finalization_integrity', None)
    return _state_payload_sha256(payload)


def _seal_finalization_report(report):
    report.pop('finalization_integrity', None)
    report['finalization_integrity'] = {
        'algorithm': 'sha256',
        'report_payload_sha256': finalization_report_payload_sha256(report),
    }


def _verify_sha256(value, label):
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in '0123456789abcdef' for character in value)
    ):
        raise SyncFinalizeIntegrityError(f'{label} must be a lowercase SHA-256 digest')


def _load_verification(output, plan):
    try:
        report = _read_json_artifact(output, 'verification.json')
    except SyncVerifyError as exc:
        raise SyncFinalizeIntegrityError(str(exc)) from exc
    integrity = report.get('verification_integrity')
    if not isinstance(integrity, dict) or set(integrity) != {
        'algorithm',
        'report_payload_sha256',
    }:
        raise SyncFinalizeIntegrityError('verification report integrity metadata is missing')
    if integrity.get('algorithm') != 'sha256':
        raise SyncFinalizeIntegrityError('unsupported verification integrity contract')
    expected = verification_report_payload_sha256(report)
    if integrity.get('report_payload_sha256') != expected:
        raise SyncFinalizeIntegrityError(
            'verification.json fingerprint mismatch; artifact may be tampered'
        )
    if report.get('verification_result') != 'passed':
        raise SyncFinalizeIntegrityError('successful sync verify is required before finalization')
    comparisons = {
        'pack_id': plan['pack_id'],
        'pack_content_sha256': plan['pack_content_sha256'],
        'base_commit': plan['base_commit'],
        'allowed_write_set': plan['allowed_write_set'],
    }
    for field in (
        'selected_skills',
        'skills_registry_sha256',
        'task_write_scope',
        'effective_write_scope',
        'skill_write_authorizations',
    ):
        if field in plan:
            comparisons[field] = plan[field]
    for field, expected_value in comparisons.items():
        if report.get(field) != expected_value:
            raise SyncFinalizeIntegrityError(
                f'verification report does not match plan field {field}'
            )
    if (
        report.get('deterministic_verification') is not True
        or report.get('semantic_meaning_verified') is not False
        or report.get('human_semantic_review_required') is not True
    ):
        raise SyncFinalizeIntegrityError('verification governance boundary is malformed')
    validation = report.get('validation')
    if not isinstance(validation, dict) or validation.get('generation_ran') is not True:
        raise SyncFinalizeIntegrityError('verification pipeline did not complete generation')
    for stage in ('before_generation', 'after_generation'):
        summary = validation.get(stage)
        counts = summary.get('counts') if isinstance(summary, dict) else None
        if not isinstance(counts, dict) or counts.get('BLOCKING') or counts.get('ERROR'):
            raise SyncFinalizeIntegrityError(
                f'verification {stage} is missing or contains blocking errors'
            )
    fingerprint = report.get('verification_fingerprint')
    _verify_sha256(fingerprint, 'verification_fingerprint')
    state = report.get('verified_working_tree_state')
    if not isinstance(state, dict) or _state_payload_sha256(state) != fingerprint:
        raise SyncFinalizeIntegrityError('verified working-tree state fingerprint is inconsistent')
    canonical_paths = report.get('actual_changed_canonical_paths')
    actual_paths = report.get('actual_changed_paths')
    if (
        not isinstance(canonical_paths, list)
        or canonical_paths != sorted(set(canonical_paths))
        or canonical_paths != actual_paths
    ):
        raise SyncFinalizeIntegrityError(
            'verification canonical change set is malformed or contains non-canonical paths'
        )
    return report


def _load_previous_finalization(output):
    path = output / 'finalization.json'
    if not path.exists():
        return None
    try:
        report = _read_json_artifact(output, 'finalization.json')
    except SyncVerifyError as exc:
        raise SyncFinalizeIntegrityError(str(exc)) from exc
    integrity = report.get('finalization_integrity')
    if not isinstance(integrity, dict) or set(integrity) != {
        'algorithm',
        'report_payload_sha256',
    }:
        raise SyncFinalizeIntegrityError('finalization report integrity metadata is missing')
    if (
        integrity.get('algorithm') != 'sha256'
        or integrity.get('report_payload_sha256') != finalization_report_payload_sha256(report)
    ):
        raise SyncFinalizeIntegrityError('finalization report fingerprint mismatch')
    return report


def _current_verified_state(root, integrity, verification):
    plan = integrity['plan']
    changes = collect_git_changes(root, plan['base_commit'])
    scope = _scope_analysis(
        root,
        changes,
        plan['allowed_write_set'],
        integrity['pack_path'],
        plan['ignored_untracked_baseline'],
    )
    if scope['changes_outside_scope'] or scope['partially_staged_paths']:
        problems = scope['changes_outside_scope'] + scope['partially_staged_paths']
        raise SyncFinalizeScopeError(
            'repository differs outside the verified change set: ' + ', '.join(problems)
        )
    state, fingerprint = verified_working_tree_state(root, plan, scope)
    if (
        fingerprint != verification['verification_fingerprint']
        or state != verification['verified_working_tree_state']
    ):
        raise SyncFinalizeIntegrityError(
            'verified canonical state is stale; run project sync verify again'
        )
    return changes, scope


def _paths_from_records(records):
    return sorted({path for record in records for path in _record_paths(record)})


def _base_report(integrity, verification, preflight, commit_requested, push_requested, message):
    verified_paths = verification['actual_changed_canonical_paths']
    warnings = []
    if not preflight['remote']:
        warnings.append('current branch has no configured remote')
    if not preflight['upstream']:
        warnings.append('current branch has no configured upstream')
    if not verified_paths:
        warnings.append('verification contains no canonical changes to commit')
    report = {
        'schema_version': 1,
        'pack_id': integrity['plan']['pack_id'],
        'pack_hash': integrity['plan']['pack_content_sha256'],
        'base_commit': integrity['plan']['base_commit'],
        'verification_fingerprint': verification['verification_fingerprint'],
        'current_head': integrity['head'],
        'branch': preflight['branch'],
        'upstream': preflight['upstream'],
        'remote': preflight['remote'],
        'remote_url': preflight['remote_url'],
        'verified_canonical_paths': verified_paths,
        'staged_paths': [],
        'commit_requested': bool(commit_requested),
        'commit_ready': bool(verified_paths),
        'commit_result': 'not_requested',
        'commit_sha': None,
        'commit_message': message,
        'committed_paths': [],
        'push_requested': bool(push_requested),
        'push_result': 'not_requested',
        'state': 'verified',
        'deterministic_verification': 'passed',
        'semantic_meaning_verified_by_cli': False,
        'human_semantic_approval_required_before_commit': True,
        'human_semantic_review_reminder': (
            '--commit is an explicit technical authorization to record this already '
            'verified state; the CLI does not cryptographically prove approver identity.'
        ),
        'errors': [],
        'warnings': warnings,
    }
    for field in (
        'selected_skills',
        'skills_registry_sha256',
        'task_write_scope',
        'effective_write_scope',
        'skill_write_authorizations',
    ):
        if field in integrity['plan']:
            report[field] = integrity['plan'][field]
    return report


def _markdown_report(report):
    lines = [
        '# SYNC Finalization',
        '',
        '> Deterministic verification: passed. Semantic meaning verified by CLI: false.',
        '> Human semantic approval is required before commit.',
        '',
        f"- Pack: `{report['pack_id']}`",
        f"- State: **{report['state']}**",
        f"- Pack SHA-256: `{report['pack_hash']}`",
        f"- Verification fingerprint: `{report['verification_fingerprint']}`",
        f"- Base commit: `{report['base_commit']}`",
        f"- Current HEAD: `{report['current_head']}`",
        f"- Branch: `{report['branch']}`",
        f"- Upstream: `{report['upstream'] or 'none'}`",
        '',
        '## Verified Canonical Paths',
        '',
    ]
    lines.extend(f'- `{path}`' for path in report['verified_canonical_paths'])
    if not report['verified_canonical_paths']:
        lines.append('- None.')
    lines.extend([
        '',
        '## Commit',
        '',
        f"- Requested: `{str(report['commit_requested']).lower()}`",
        f"- Result: `{report['commit_result']}`",
        f"- SHA: `{report['commit_sha'] or 'none'}`",
        f"- Message: `{report['commit_message'] or 'none'}`",
        '',
        '## Push',
        '',
        f"- Requested: `{str(report['push_requested']).lower()}`",
        f"- Result: `{report['push_result']}`",
        '',
        '## Warnings / Errors',
        '',
    ])
    lines.extend(f'- Warning: {item}' for item in report['warnings'])
    lines.extend(f'- Error: {item}' for item in report['errors'])
    if not report['warnings'] and not report['errors']:
        lines.append('- None.')
    lines.extend(['', report['human_semantic_review_reminder'], ''])
    return '\n'.join(lines)


def _write_reports(output, report):
    _seal_finalization_report(report)
    try:
        _write_output(
            output,
            'finalization.json',
            json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + '\n',
        )
        _write_output(output, 'finalization.md', _markdown_report(report))
    except SyncPlanError as exc:
        raise SyncFinalizeIntegrityError(str(exc)) from exc


def _record_failure(output, report, error):
    report['errors'].append(str(error))
    if isinstance(error, SyncPushError):
        report['push_result'] = 'failed'
        if report.get('commit_sha'):
            report['state'] = 'committed'
    elif isinstance(error, SyncCommitError):
        report['commit_result'] = 'failed'
    if report['state'] != 'committed':
        report['state'] = 'failed'
    _write_reports(output, report)


def _index_snapshot(root):
    index = _git_path(root, 'index')
    return index, index.exists(), index.read_bytes() if index.exists() else None


def _restore_index(snapshot):
    index, existed, content = snapshot
    if existed:
        index.parent.mkdir(parents=True, exist_ok=True)
        temporary = index.with_name(f'{index.name}.project-sync-{uuid.uuid4().hex}.tmp')
        try:
            temporary.write_bytes(content)
            os.replace(temporary, index)
        finally:
            if temporary.exists():
                temporary.unlink()
    elif index.exists():
        index.unlink()


def _safe_stage(root, integrity, verification):
    paths = verification['actual_changed_canonical_paths']
    if not paths:
        raise SyncCommitError('no verified canonical changes to commit')
    result = _run_git(root, ['add', '--', *paths], allow_failure=True)
    if result.returncode:
        raise SyncCommitError(
            f'safe staging failed: {result.stderr.strip() or "unknown Git error"}'
        )
    changes = collect_git_changes(root, integrity['plan']['base_commit'])
    scope = _scope_analysis(
        root,
        changes,
        integrity['plan']['allowed_write_set'],
        integrity['pack_path'],
        integrity['plan']['ignored_untracked_baseline'],
    )
    staged = _paths_from_records(changes['staged'])
    unstaged = sorted(
        path for path in _paths_from_records(changes['unstaged'])
        if not _is_generated(path) and path != _repo_relative(root, integrity['pack_path'])
    )
    if scope['changes_outside_scope'] or scope['partially_staged_paths']:
        raise SyncFinalizeScopeError('safe staging exposed an out-of-scope Git change')
    if staged != paths:
        raise SyncCommitError(
            f'staged paths differ from verified paths: expected {paths}, found {staged}'
        )
    if unstaged:
        raise SyncCommitError(
            'verified paths still have unstaged content after staging: ' + ', '.join(unstaged)
        )
    _verify_staged_content(root, verification)
    state, fingerprint = verified_working_tree_state(root, integrity['plan'], scope)
    if (
        fingerprint != verification['verification_fingerprint']
        or state != verification['verified_working_tree_state']
    ):
        raise SyncFinalizeIntegrityError('canonical state changed during safe staging')
    return staged


def _verify_staged_content(root, verification):
    paths = verification['actual_changed_canonical_paths']
    result = _run_git(root, ['ls-files', '--stage', '-z', '--', *paths], text=False)
    entries = {}
    for value in result.stdout.decode('utf-8', errors='strict').split('\0'):
        if not value:
            continue
        metadata, path = value.split('\t', 1)
        mode, object_id, stage = metadata.split()
        if stage != '0':
            raise SyncCommitError(f'non-zero-stage index entry for verified path: {path}')
        entries[path.replace('\\', '/')] = {'mode': mode, 'object_id': object_id}

    expected_by_path = {
        item['path']: item
        for item in verification['verified_working_tree_state']['entries']
    }
    for path in paths:
        expected = expected_by_path.get(path)
        if not isinstance(expected, dict):
            raise SyncFinalizeIntegrityError(f'verified state is missing path: {path}')
        if expected.get('state') == 'absent':
            if path in entries:
                raise SyncCommitError(f'deleted verified path remains in the index: {path}')
            continue
        if path not in entries:
            raise SyncCommitError(f'verified file is missing from the index: {path}')
        blob = _run_git(
            root,
            ['hash-object', f'--path={path}', path],
            allow_failure=True,
        )
        if blob.returncode:
            raise SyncCommitError(
                f'cannot fingerprint staged content for {path}: {blob.stderr.strip()}'
            )
        if entries[path]['object_id'] != blob.stdout.strip():
            raise SyncCommitError(f'staged content differs from verified working tree: {path}')


def _commit_paths(root, commit_sha):
    return _paths_from_records(
        _parse_name_status(
            root,
            [
                'diff-tree',
                '--root',
                '--no-commit-id',
                '--name-status',
                '-z',
                '--find-renames',
                '-r',
                commit_sha,
                '--',
            ],
        )
    )


def _prove_commit(root, report, verification):
    commit_sha = report.get('commit_sha')
    if not COMMIT_SHA_RE.fullmatch(commit_sha or ''):
        raise SyncFinalizeIntegrityError('recorded SYNC commit SHA is missing or malformed')
    exists = _run_git(root, ['cat-file', '-e', f'{commit_sha}^{{commit}}'], allow_failure=True)
    if exists.returncode:
        raise SyncFinalizeIntegrityError('recorded SYNC commit is not readable')
    head = _run_git(root, ['rev-parse', 'HEAD']).stdout.strip().lower()
    if head != commit_sha:
        raise SyncFinalizeIntegrityError(
            'HEAD changed after the recorded SYNC commit; cannot prove pack-to-commit identity'
        )
    parents = _run_git(root, ['rev-list', '--parents', '-n', '1', commit_sha]).stdout.split()
    if len(parents) != 2 or parents[1].lower() != report['base_commit']:
        raise SyncFinalizeIntegrityError('recorded SYNC commit is not a direct child of base_commit')
    committed_paths = _commit_paths(root, commit_sha)
    if committed_paths != verification['actual_changed_canonical_paths']:
        raise SyncFinalizeIntegrityError(
            'recorded commit paths do not match the verified canonical change set'
        )
    message = _run_git(root, ['show', '-s', '--format=%B', commit_sha]).stdout.rstrip('\r\n')
    if message != report.get('commit_message'):
        raise SyncFinalizeIntegrityError('recorded commit message does not match Git history')
    return committed_paths


def _commit(root, integrity, verification, report, message):
    snapshot = _index_snapshot(root)
    committed = False
    try:
        staged = _safe_stage(root, integrity, verification)
        report['staged_paths'] = staged
        result = _run_git(root, ['commit', '-m', message], allow_failure=True)
        if result.returncode:
            raise SyncCommitError(
                f'Git commit failed: {result.stderr.strip() or result.stdout.strip() or "unknown Git error"}'
            )
        committed = True
        commit_sha = _run_git(root, ['rev-parse', 'HEAD']).stdout.strip().lower()
        report['commit_sha'] = commit_sha
        report['commit_message'] = message
        report['committed_paths'] = verification['actual_changed_canonical_paths']
        report['commit_result'] = 'committed'
        report['current_head'] = commit_sha
        report['state'] = 'committed'
        _prove_commit(root, report, verification)
    except Exception:
        if not committed:
            _restore_index(snapshot)
        raise


def _push(root, report, verification, preflight):
    if not report.get('commit_sha'):
        raise SyncPushError(
            'push requires an existing verified commit or explicit --commit --push'
        )
    _prove_commit(root, report, verification)
    if not preflight['remote'] or preflight['remote'] == '.' or not preflight['remote_url']:
        raise SyncPushError('current branch has no usable configured remote')
    if not preflight['upstream']:
        raise SyncPushError('current branch has no configured upstream')
    upstream_result = _run_git(root, ['rev-parse', '@{upstream}'], allow_failure=True)
    already_synchronized = (
        not upstream_result.returncode
        and upstream_result.stdout.strip().lower() == report['commit_sha']
    )
    result = _run_git(root, ['push'], allow_failure=True)
    if result.returncode:
        raise SyncPushError(
            f'Git push failed after commit succeeded: '
            f'{result.stderr.strip() or result.stdout.strip() or "unknown Git error"}'
        )
    upstream_after = _run_git(root, ['rev-parse', '@{upstream}'], allow_failure=True)
    if upstream_after.returncode or upstream_after.stdout.strip().lower() != report['commit_sha']:
        raise SyncPushError('push returned success but upstream does not match the SYNC commit')
    report['push_result'] = 'already_synchronized' if already_synchronized else 'pushed'
    report['state'] = 'pushed'


def _normalize_message(pack_id, message):
    if message is None:
        return f'sync: apply {pack_id}'
    if not isinstance(message, str) or not message.strip():
        raise SyncCommitError('commit message must be a non-empty string')
    if '\0' in message or len(message) > MAX_COMMIT_MESSAGE_CHARS:
        raise SyncCommitError('commit message contains NUL or is too long')
    return message.strip()


def _selector_pack_id(selector):
    value = str(selector)
    if DURABLE_PACK_ID.fullmatch(value):
        return value
    name = Path(value).name
    if name.endswith(('.yaml', '.yml', '.json')):
        candidate = Path(name).stem
        if DURABLE_PACK_ID.fullmatch(candidate):
            return candidate
    return None


def _transport_snapshot_unchanged(root, pack):
    transport = (pack.get('provenance') or {}).get('transport')
    if not isinstance(transport, dict) or transport.get('kind') != 'github_issue':
        return False
    # Import lazily: pull depends on intake/planning but not finalization.
    from .sync_pull import github_repository, gh_get, issue_request, pull_policy
    repository = github_repository(root)
    if repository.lower() != transport['repository'].lower():
        raise SyncFinalizeIntegrityError('GitHub transport repository differs from current origin')
    _, policy = pull_policy(root, repository)
    issue = gh_get(root, f'repos/{repository}/issues/{transport["issue_number"]}')
    current = issue_request(issue, repository, policy['allowed_authors'], require_open=False)
    if current['transport'] != transport:
        raise SyncFinalizeIntegrityError('GitHub transport snapshot changed after intake')
    return True


def _publish_terminal(root, integrity, *, outcome, reason, verification=None, report=None):
    pack = integrity['pack']
    raw = integrity['pack_path'].read_bytes()
    fingerprint = verification.get('verification_fingerprint') if verification else None
    commit_sha = report.get('commit_sha') if report else None
    paths = verification.get('actual_changed_canonical_paths', []) if verification else []
    push_proof = None
    if outcome == 'pushed':
        push_proof = {
            'remote': report['remote'],
            'upstream': report['upstream'],
            'commit_sha': commit_sha,
        }
    try:
        binding = build_terminal_binding(
            pack,
            raw,
            outcome=outcome,
            reason=reason,
            verification_fingerprint=fingerprint,
            commit_sha=commit_sha,
            verified_paths=paths,
            push_proof=push_proof,
        )
        archived = terminalize_binding(
            root, integrity['pack_path'], pack, raw, binding,
        )
    except (SyncBindingError, OSError, ValueError, TypeError) as exc:
        raise SyncFinalizeIntegrityError(f'terminalization failed: {exc}') from exc
    return archived


def _complete_without_commit(root, selector, outcome, reason):
    if outcome not in COMPLETE_OUTCOMES:
        raise SyncFinalizeIntegrityError(
            '--outcome must be reviewed-no-change, rejected, or abandoned'
        )
    if not isinstance(reason, str) or not reason.strip() or '\0' in reason:
        raise SyncFinalizeIntegrityError('--complete requires a non-empty --reason')
    try:
        integrity = _resolve_integrity_inputs(root, selector, require_base_head=True)
    except SyncVerifyError as exc:
        raise SyncFinalizeIntegrityError(str(exc)) from exc
    if not _transport_snapshot_unchanged(root, integrity['pack']):
        raise SyncFinalizeIntegrityError('--complete is supported only for GitHub transport packs')
    preflight = _git_preflight(root)
    plan = integrity['plan']
    changes = collect_git_changes(root, plan['base_commit'])
    scope = _scope_analysis(
        root,
        changes,
        plan['allowed_write_set'],
        integrity['pack_path'],
        plan['ignored_untracked_baseline'],
    )
    if scope['actual_changed_paths'] or scope['partially_staged_paths'] or scope['ignored_untracked_drift']:
        problems = sorted(set(
            scope['actual_changed_paths']
            + scope['partially_staged_paths']
            + scope['ignored_untracked_drift']
        ))
        raise SyncFinalizeScopeError(
            'non-commit completion requires a clean canonical baseline: ' + ', '.join(problems)
        )
    verification = None
    if outcome == 'reviewed-no-change':
        verification = _load_verification(integrity['output'], plan)
        _current_verified_state(root, integrity, verification)
        if verification['actual_changed_canonical_paths']:
            raise SyncFinalizeScopeError('reviewed-no-change cannot complete canonical edits')
    pseudo_verification = verification or {
        'actual_changed_canonical_paths': [],
        'verification_fingerprint': None,
    }
    report = _base_report(
        integrity, pseudo_verification, preflight, False, False, None,
    )
    archived = _publish_terminal(
        root,
        integrity,
        outcome=outcome,
        reason=reason.strip(),
        verification=verification,
        report=report,
    )
    report.update({
        'state': 'completed',
        'terminal_outcome': outcome,
        'terminal_reason': reason.strip(),
        'transport_state': 'completed',
        'archive_location': str(archived.path.parent),
    })
    _write_reports(integrity['output'], report)
    return integrity['output'], report


def _completed_response(root, selector, *, complete, outcome, reason, commit, push):
    pack_id = _selector_pack_id(selector)
    if not pack_id:
        return None
    try:
        binding = next(
            (item for item in load_sync_bindings(root)
             if item.state == 'completed' and item.pack['pack_id'] == pack_id),
            None,
        )
    except SyncBindingError as exc:
        raise SyncFinalizeIntegrityError(str(exc)) from exc
    if binding is None:
        return None
    if binding.recoverable_active_path is not None:
        try:
            binding = terminalize_binding(
                root,
                binding.recoverable_active_path,
                binding.pack,
                binding.raw,
                binding.terminal,
            )
        except SyncBindingError as exc:
            raise SyncFinalizeIntegrityError(f'terminal cleanup recovery failed: {exc}') from exc
    terminal = binding.terminal['terminal']
    if complete and (outcome != terminal['outcome'] or reason.strip() != (terminal.get('reason') or '')):
        raise SyncFinalizeIntegrityError('pack is already completed with a different terminal outcome')
    value = terminal_status(binding)
    value.update({
        'state': 'completed',
        'commit_result': 'already_committed' if terminal.get('commit_sha') else 'not_applicable',
        'push_result': 'already_synchronized' if terminal['outcome'] == 'pushed' else 'not_applicable',
        'terminal_outcome': terminal['outcome'],
        'archive_location': str(binding.path.parent),
    })
    if push and terminal['outcome'] != 'pushed':
        raise SyncPushError('completed binding does not contain pushed transport proof')
    return binding.path.parent, value


def finalize_sync(root, selector, *, commit=False, push=False, message=None,
                  complete=False, outcome=None, reason=None):
    """Prepare, commit, and explicitly push one previously verified SYNC state."""
    root = Path(root).resolve()
    if complete and (commit or push or message is not None):
        raise SyncFinalizeIntegrityError('--complete is incompatible with --commit, --push, and --message')
    if not complete and (outcome is not None or reason is not None):
        raise SyncFinalizeIntegrityError('--outcome and --reason require --complete')
    if complete and (outcome is None or reason is None):
        raise SyncFinalizeIntegrityError('--complete requires --outcome and --reason')
    if message is not None and not commit:
        raise SyncCommitError('--message requires --commit')
    completed = _completed_response(
        root, selector, complete=complete, outcome=outcome,
        reason=reason or '', commit=commit, push=push,
    )
    if completed is not None:
        return completed
    if complete:
        return _complete_without_commit(root, selector, outcome, reason)
    try:
        integrity = _resolve_integrity_inputs(root, selector, require_base_head=False)
    except SyncVerifyError as exc:
        raise SyncFinalizeIntegrityError(str(exc)) from exc
    verification = _load_verification(integrity['output'], integrity['plan'])
    github_transport = _transport_snapshot_unchanged(root, integrity['pack'])
    previous = _load_previous_finalization(integrity['output'])
    normalized_message = _normalize_message(integrity['plan']['pack_id'], message)
    try:
        preflight = _git_preflight(root)
    except SyncFinalizeError as exc:
        fallback = {
            'branch': None,
            'upstream': None,
            'remote': None,
            'remote_url': None,
            'operations_in_progress': [],
            'unresolved_conflicts': [],
        }
        report = _base_report(
            integrity,
            verification,
            fallback,
            commit,
            push,
            normalized_message,
        )
        _record_failure(integrity['output'], report, exc)
        raise
    report = _base_report(
        integrity,
        verification,
        preflight,
        commit,
        push,
        normalized_message,
    )

    try:
        if previous and previous.get('commit_sha'):
            for field in ('pack_id', 'pack_hash', 'base_commit', 'verification_fingerprint'):
                if previous.get(field) != report.get(field):
                    raise SyncFinalizeIntegrityError(
                        f'previous finalization does not match {field}'
                    )
            report.update({
                'commit_sha': previous['commit_sha'],
                'commit_message': previous['commit_message'],
                'committed_paths': previous['committed_paths'],
                'commit_result': 'already_committed',
                'current_head': integrity['head'],
                'state': 'committed',
            })
            _prove_commit(root, report, verification)
        elif integrity['head'] != integrity['plan']['base_commit']:
            raise SyncFinalizeIntegrityError(
                'HEAD changed after verification; run planning and verification again'
            )

        changes, _ = _current_verified_state(root, integrity, verification)
        report['staged_paths'] = _paths_from_records(changes['staged'])

        if commit and not report.get('commit_sha'):
            _commit(root, integrity, verification, report, normalized_message)
        elif commit and report.get('commit_sha'):
            report['commit_result'] = 'already_committed'
        elif not report.get('commit_sha'):
            report['state'] = 'prepared'

        if github_transport and report.get('commit_sha') and not push:
            report['transport_state'] = 'awaiting_push'

        if push:
            _push(root, report, verification, preflight)

        if github_transport and push and report.get('push_result') in {'pushed', 'already_synchronized'}:
            # Re-fetch after Git side effects before publishing immutable completion.
            _transport_snapshot_unchanged(root, integrity['pack'])
            archived = _publish_terminal(
                root,
                integrity,
                outcome='pushed',
                reason=None,
                verification=verification,
                report=report,
            )
            report.update({
                'state': 'completed',
                'terminal_outcome': 'pushed',
                'transport_state': 'completed',
                'archive_location': str(archived.path.parent),
            })

        _write_reports(integrity['output'], report)
        return integrity['output'], report
    except SyncFinalizeError as exc:
        _record_failure(integrity['output'], report, exc)
        raise
    except Exception as exc:
        wrapped = SyncFinalizeIntegrityError(str(exc))
        _record_failure(integrity['output'], report, wrapped)
        raise wrapped from exc
