"""Bridge v2: read GitHub Issues through gh; delegate all intake to Bridge v1."""
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator

from .sync_intake import (
    SyncIntakeError, _check_report_paths, _find_reusable, _read_request,
    _intake_lock, intake_bindings, intake_sync,
)
from .sync_bindings import SyncBindingError
from .sync_planning import (
    MAX_SYNC_PACK_BYTES, SyncPlanError, _git_head, _write_output,
    load_sync_bytes, plan_sync, sync_format_checker,
)
from .utils import distribution_root

MARKER = '[SYNC REQUEST]'
REPOSITORY_RE = re.compile(r'[A-Za-z0-9-]+/[A-Za-z0-9_.-]+')
AUTHOR_RE = re.compile(r'[A-Za-z0-9-]+(?:\[bot\])?')
TRANSPORT_REQUEST_IDENTITY_FIELDS = (
    'kind',
    'repository',
    'issue_number',
    'issue_url',
    'issue_author',
    'body_sha256',
    'request_sha256',
    'title_sha256',
)


class SyncPullError(RuntimeError):
    exit_code = 2


def transport_request_identity(transport):
    """Return immutable GitHub request identity, excluding lifecycle provenance."""
    if not isinstance(transport, dict):
        raise SyncPullError('GitHub transport identity is missing')
    missing = [field for field in TRANSPORT_REQUEST_IDENTITY_FIELDS if field not in transport]
    if missing:
        raise SyncPullError(
            'GitHub transport identity is missing: ' + ', '.join(missing)
        )
    identity = {field: transport[field] for field in TRANSPORT_REQUEST_IDENTITY_FIELDS}
    for field in ('repository', 'issue_url', 'issue_author'):
        value = identity[field]
        if not isinstance(value, str):
            raise SyncPullError(f'GitHub transport identity field {field} is invalid')
        identity[field] = value.lower()
    return identity


def same_transport_request_identity(left, right):
    """Compare request/Issue identity while retaining mutable fields as provenance."""
    return transport_request_identity(left) == transport_request_identity(right)


def github_repository(root):
    result = subprocess.run(['git', 'remote', 'get-url', '--all', 'origin'], cwd=root,
                            capture_output=True, text=True, check=False, timeout=30)
    urls = result.stdout.strip().splitlines()
    if result.returncode or len(urls) != 1:
        raise SyncPullError('sync pull requires exactly one Git origin fetch URL')
    url = urls[0]
    if url.startswith('git@github.com:'):
        repository = url[len('git@github.com:'):]
    else:
        parsed = urlsplit(url)
        if (parsed.hostname != 'github.com' or parsed.query or parsed.fragment
                or parsed.password or parsed.port not in {None, 22, 443}
                or not ((parsed.scheme == 'https' and parsed.username is None and parsed.port in {None, 443})
                        or (parsed.scheme == 'ssh' and parsed.username == 'git' and parsed.port in {None, 22}))):
            raise SyncPullError('origin must be an HTTPS or git SSH github.com repository URL')
        repository = parsed.path.removeprefix('/')
    repository = repository.removesuffix('.git')
    if not REPOSITORY_RE.fullmatch(repository) or repository.split('/')[-1] in {'.', '..'}:
        raise SyncPullError('invalid GitHub repository in origin')
    return repository


def pull_policy(root, repository):
    config, _ = load_sync_bytes((root / 'project.yaml').read_bytes(), 'project configuration')
    external = config.get('external_systems')
    github = external.get('github', {}) if isinstance(external, dict) else {}
    if (not isinstance(github, dict) or github.get('enabled') is not True
            or github.get('mode') != 'sync'):
        raise SyncPullError(
            'sync pull is disabled: configure external_systems.github.enabled, '
            'mode: sync, and sync_pull.allowed_authors'
        )
    policy = github.get('sync_pull')
    schema = json.loads((distribution_root() / 'schemas/project.schema.json').read_text(encoding='utf-8'))
    errors = list(Draft202012Validator(schema['$defs']['github_sync_pull']).iter_errors(policy))
    if errors:
        raise SyncPullError('invalid sync_pull policy: ' + '; '.join(error.message for error in errors))
    authors = policy['allowed_authors']
    if any(not AUTHOR_RE.fullmatch(author) for author in authors) or len({a.lower() for a in authors}) != len(authors):
        raise SyncPullError('allowed_authors must contain unique GitHub logins (case-insensitive)')
    expected = policy.get('expected_repository', repository)
    if not REPOSITORY_RE.fullmatch(expected) or expected.lower() != repository.lower():
        raise SyncPullError('wrong repository: origin differs from sync_pull.expected_repository')
    return config['project']['id'], policy


def _gh_executable():
    executable = shutil.which('gh')
    if executable:
        return executable
    if os.name == 'nt':
        candidate = Path(os.environ.get('ProgramFiles', 'C:/Program Files')) / 'GitHub CLI/gh.exe'
        if candidate.is_file():
            return str(candidate)
    raise SyncPullError('gh is unavailable; install GitHub CLI and run gh auth login')


def gh_get(root, endpoint, fields=()):
    """GET only, explicit host/repository; never read/store a token or echo gh stderr."""
    args = [_gh_executable(), 'api', '--hostname', 'github.com', '--method', 'GET', endpoint]
    for field in fields:
        args += ['-f', field]
    env = dict(os.environ, GH_PROMPT_DISABLED='1', GH_NO_UPDATE_NOTIFIER='1')
    try:
        result = subprocess.run(args, cwd=root, env=env, capture_output=True, check=False, timeout=30)
    except FileNotFoundError as exc:
        raise SyncPullError('gh is unavailable; install GitHub CLI') from exc
    except subprocess.TimeoutExpired as exc:
        raise SyncPullError('gh GET timed out; no acknowledgement was made') from exc
    if result.returncode:
        raise SyncPullError(f'gh GET failed (exit {result.returncode}); check gh auth status --hostname github.com and repository access')
    value, _ = load_sync_bytes(result.stdout, 'gh JSON response')
    return value


def open_transport_issues(root, repository):
    items, seen = [], set()
    # GitHub search can return at most 1000 results. Never silently truncate.
    for page in range(1, 21):
        result = gh_get(root, 'search/issues', (
            f'q=repo:{repository} is:issue is:open in:title "{MARKER}"',
            'per_page=50', f'page={page}',
        ))
        total = result.get('total_count')
        batch = result.get('items')
        if (type(total) is not int or total < 0 or total > 1000
                or result.get('incomplete_results') is not False or not isinstance(batch, list)):
            raise SyncPullError('incomplete/invalid GitHub search; narrow selection with --issue NUMBER')
        for issue in batch:
            number = issue.get('number') if isinstance(issue, dict) else None
            if type(number) is not int or number <= 0 or number in seen:
                raise SyncPullError('invalid/duplicate issue identity in GitHub search')
            seen.add(number)
            if isinstance(issue.get('title'), str) and issue['title'].startswith(MARKER):
                items.append(issue)
        if len(seen) >= total:
            return items
        if not batch:
            break
    raise SyncPullError('incomplete GitHub pagination; retry or select --issue NUMBER')


def extract_request(body):
    """Accept raw YAML/JSON or one tagged fence; surrounding prose is inert."""
    if not isinstance(body, str) or len(body.encode('utf-8')) > MAX_SYNC_PACK_BYTES:
        raise SyncPullError('issue body is missing or exceeds the transport size limit')
    envelope = body.lstrip()
    fences = list(re.finditer(r'(?m)^```[^\r\n]*(?:\r?\n|$)', envelope))
    if fences:
        if (len(fences) != 2 or not re.fullmatch(r'```(?:yaml|yml|json)[ \t]*\r?\n', fences[0][0])
                or not re.fullmatch(r'```[ \t]*(?:\r?\n|$)', fences[1][0])):
            raise SyncPullError('issue must contain exactly one yaml/json request fence')
        content = envelope[fences[0].end():fences[1].start()]
        content = content.removesuffix('\n').removesuffix('\r')
        return content.encode('utf-8')
    return body.encode('utf-8')


def issue_request(issue, repository, allowed_authors, *, require_open=True):
    number = issue.get('number')
    if type(number) is not int or number <= 0 or 'pull_request' in issue:
        raise SyncPullError('transport must be a GitHub Issue, not a pull request')
    expected_url = f'https://github.com/{repository}/issues/{number}'
    if str(issue.get('html_url', '')).lower() != expected_url.lower():
        raise SyncPullError(f'wrong repository/URL for issue #{number}')
    if require_open and issue.get('state') != 'open':
        raise SyncPullError(f'issue #{number} is not open')
    title = issue.get('title')
    if not isinstance(title, str) or not title.startswith(MARKER):
        raise SyncPullError(f'issue #{number} does not have the exact {MARKER} title marker')
    user = issue.get('user')
    author = user.get('login') if isinstance(user, dict) else None
    if not isinstance(author, str) or author.lower() not in {a.lower() for a in allowed_authors}:
        raise SyncPullError(f'unauthorized transport author for issue #{number}')
    raw = extract_request(issue.get('body'))
    request, request_hash = _read_request('-', stdin=BytesIO(raw))
    transport = {
        'kind': 'github_issue', 'repository': repository, 'issue_number': number,
        'issue_url': expected_url, 'issue_author': author,
        'issue_updated_at': issue.get('updated_at'),
        'body_sha256': sha256(issue['body'].encode('utf-8')).hexdigest(),
        'request_sha256': request_hash, 'title_sha256': sha256(title.encode('utf-8')).hexdigest(),
    }
    schema = json.loads((distribution_root() / 'schemas/sync-pack.schema.json').read_text(encoding='utf-8'))
    errors = list(Draft202012Validator(schema['$defs']['github_issue_transport'],
                                      format_checker=sync_format_checker()).iter_errors(transport))
    if errors:
        raise SyncPullError(f'invalid transport metadata for issue #{number}: ' + '; '.join(e.message for e in errors))
    return {'request': request, 'raw': raw, 'transport': transport, 'binding': None}


def inspect_pull(root, issue_number=None, *, strict_processed_drift=False):
    """Read-only discovery/authorship/schema/drift checks; no generated writes."""
    root = Path(root).resolve()
    repository = github_repository(root)
    project_id, policy = pull_policy(root, repository)
    head = _git_head(root)
    bindings = {}
    for binding in intake_bindings(root):
        path, pack, raw = tuple(binding)
        transport = pack.get('provenance', {}).get('transport')
        if not transport:
            continue
        if transport['repository'].lower() != repository.lower():
            raise SyncPullError('wrong repository: stored transport binding differs from origin')
        number = transport['issue_number']
        if number in bindings:
            raise SyncPullError(f'duplicate stored transport binding for issue #{number}')
        bindings[number] = binding
    if issue_number is not None and (type(issue_number) is not int or issue_number <= 0):
        raise SyncPullError('--issue must be a positive integer')
    # Explicit selection still discovers marked open candidates so duplicate request
    # identities cannot be hidden by choosing one Issue. The selected Issue is added
    # independently, so search-index lag cannot hide it.
    initial = open_transport_issues(root, repository)
    numbers = set(bindings)
    if issue_number is not None:
        numbers.add(issue_number)
    for issue in initial:
        number = issue.get('number')
        if type(number) is not int or number <= 0:
            raise SyncPullError('GitHub response has an invalid issue number')
        numbers.add(number)
    candidates, reconciliation_warnings = {}, []
    for number in sorted(numbers):
        try:
            issue = gh_get(root, f'repos/{repository}/issues/{number}')
            if issue.get('number') != number:
                raise SyncPullError('GitHub response has a mismatched issue number')
            candidate = issue_request(issue, repository, policy['allowed_authors'], require_open=number not in bindings)
            if number in bindings:
                binding = bindings[number]
                path, pack, raw = tuple(binding)
                stored_transport = pack['provenance']['transport']
                completed_identity_matches = (
                    binding.state == 'completed'
                    and strict_processed_drift
                    and same_transport_request_identity(candidate['transport'], stored_transport)
                    and candidate['request']['request_id'] == pack['provenance']['request_id']
                )
                if not completed_identity_matches and candidate['transport'] != stored_transport:
                    raise SyncPullError('transport metadata/body/title changed')
                if binding.state == 'completed':
                    candidate['binding'] = binding
                else:
                    existing, _ = _find_reusable(root, candidate['request'], candidate['transport']['request_sha256'],
                                                 project_id, pack['base_commit'], candidate['transport'])
                    if existing is None or existing[0] != path:
                        raise SyncPullError('stored transport binding cannot be verified')
                    _check_report_paths(root, pack['pack_id'])
                    candidate['binding'] = binding
        except (SyncPullError, SyncIntakeError, SyncPlanError) as exc:
            if number == issue_number:
                if number in bindings:
                    raise SyncPullError(f'transport drift/conflict for processed issue #{number}: {exc}') from exc
                raise
            if strict_processed_drift and number in bindings:
                raise SyncPullError(
                    f'transport drift/conflict for processed issue #{number}: {exc}'
                ) from exc
            if number in bindings or issue_number is not None:
                reconciliation_warnings.append(f'issue #{number}: reconciliation needed: {exc}')
                continue
            raise
        candidates[number] = candidate
    request_issues = {}
    for number, binding in bindings.items():
        pack = binding.pack
        request_issues.setdefault(pack['provenance']['request_id'], set()).add(number)
    for number, candidate in candidates.items():
        request_issues.setdefault(candidate['request']['request_id'], set()).add(number)
    duplicates = {request_id: sorted(numbers) for request_id, numbers in request_issues.items() if len(numbers) > 1}
    if duplicates:
        rendered = ', '.join(f'{request_id}: issues {numbers}' for request_id, numbers in sorted(duplicates.items()))
        raise SyncPullError(f'duplicate request_id across transport Issues: {rendered}; use one Issue per request')
    pending = sorted(number for number, candidate in candidates.items() if candidate['binding'] is None)
    if issue_number is not None:
        selected = candidates.get(issue_number)
        if selected is None:
            raise SyncPullError('selected Issue was not returned by GitHub')
    elif len(pending) > 1:
        raise SyncPullError(f'multiple pending transport Issues {pending}; select one with --issue NUMBER (no packs written)')
    elif pending:
        selected = candidates[pending[0]]
    elif len(candidates) == 1:
        selected = next(iter(candidates.values()))
    else:
        selected = None
    return {'repository': repository, 'project_id': project_id, 'head': head,
            'policy': policy, 'selected': selected, 'processed_count': len(bindings),
            'warnings': reconciliation_warnings}


def pull_sync(root, *, plan=False, issue_number=None, _lock_held=False,
              _strict_processed_drift=False):
    try:
        root = Path(root).resolve()
        if _lock_held:
            return _pull_sync(
                root, plan=plan, issue_number=issue_number,
                strict_processed_drift=_strict_processed_drift,
            )
        with _intake_lock(root):
            return _pull_sync(
                root, plan=plan, issue_number=issue_number,
                strict_processed_drift=_strict_processed_drift,
            )
    except SyncPullError:
        raise
    except (SyncIntakeError, SyncBindingError, SyncPlanError, OSError, ValueError, TypeError, KeyError, subprocess.TimeoutExpired) as exc:
        raise SyncPullError(str(exc)) from exc


def _pull_sync(root, *, plan, issue_number, strict_processed_drift=False):
    inspection = inspect_pull(
        root, issue_number, strict_processed_drift=strict_processed_drift,
    )
    candidate = inspection['selected']
    if candidate is None:
        if plan and inspection['processed_count'] > 1:
            raise SyncPullError('no pending requests; select --issue NUMBER to replan a processed pack')
        return {'status': 'no_pending', 'repository': inspection['repository'],
                'processed_count': inspection['processed_count'], 'warnings': inspection['warnings']}
    repository, transport = inspection['repository'], candidate['transport']
    endpoint = f'repos/{repository}/issues/{transport["issue_number"]}'

    def refresh():
        fresh = issue_request(gh_get(root, endpoint), repository, inspection['policy']['allowed_authors'],
                              require_open=candidate['binding'] is None)
        if fresh['transport'] != transport:
            raise SyncPullError('transport drift/conflict during pull; immutable pack, if already created, is retained')

    refresh()
    if (github_repository(root) != repository or pull_policy(root, repository) != (inspection['project_id'], inspection['policy'])
            or _git_head(root) != inspection['head']):
        raise SyncPullError('local repository/policy/HEAD changed during pull; retry')
    if candidate['binding'] is None:
        path, intake = intake_sync(
            root, '-', stdin=BytesIO(candidate['raw']), plan=plan,
            transport=transport, _lock_held=True,
        )
        status = intake['intake_result']
        pack_id, base_commit = intake['pack_id'], intake['base_commit']
        pack_hash = intake['pack_sha256']
    else:
        binding = candidate['binding']
        path, pack, raw = tuple(binding)
        pack_id, base_commit = pack['pack_id'], pack['base_commit']
        pack_hash = sha256(raw).hexdigest()
        status = 'completed' if binding.state == 'completed' else 'already_processed'
        if plan:
            if binding.state == 'completed':
                raise SyncPullError('completed transport pack cannot be replanned or semantically applied again')
            plan_sync(root, path)  # A stale binding is never silently rebound by pull.
    refresh()
    receipt = {
        'schema_version': 1, 'acknowledgement': 'local_only', 'status': 'processed',
        'pack_id': pack_id,
        'pack_path': (binding.original_pack_path if candidate['binding'] is not None and binding.state == 'completed'
                      else path.relative_to(root).as_posix()),
        'pack_sha256': pack_hash, 'base_commit': base_commit, 'project_id': inspection['project_id'],
        'request_id': candidate['request']['request_id'], 'transport': transport,
        'remote_action': 'none', 'warnings': inspection['warnings'],
    }
    if candidate['binding'] is not None and binding.state == 'completed':
        receipt['terminal_outcome'] = binding.terminal['terminal']['outcome']
    output = _check_report_paths(root, pack_id)
    output.mkdir(parents=True, exist_ok=True)
    _write_output(output, 'pull.json', json.dumps(receipt, indent=2, sort_keys=True) + '\n')
    _write_output(output, 'pull.md', '# SYNC pull receipt\n\nGenerated, non-canonical; local acknowledgement only.\n\n'
                  + '```json\n' + json.dumps(receipt, indent=2, sort_keys=True) + '\n```\n')
    return {**receipt, 'status': status, 'repository': repository, 'plan_result': 'ready' if plan else 'not_requested'}
