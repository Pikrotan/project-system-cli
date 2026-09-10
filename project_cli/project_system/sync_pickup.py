"""OS-neutral, single bounded automatic SYNC pickup cycle."""
from pathlib import Path

from . import sync_pull
from .sync_bindings import SyncBindingError, validate_transactions
from .sync_intake import SyncIntakeError, _intake_lock, intake_bindings
from .sync_migration import classify_active_binding
from .sync_planning import SyncPlanError, _git_status_paths


BLOCKED_STATUSES = {
    'blocked_active',
    'blocked_awaiting_push',
    'blocked_config',
    'blocked_conflict',
    'blocked_dirty',
    'blocked_malformed',
    'blocked_transaction',
    'blocked_transport',
}


class SyncPickupError(RuntimeError):
    exit_code = 3


class SyncPickupMalformedError(SyncPickupError):
    pass


def _result(repository, status, reason=None, **fields):
    value = {
        'schema_version': 1,
        'repository': repository,
        'status': status,
    }
    if reason:
        value['reason'] = reason
    value.update(fields)
    return value


def _dirty_paths(root):
    return sorted(
        path for path in _git_status_paths(root)
        if path != '.generated' and not path.startswith('.generated/')
    )


def _transport_bindings(bindings, repository):
    by_issue = {}
    for binding in bindings:
        transport = (binding.pack.get('provenance') or {}).get('transport')
        if not transport:
            continue
        if transport.get('kind') != 'github_issue':
            raise SyncPickupError('stored transport binding has an unsupported kind')
        if str(transport.get('repository', '')).lower() != repository.lower():
            raise SyncPickupError('stored transport binding repository differs from origin')
        number = transport.get('issue_number')
        if type(number) is not int or number <= 0 or number in by_issue:
            raise SyncPickupError('duplicate or invalid stored transport Issue identity')
        by_issue[number] = binding
    return by_issue


def _reconcile_completed(root, repository, policy, bindings):
    """Return request identities; any completed transport drift is blocking."""
    request_issues = {}
    for number, binding in sorted(bindings.items()):
        if binding.state != 'completed':
            continue
        if binding.recoverable_active_path is not None:
            raise SyncPickupError(
                f'completed issue #{number} still has a recoverable active inbox copy'
            )
        issue = sync_pull.gh_get(root, f'repos/{repository}/issues/{number}')
        if issue.get('number') != number:
            raise SyncPickupError(f'GitHub returned a mismatched completed issue #{number}')
        try:
            current = sync_pull.issue_request(
                issue, repository, policy['allowed_authors'], require_open=False,
            )
        except (sync_pull.SyncPullError, SyncIntakeError, SyncPlanError) as exc:
            raise SyncPickupError(
                f'transport drift/conflict for completed issue #{number}: {exc}'
            ) from exc
        stored_transport = binding.pack['provenance']['transport']
        stored_request = binding.pack['provenance']['request_id']
        if (not sync_pull.same_transport_request_identity(current['transport'], stored_transport)
                or current['request']['request_id'] != stored_request):
            raise SyncPickupError(
                f'transport drift/conflict for completed issue #{number}'
            )
        request_issues.setdefault(stored_request, set()).add(number)
    return request_issues


def _pending_queue(root, repository, policy, completed_by_issue):
    """Inspect authorized marked open Issues and return the oldest valid request."""
    request_issues = _reconcile_completed(
        root, repository, policy, completed_by_issue,
    )
    allowed = {author.lower() for author in policy['allowed_authors']}
    relevant = {}
    for summary in sync_pull.open_transport_issues(root, repository):
        number = summary.get('number') if isinstance(summary, dict) else None
        if type(number) is not int or number <= 0:
            raise SyncPickupError('GitHub search returned an invalid Issue identity')
        if number in completed_by_issue:
            continue
        issue = sync_pull.gh_get(root, f'repos/{repository}/issues/{number}')
        if issue.get('number') != number:
            raise SyncPickupError(f'GitHub returned a mismatched issue #{number}')
        title = issue.get('title')
        user = issue.get('user')
        author = user.get('login') if isinstance(user, dict) else None
        if issue.get('state') != 'open':
            continue
        if not isinstance(title, str) or not title.startswith(sync_pull.MARKER):
            continue
        if not isinstance(author, str) or author.lower() not in allowed:
            continue
        relevant[number] = issue

    if not relevant:
        return None, 0

    head_number = min(relevant)
    valid = {}
    head_error = None
    for number, issue in sorted(relevant.items()):
        try:
            candidate = sync_pull.issue_request(
                issue, repository, policy['allowed_authors'], require_open=True,
            )
        except (sync_pull.SyncPullError, SyncIntakeError, SyncPlanError) as exc:
            if number == head_number:
                head_error = exc
            continue
        valid[number] = candidate
        request_issues.setdefault(candidate['request']['request_id'], set()).add(number)

    duplicates = {
        request_id: sorted(numbers)
        for request_id, numbers in request_issues.items()
        if len(numbers) > 1
    }
    if duplicates:
        rendered = ', '.join(
            f'{request_id}: issues {numbers}'
            for request_id, numbers in sorted(duplicates.items())
        )
        raise SyncPickupError(
            f'duplicate request_id across relevant transport Issues: {rendered}'
        )
    if head_error is not None:
        raise SyncPickupMalformedError(
            f'malformed authorized oldest issue #{head_number}: {head_error}'
        )
    return valid[head_number], len(relevant)


def pickup_once(root):
    """Run exactly one bounded pickup cycle; never apply semantic changes."""
    root = Path(root).resolve()
    repository = None
    try:
        repository = sync_pull.github_repository(root)
        _, policy = sync_pull.pull_policy(root, repository)
    except (sync_pull.SyncPullError, SyncPlanError, OSError, ValueError, TypeError) as exc:
        return _result(repository, 'blocked_config', str(exc))

    try:
        with _intake_lock(root):
            transactions = validate_transactions(root)
            if transactions:
                ids = sorted(item['pack_id'] for item in transactions)
                return _result(
                    repository, 'blocked_transaction',
                    'unfinished terminal transaction(s): ' + ', '.join(ids),
                )

            bindings = intake_bindings(root)
            active = [binding for binding in bindings if binding.state == 'active']
            if active:
                classifications = [classify_active_binding(root, binding) for binding in active]
                conflicts = [item for item in classifications if item['status'] == 'conflict']
                if conflicts:
                    return _result(
                        repository, 'blocked_conflict', conflicts[0].get('detail', 'active binding conflict'),
                        active_packs=sorted(binding.pack['pack_id'] for binding in active),
                    )
                awaiting = [item['pack_id'] for item in classifications if item['status'] == 'awaiting_push']
                if awaiting:
                    return _result(
                        repository, 'blocked_awaiting_push',
                        'verified SYNC commit is awaiting push', active_packs=sorted(awaiting),
                    )
                return _result(
                    repository, 'blocked_active', 'an active SYNC pack must be resolved first',
                    active_packs=sorted(binding.pack['pack_id'] for binding in active),
                )

            try:
                dirty = _dirty_paths(root)
            except SyncPlanError as exc:
                return _result(repository, 'blocked_conflict', str(exc))
            if dirty:
                return _result(
                    repository, 'blocked_dirty',
                    'working tree has non-disposable changes: ' + ', '.join(dirty),
                    dirty_paths=dirty,
                )

            by_issue = _transport_bindings(bindings, repository)
            candidate, pending_count = _pending_queue(root, repository, policy, by_issue)
            if candidate is None:
                return _result(
                    repository, 'no_pending', 'no authorized marked open request is pending',
                    processed_count=len(by_issue), pending_count=0,
                )
            issue_number = candidate['transport']['issue_number']
            report = sync_pull.pull_sync(
                root, plan=True, issue_number=issue_number, _lock_held=True,
                _strict_processed_drift=True,
            )
            return _result(
                repository,
                'created' if report['status'] == 'created' else 'processed',
                issue_number=issue_number,
                pack_id=report['pack_id'],
                pack_path=report['pack_path'],
                plan=report['plan_result'],
                pending_count=pending_count,
                processed_count=len(by_issue),
            )
    except SyncPickupMalformedError as exc:
        return _result(repository, 'blocked_malformed', str(exc))
    except SyncPlanError as exc:
        return _result(repository, 'blocked_transport', str(exc))
    except (SyncPickupError, SyncBindingError, SyncIntakeError) as exc:
        return _result(repository, 'blocked_conflict', str(exc))
    except sync_pull.SyncPullError as exc:
        message = str(exc)
        status = 'blocked_transport' if message.startswith('gh ') or 'GitHub' in message else 'blocked_conflict'
        return _result(repository, status, message)
