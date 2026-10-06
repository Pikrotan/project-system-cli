"""Task-local deterministic lifecycle verification using the common Rule pipeline."""

from hashlib import sha256
from pathlib import Path

from yaml import YAMLError

from .rule_evidence import (
    RuleEvidence, RuleEvidenceError, RuleResultEvidence, canonical_sha256, rule_evidence_to_dict,
)
from .skills import _pattern_contains
from .task_baseline import (
    TaskBaselineError, capture_task_state, load_json, load_task_baseline, read_bounded,
    safe_task_path, serialize_json, snapshot_paths, task_delta, task_directory,
    validate_entries, validate_schema, write_task_artifact, MAX_BASELINE_BYTES,
)
from .task_obligations import (
    MAX_OBLIGATIONS_BYTES, TaskObligationsError, build_task_obligations,
    load_task_obligations, serialize_task_obligations,
)
from .task_specification import (
    COMMIT_RE, SHA_RE, MAX_SPEC_BYTES, TaskSpecificationError, _path, load_task_specification,
    task_git_head, task_project_id,
)
from .utils import ID_RE, load_yaml
from .validation import _rule_issues, counts, validate_report

PROFILE = 'project-system-task-verification-v1'
MAX_VERIFICATION_BYTES = 16 * 1024 * 1024
MAX_SKILLS_INPUT_BYTES = 4 * 1024 * 1024


class TaskVerifyError(RuntimeError):
    exit_code = 3
    category = 'integrity/staleness'


class TaskScopeError(TaskVerifyError):
    exit_code = 4
    category = 'scope'


class TaskValidationError(TaskVerifyError):
    exit_code = 5
    category = 'validation'


def _skills_current(root, spec):
    for relative, expected in [('.project/skills.yaml', spec['skills']['registry_sha256']), *(
            (item['path'], item['sha256']) for item in spec['skills']['selected'])]:
        raw = read_bounded(safe_task_path(root, relative), MAX_SKILLS_INPUT_BYTES, 'task Skills input')
        if sha256(raw).hexdigest() != expected:
            raise TaskVerifyError('task Skills registry or selected Skill bytes changed')


def _inputs(root, output, target):
    # Safe path inspection precedes all artifact and configuration reads.
    paths = {name: safe_task_path(root, (output / name).relative_to(root).as_posix())
             for name in ('task-spec.json', 'task-obligations.json', 'task-baseline.json')}
    spec = load_task_specification(paths['task-spec.json'])
    obligations = load_task_obligations(paths['task-obligations.json'])
    baseline = load_task_baseline(paths['task-baseline.json'])
    raw = {name: read_bounded(paths[name], limit, name) for name, limit in (
        ('task-spec.json', MAX_SPEC_BYTES), ('task-obligations.json', MAX_OBLIGATIONS_BYTES),
        ('task-baseline.json', MAX_BASELINE_BYTES))}
    # Loaders and exact digests must refer to the same bytes, not a read-race.
    if (load_task_specification(paths['task-spec.json']) != spec
            or load_task_obligations(paths['task-obligations.json']) != obligations
            or load_task_baseline(paths['task-baseline.json']) != baseline):
        raise TaskVerifyError('task artifacts changed during loading')
    hashes = {name: sha256(value).hexdigest() for name, value in raw.items()}
    if (baseline['task_spec_sha256'] != hashes['task-spec.json']
            or baseline['task_obligations_sha256'] != hashes['task-obligations.json']
            or obligations['task_spec_sha256'] != hashes['task-spec.json']):
        raise TaskVerifyError('task artifact hash binding mismatch')
    config = load_yaml(safe_task_path(root, 'project.yaml'))
    head = task_git_head(root)
    if (task_project_id(config) != spec['project_id'] or spec['target']['id'] != target
            or spec['verification_checkpoint'] != 'task_verify'
            or head != spec['base_commit'] or baseline['base_commit'] != spec['base_commit']):
        raise TaskVerifyError('task project/target/checkpoint/base commit is stale or inconsistent')
    rebuilt = serialize_task_obligations(build_task_obligations(root, paths['task-spec.json'], target))
    if rebuilt.encode('utf-8') != raw['task-obligations.json']:
        raise TaskVerifyError('task definition inputs changed; persisted obligations are stale')
    _skills_current(root, spec)
    # Detect byte changes after loading and after the canonical rebuild too.
    for name, value in raw.items():
        if read_bounded(paths[name], len(value), name) != value:
            raise TaskVerifyError('task artifacts changed during freshness checks')
    return spec, obligations, baseline, hashes, raw


def _state_payload(document):
    return {key: document[key] for key in (
        'base_commit', 'task_spec_sha256', 'task_obligations_sha256', 'task_baseline_sha256',
        'effective_write_scope', 'task_changed_paths', 'task_state')}


def validate_task_verification(document):
    validate_schema(document, 'task-verification.schema.json')
    if type(document['schema_version']) is not int:
        raise TaskBaselineError('invalid task verification version')
    if (not ID_RE.fullmatch(document['target_id'])
            or any(not COMMIT_RE.fullmatch(document[key]) for key in ('base_commit', 'current_head'))
            or any(not SHA_RE.fullmatch(document[key]) for key in (
                'task_spec_sha256', 'task_obligations_sha256', 'task_baseline_sha256',
                'working_tree_fingerprint', 'verification_integrity'))):
        raise TaskBaselineError('invalid exact task verification identity/hash')
    for name in ('obligation_count', 'risk_count'):
        if type(document[name]) is not int:
            raise TaskBaselineError(f'invalid {name}')
    validate_entries(document['task_state'])
    for name, pattern in (('task_changed_paths', False), ('changes_outside_scope', False),
                          ('effective_write_scope', True)):
        paths = document[name]
        if paths != sorted(set(paths)):
            raise TaskBaselineError(f'{name} must be sorted and unique')
        for value in paths:
            _path(value, name, pattern=pattern)
    if [item['path'] for item in document['task_state']] != document['task_changed_paths']:
        raise TaskBaselineError('task delta state/path mismatch')
    outside = [path for path in document['task_changed_paths'] if not any(
        _pattern_contains(pattern, path) for pattern in document['effective_write_scope'])]
    if document['changes_outside_scope'] != outside or document['current_head'] != document['base_commit']:
        raise TaskBaselineError('task verification scope/HEAD contradiction')
    issues = [tuple(item) for item in document['validation']['issues']]
    if any(type(value) is not int for value in document['validation']['counts'].values()):
        raise TaskBaselineError('task validation counts must be integers')
    if document['validation']['counts'] != counts(issues):
        raise TaskBaselineError('task validation counts contradict issues')
    expected = 'SCOPE_FAIL' if outside else ('VALIDATION_FAIL' if any(
        item[0] in {'BLOCKING', 'ERROR'} for item in issues) else 'PASS')
    if document['verification_result'] != expected:
        raise TaskBaselineError('task verification result contradicts scope/validation')
    evidence = document['rule_evidence']
    if evidence is not None:
        # Reuse common Evidence v1 dataclasses and its canonical serializer/fingerprint.
        # This is a persisted projection, not a second engine or Evidence model.
        results = tuple(RuleResultEvidence(**(item | {
            'resolved_scope': tuple(item['resolved_scope']), 'artifacts': tuple(item['artifacts'])
        })) for item in evidence['results'])
        native = RuleEvidence(**(evidence | {'results': results,
                                              'applied_exception_ids': tuple(evidence['applied_exception_ids'])}))
        try:
            if rule_evidence_to_dict(native) != evidence:
                raise TaskBaselineError('nested Rule Evidence is not canonical')
        except RuleEvidenceError as exc:
            raise TaskBaselineError(str(exc)) from exc
        if (evidence['base_commit'] != document['base_commit']
                or evidence['git_head'] != document['current_head'] or evidence['checkpoint'] != 'task_verify'):
            raise TaskBaselineError('nested common Rule Evidence lifecycle mismatch')
        if (any(not SHA_RE.fullmatch(evidence[key]) for key in (
                'rules_registry_sha256', 'exception_registry_sha256',
                'evaluation_context_sha256', 'evidence_fingerprint'))
                or any(not SHA_RE.fullmatch(item.rule_sha256) for item in results)):
            raise TaskBaselineError('nested common Rule Evidence hash is malformed')
        ids = [item.rule_id for item in results]
        if ids != sorted(set(ids)):
            raise TaskBaselineError('nested Rule Evidence results must be sorted and unique')
        if (type(evidence['schema_version']) is not int
                or evidence['applied_exception_ids'] != sorted(set(evidence['applied_exception_ids']))
                or sorted(item.exception_id for item in results if item.exception_id is not None)
                != evidence['applied_exception_ids']):
            raise TaskBaselineError('nested Rule Evidence version/exception contradiction')
        if any(tuple(item) not in issues for item in _rule_issues(results)):
            raise TaskBaselineError('nested Rule Evidence disagrees with validation issues')
        for item in results:
            if ((item.effective_status == 'WAIVED' and (item.raw_status != 'FAIL' or item.exception_id is None))
                    or (item.effective_status != 'WAIVED' and (
                        item.effective_status != item.raw_status or item.exception_id is not None))):
                raise TaskBaselineError('nested Rule Evidence outcome contradiction')
    if canonical_sha256(_state_payload(document)) != document['working_tree_fingerprint']:
        raise TaskBaselineError('task working-tree fingerprint mismatch')
    payload = {key: value for key, value in document.items() if key != 'verification_integrity'}
    if document['verification_integrity'] != canonical_sha256(payload):
        raise TaskBaselineError('task verification integrity mismatch')
    return document


def serialize_task_verification(document):
    validate_task_verification(document)
    text = serialize_json(document)
    if len(text.encode('utf-8')) > MAX_VERIFICATION_BYTES:
        raise TaskBaselineError('Task Verification exceeds size limit')
    return text


def load_task_verification(path):
    return validate_task_verification(load_json(path, MAX_VERIFICATION_BYTES, 'Task Verification'))


def _projection(document):
    return ('# Task Verification (derived projection)\n\n'
            f'- Target: `{document["target_id"]}`\n'
            f'- Deterministic result: `{document["verification_result"]}`\n'
            f'- Checkpoint: `task_verify`\n'
            f'- Working-tree fingerprint: `{document["working_tree_fingerprint"]}`\n'
            f'- Obligations (context only): {document["obligation_count"]}\n'
            f'- Risks (context only): {document["risk_count"]}\n\n'
            'Semantic acceptance verified: **false**. Task completion claimed: **false**.\n'
            'No approval, finalization, staging, commit or push is performed.\n\n'
            '## Task delta\n\n' + ('\n'.join(f'- `{path}`' for path in document['task_changed_paths']) or '_None._')
            + '\n\n## Validation\n\n'
            + ('\n'.join(f'- {severity} {location}: {message}'
                         for severity, location, message in document['validation']['issues']) or '_No issues._') + '\n')


def _verify_task(root, target, budget):
    root = Path(root).absolute()
    output = task_directory(root, target, budget)
    spec, obligations, baseline, hashes, raw = _inputs(root, output, target)
    current = capture_task_state(root, spec['base_commit'])
    changed = task_delta(baseline['entries'], current)
    state = snapshot_paths(root, changed)
    scope = spec['write_scope']['effective']
    outside = [path for path in changed if not any(_pattern_contains(pattern, path) for pattern in scope)]
    common = None
    if not outside:
        try:
            common = validate_report(root, rule_checkpoint='task_verify',
                                     rule_base_commit=spec['base_commit'], rule_evaluation_paths=tuple(changed))
        except Exception:
            # A failed common pipeline is infrastructure failure, not Rule FAIL.
            # No trustworthy report/Evidence exists; do not fabricate either or
            # expose untrusted configuration/checker values in a traceback.
            raise TaskValidationError('common validation pipeline could not produce a trustworthy report') from None
        # Checkers can execute project code. Do not seal a state that changed during validation.
        after = _inputs(root, output, target)
        if (after[4] != raw or capture_task_state(root, spec['base_commit']) != current
                or snapshot_paths(root, changed) != state):
            raise TaskVerifyError('task state changed during validation')
    issues = [list(item) for item in common.issues] if common is not None else []
    result = 'SCOPE_FAIL' if outside else ('VALIDATION_FAIL' if any(
        item[0] in {'BLOCKING', 'ERROR'} for item in issues) else 'PASS')
    document = {
        'schema_version': 1, 'profile': PROFILE, 'target_id': target,
        'base_commit': spec['base_commit'], 'current_head': spec['base_commit'],
        'task_spec_sha256': hashes['task-spec.json'],
        'task_obligations_sha256': hashes['task-obligations.json'],
        'task_baseline_sha256': hashes['task-baseline.json'],
        'verification_checkpoint': 'task_verify', 'task_changed_paths': changed,
        'effective_write_scope': scope, 'changes_outside_scope': outside, 'task_state': state,
        'validation': {'issues': issues, 'counts': counts(issues)},
        'rule_evidence': rule_evidence_to_dict(common.rule_evidence) if common is not None and common.rule_evidence is not None else None,
        'obligation_count': len(obligations['obligations']), 'risk_count': len(obligations['risks']),
        'deterministic_verification': True, 'semantic_acceptance_verified': False,
        'task_completion_claimed': False, 'verification_result': result,
    }
    document['working_tree_fingerprint'] = canonical_sha256(_state_payload(document))
    document['verification_integrity'] = canonical_sha256(document)
    write_task_artifact(root, output / 'task-verification.json', serialize_task_verification(document))
    write_task_artifact(root, output / 'task-verification.md', _projection(document))
    if outside:
        raise TaskScopeError('task changes exceed effective write scope: ' + ', '.join(outside))
    if result != 'PASS':
        raise TaskValidationError('task deterministic validation has BLOCKING/ERROR issues; see task-verification.json')
    return output, document


def verify_task(root, target, budget='medium'):
    try:
        return _verify_task(root, target, budget)
    except TaskVerifyError:
        raise
    except (TaskBaselineError, TaskSpecificationError, TaskObligationsError,
            RuleEvidenceError, OSError, ValueError, YAMLError, RecursionError) as exc:
        raise TaskVerifyError(str(exc)) from exc
