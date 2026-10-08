from pathlib import Path
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
import subprocess

from . import __version__
from .utils import load_yaml, ID_RE
from .schemas import validate_object_schema, validate_project_schema
from .graph import extract_refs, dependency_cycles
from .objects import DIRS
from .object_loader import load_object_layer
from .skills import inspect_skill_layer
from .source_layer import inspect_source_layer
from .intake_layer import inspect_intake_layer
from .representation_layer import inspect_representation_layer
from .extraction_layer import inspect_extraction_layer
from .rules import inspect_rules_layer, validate_rule_references
from .process_runner import run_process
from .rule_engine import (
    RuleEvaluationContext,
    SUPPORTED_CHECKPOINTS,
    evaluate_rules,
)
from .rule_evidence import RuleEvidence, RuleEvidenceError, build_rule_evidence
from .rule_exceptions import (
    RuleExceptionResolutionError,
    resolve_rule_exceptions,
)

BAD_DEP_STATUSES={'deprecated','removed','rejected','superseded','cancelled'}


@dataclass(frozen=True)
class ProjectValidationReport:
    issues: tuple[tuple[str, str, str], ...]
    rule_evidence: RuleEvidence | None


class _RuleGitIdentityError(RuntimeError):
    pass


def _git_head(root):
    try:
        result=run_process(
            ['git','rev-parse','HEAD'],cwd=root,capture_output=True,text=True,
            check=False,timeout=30,
        )
    except (OSError,subprocess.TimeoutExpired) as exc:
        raise _RuleGitIdentityError(
            f'cannot establish Git HEAD for Rule Evidence: {exc}'
        ) from exc
    if result.returncode:
        reason=result.stderr.strip() or 'git rev-parse HEAD failed'
        raise _RuleGitIdentityError(
            f'cannot establish Git HEAD for Rule Evidence: {reason}'
        )
    return result.stdout.strip()


def _utc_now():
    return datetime.now(timezone.utc)


def _object_layer_is_complete(layer):
    if layer.errors or layer.unsupported_paths:
        return False
    if layer.has_content and not layer.objects:
        return False
    if len(layer.records)!=len(layer.objects):
        return False
    for record in layer.records:
        oid=record.data.get('id')
        if (not isinstance(oid,str) or not ID_RE.fullmatch(oid)
                or record.filename_id!=oid or oid not in layer.objects):
            return False
    return True


def _project_id(cfg):
    project=cfg.get('project') if isinstance(cfg,dict) else None
    oid=project.get('id') if isinstance(project,dict) else None
    return oid if isinstance(oid,str) and oid.strip() else None


def _active_rules(layer):
    registry=layer.rules_registry
    rules=registry.get('rules') if isinstance(registry,dict) else None
    return isinstance(rules,dict) and any(
        isinstance(rule,dict) and rule.get('status')=='active'
        for rule in rules.values()
    )


def _temporary_exception_complete_evaluation_rule_ids(layer):
    """Return Rules whose active temporary waiver requires complete evaluation."""
    rules_registry = layer.rules_registry
    exception_registry = layer.exception_registry
    if not isinstance(rules_registry, dict) or not isinstance(exception_registry, dict):
        return ()

    rules = rules_registry.get("rules")
    exceptions = exception_registry.get("exceptions")
    if not isinstance(rules, dict) or not isinstance(exceptions, dict):
        return ()

    rule_ids = set()
    for exception in exceptions.values():
        if not isinstance(exception, dict):
            continue
        if exception.get("state") != "active" or exception.get("mode") != "temporary":
            continue

        rule_id = exception.get("rule_id")
        rule = rules.get(rule_id)
        if not isinstance(rule, dict):
            continue
        if (
            rule.get("status") != "active"
            or rule.get("exception_policy") != "decision_required"
        ):
            continue

        verification = rule.get("verification")
        if (
            isinstance(verification, dict)
            and verification.get("method") == "deterministic"
            and verification.get("checker")
            in {
                "architecture.dependency_boundary",
                "code.verification",
            }
        ):
            rule_ids.add(rule_id)

    return tuple(sorted(rule_ids))


def _rule_preconditions(layer,reference_issues,cfg,project_schema_messages):
    return (
        not project_schema_messages
        and layer.active
        and isinstance(layer.rules_registry,dict)
        and isinstance(layer.exception_registry,dict)
        and not any(level=='BLOCKING' for level,_,_ in layer.issues)
        and not any(level=='BLOCKING' for level,_,_ in reference_issues)
        and _project_id(cfg) is not None
    )


def _rule_reason(result):
    if isinstance(result.failure_reason,str) and result.failure_reason:
        return result.failure_reason
    reason=result.details.get('reason')
    if isinstance(reason,str) and reason:
        return reason
    if result.raw_status=='PENDING':
        return f'{result.verification_method} verification required'
    return 'rule verification did not complete successfully'


def _rule_issues(results):
    issues=[]
    for result in results:
        status=result.effective_status
        if status in {'PASS','NOT_APPLICABLE'}:
            continue
        if status=='WAIVED':
            issues.append((
                'INFO',result.rule_id,
                f'executable rule WAIVED: raw {result.raw_status} waived by {result.exception_id}',
            ))
            continue
        severity='ERROR' if status=='ERROR' else result.severity
        issues.append((
            severity,result.rule_id,
            f'executable rule {status}: {_rule_reason(result)}',
        ))
    return issues


def validate_report(
    root,
    *,
    rule_checkpoint='project_validate',
    rule_base_commit=None,
    rule_as_of=None,
    rule_evaluation_paths=None,
):
    if rule_checkpoint not in SUPPORTED_CHECKPOINTS:
        raise ValueError(f'unsupported rule checkpoint: {rule_checkpoint!r}')
    issues=[]
    cfg=load_yaml(Path(root)/'project.yaml')
    project_schema_messages=list(validate_project_schema(cfg))
    for m in project_schema_messages: issues.append(('BLOCKING','project.yaml',m))
    source_layer=inspect_source_layer(root,cfg)
    issues.extend(source_layer.issues)
    issues.extend(inspect_intake_layer(root).issues)
    representation_layer=inspect_representation_layer(root,cfg,source_layer)
    issues.extend(representation_layer.issues)
    issues.extend(inspect_extraction_layer(root,cfg,representation_layer).issues)
    issues.extend(inspect_skill_layer(root,cfg).issues)
    rule_layer=inspect_rules_layer(root,cfg)
    issues.extend(rule_layer.issues)
    layer=load_object_layer(root)
    reference_issues=validate_rule_references(rule_layer,layer.objects)
    issues.extend(reference_issues)
    for p in layer.unsupported_paths:
        issues.append(('ERROR',str(p.relative_to(root)),'unsupported atomic object format; use .md with YAML frontmatter and Markdown body'))
    for failure in layer.errors:
        issues.append(('ERROR',str(failure.path.relative_to(root)),str(failure.error)))
    if layer.has_content and not layer.objects:
        issues.append(('ERROR','knowledge','knowledge contains files but no atomic objects were recognized'))
    objects=[(record.path,record.data) for record in layer.records]
    for record in layer.records:
        p,data=record.path,record.data
        expected_dir=DIRS.get(data.get('type'))
        if expected_dir and p.parent.name!=expected_dir:
            issues.append(('ERROR',str(p.relative_to(root)),f'object type {data.get("type")} must live under knowledge/{expected_dir}/'))
        for m in validate_object_schema(data): issues.append(('ERROR',str(p.relative_to(root)),m))
        if record.filename_id is None:
            issues.append(('ERROR',str(p.relative_to(root)),'filename must be ID.md or ID-slug.md with a valid object ID prefix'))
        elif data.get('id') and record.filename_id!=data['id']:
            issues.append(('ERROR',str(p.relative_to(root)),'filename ID prefix does not match object id'))
    object_counts=Counter(d.get('id') for _,d in objects if d.get('id'))
    for oid,n in object_counts.items():
        if n>1: issues.append(('ERROR',oid,'duplicate ID'))
    objmap=layer.objects
    for p,d in objects:
        for ref in extract_refs(d):
            if ref not in objmap: issues.append(('ERROR',d.get('id',str(p)) ,f'broken reference: {ref}'))
        for dep in d.get('depends_on',[]) or []:
            if dep in objmap and objmap[dep]['data'].get('status') in BAD_DEP_STATUSES and d.get('status') in {'active','approved','implemented','shipped','in_progress'}:
                issues.append(('WARNING',d.get('id','?'),f'current object depends on non-current {dep} ({objmap[dep]["data"].get("status")})'))
    for cyc in dependency_cycles(root): issues.append(('ERROR','dependency_graph','cycle: '+' -> '.join(cyc)))
    active_decisions=[d for _,d in objects if d.get('type')=='decision' and d.get('status')=='active']
    if active_decisions and isinstance(cfg,dict) and cfg.get('validation',{}).get('human_approval_checks',True):
        mode=cfg.get('governance_mode','solo')
        if mode=='solo':
            msg='approval metadata is structurally valid only; solo HITL is procedural and cannot be proven by the local validator'
        elif mode=='small_team':
            msg='approval metadata is structurally valid only; enforce required human review in the hosting platform for governed changes'
        else:
            msg='local validator cannot prove hosting-platform human approval; enforce protected branch / required human review externally'
        issues.append(('INFO','governance',msg))

    evidence=None
    if (_rule_preconditions(rule_layer,reference_issues,cfg,project_schema_messages)
            and _active_rules(rule_layer)):
        try:
            git_head=_git_head(root)
        except _RuleGitIdentityError as exc:
            issues.append(('ERROR','rules_evidence',str(exc)))
        else:
            context=RuleEvaluationContext(
                project_root=Path(root),checkpoint=rule_checkpoint,
                objects=layer.objects,
                object_layer_complete=_object_layer_is_complete(layer),
                evaluation_paths=rule_evaluation_paths,
                complete_evaluation_rule_ids=(
                    _temporary_exception_complete_evaluation_rule_ids(rule_layer)
                ),
            )
            results=evaluate_rules(rule_layer.rules_registry,context)
            try:
                exception_resolution=resolve_rule_exceptions(
                    rule_layer.rules_registry,
                    rule_layer.exception_registry,
                    context,
                    results,
                    as_of=_utc_now() if rule_as_of is None else rule_as_of,
                )
            except RuleExceptionResolutionError as exc:
                issues.append((
                    'ERROR','rules_exceptions',
                    f'cannot resolve trustworthy Rule exceptions: {exc}',
                ))
            else:
                try:
                    evidence=build_rule_evidence(
                        project_id=_project_id(cfg),git_head=git_head,
                        base_commit=rule_base_commit,
                        cli_version=__version__,rules_registry=rule_layer.rules_registry,
                        exception_registry=rule_layer.exception_registry,
                        context=context,results=results,
                        exception_resolution=exception_resolution,
                    )
                except RuleEvidenceError as exc:
                    issues.append(('ERROR','rules_evidence',f'cannot build trustworthy Rule Evidence: {exc}'))
                else:
                    issues.extend(_rule_issues(evidence.results))
    return ProjectValidationReport(tuple(issues),evidence)


def validate(root):
    return list(validate_report(root).issues)


def counts(issues):
    c=Counter(x[0] for x in issues)
    return {k:c.get(k,0) for k in ['BLOCKING','ERROR','WARNING','INFO']}
