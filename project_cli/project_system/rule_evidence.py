"""Deterministic in-memory Rule Evidence v1 derived from Rule Engine results.

Repository checkers inspect the filesystem under ``project_root``.  Stage 3
deliberately binds that evaluation through Git identity plus normalized result
details; it does not attempt to fingerprint the complete working tree.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
import json
import math
from pathlib import Path, PurePath
import re
from typing import Any

from . import rule_checkers
from .rule_engine import (
    RuleEvaluationContext,
    RuleEvaluationResult,
    SUPPORTED_CHECKPOINTS,
)


EVIDENCE_SCHEMA_VERSION = 1
RAW_STATUSES = frozenset(
    {"PASS", "FAIL", "ERROR", "NOT_APPLICABLE", "PENDING"}
)
_GIT_SHA1_RE = re.compile(r"^[0-9a-f]{40}$")


class RuleEvidenceError(RuntimeError):
    """A supplied value cannot form canonical, trustworthy Rule Evidence."""


@dataclass(frozen=True)
class RuleResultEvidence:
    """Evidence for one evaluated active Rule before exception resolution."""

    rule_id: str
    rule_sha256: str
    verification_method: str
    checker: str | None
    checker_version: str | None
    severity: str
    resolved_scope: tuple[str, ...]
    raw_status: str
    effective_status: str
    details: dict[str, Any]
    artifacts: tuple[Any, ...]
    exception_id: str | None
    failure_reason: str | None


@dataclass(frozen=True)
class RuleEvidence:
    """One deterministic Rule Evidence v1 run."""

    schema_version: int
    project_id: str
    git_head: str
    base_commit: str | None
    cli_version: str
    checkpoint: str
    rules_registry_sha256: str
    exception_registry_sha256: str
    evaluation_context_sha256: str
    results: tuple[RuleResultEvidence, ...]
    applied_exception_ids: tuple[str, ...]
    evidence_fingerprint: str


def _normalize_json(value, *, location="value"):
    if value is None or type(value) in {bool, int, str}:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise RuleEvidenceError(
                f"{location} contains a non-finite float"
            )
        return value
    if isinstance(value, PurePath):
        return value.as_posix()
    if isinstance(value, Mapping):
        items = []
        for key, item in value.items():
            if not isinstance(key, str):
                raise RuleEvidenceError(
                    f"{location} contains a non-string mapping key"
                )
            items.append((key, item))
        return {
            key: _normalize_json(item, location=f"{location}.{key}")
            for key, item in sorted(items, key=lambda pair: pair[0])
        }
    if isinstance(value, (list, tuple)):
        return [
            _normalize_json(item, location=f"{location}[{index}]")
            for index, item in enumerate(value)
        ]
    raise RuleEvidenceError(
        f"{location} contains unsupported value type: {type(value).__name__}"
    )


def _canonical_json(value):
    normalized = _normalize_json(value)
    try:
        return json.dumps(
            normalized,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise RuleEvidenceError(f"value is not canonical JSON: {exc}") from exc


def canonical_sha256(value):
    """Return a lowercase SHA-256 digest of canonical semantic JSON."""
    return sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _require_nonempty_string(value, label):
    if not isinstance(value, str) or not value.strip():
        raise RuleEvidenceError(f"{label} must be a non-empty string")


def _require_commit(value, label, *, optional=False):
    if optional and value is None:
        return
    if not isinstance(value, str) or not _GIT_SHA1_RE.fullmatch(value):
        raise RuleEvidenceError(
            f"{label} must be a full lowercase 40-character Git SHA-1"
        )


def _require_mapping(value, label):
    if not isinstance(value, Mapping):
        raise RuleEvidenceError(f"{label} must be a mapping/object")
    return value


def _rule_scope(rule, rule_id):
    scope = rule.get("scope")
    if scope is None:
        return ()
    scope = _require_mapping(scope, f"rules.{rule_id}.scope")
    paths = scope.get("paths")
    if not isinstance(paths, list) or not all(
        isinstance(path, str) for path in paths
    ):
        raise RuleEvidenceError(
            f"rules.{rule_id}.scope.paths must be a list of strings"
        )
    return tuple(paths)


def _normalize_context_value(value, *, project_root, location):
    if isinstance(value, PurePath):
        path = Path(value)
        root = Path(project_root)

        if path.is_absolute():
            if not root.is_absolute():
                raise RuleEvidenceError(
                    f"{location} contains an absolute path but project_root is relative"
                )
            try:
                path = path.relative_to(root)
            except ValueError as exc:
                raise RuleEvidenceError(
                    f"{location} contains an absolute path outside project_root"
                ) from exc

        return path.as_posix()

    if isinstance(value, Mapping):
        items = []
        for key, item in value.items():
            if not isinstance(key, str):
                raise RuleEvidenceError(
                    f"{location} contains a non-string mapping key"
                )
            items.append((key, item))
        return {
            key: _normalize_context_value(
                item,
                project_root=project_root,
                location=f"{location}.{key}",
            )
            for key, item in sorted(items, key=lambda pair: pair[0])
        }

    if isinstance(value, (list, tuple)):
        return [
            _normalize_context_value(
                item,
                project_root=project_root,
                location=f"{location}[{index}]",
            )
            for index, item in enumerate(value)
        ]

    return _normalize_json(value, location=location)


def _context_payload(context):
    if not isinstance(context, RuleEvaluationContext):
        raise RuleEvidenceError("context must be a RuleEvaluationContext")
    if context.checkpoint not in SUPPORTED_CHECKPOINTS:
        raise RuleEvidenceError(
            f"context checkpoint is unsupported: {context.checkpoint!r}"
        )
    if type(context.object_layer_complete) is not bool:
        raise RuleEvidenceError("context object_layer_complete must be a boolean")
    _require_mapping(context.objects, "context objects")
    return _normalize_context_value(
        {
            "checkpoint": context.checkpoint,
            "object_layer_complete": context.object_layer_complete,
            "objects": context.objects,
        },
        project_root=context.project_root,
        location="evaluation_context",
    )


def _definition_contract(rule, rule_id):
    rule = _require_mapping(rule, f"rules.{rule_id}")
    if rule.get("status") != "active":
        raise RuleEvidenceError(
            f"rules.{rule_id} must be active to have evaluation evidence"
        )

    verification = _require_mapping(
        rule.get("verification"), f"rules.{rule_id}.verification"
    )
    enforcement = _require_mapping(
        rule.get("enforcement"), f"rules.{rule_id}.enforcement"
    )
    method = verification.get("method")
    if method not in {"deterministic", "ai", "human"}:
        raise RuleEvidenceError(
            f"rules.{rule_id}.verification.method is unsupported"
        )
    severity = enforcement.get("severity")
    if not isinstance(severity, str):
        raise RuleEvidenceError(
            f"rules.{rule_id}.enforcement.severity must be a string"
        )

    checkpoints = enforcement.get("checkpoints")
    if not isinstance(checkpoints, list) or not all(
        isinstance(checkpoint, str) for checkpoint in checkpoints
    ):
        raise RuleEvidenceError(
            f"rules.{rule_id}.enforcement.checkpoints must be a list of strings"
        )

    return (
        rule,
        verification,
        method,
        severity,
        tuple(checkpoints),
        _rule_scope(rule, rule_id),
    )


def _result_evidence(raw_result, rules, context):
    if not isinstance(raw_result, RuleEvaluationResult):
        raise RuleEvidenceError(
            "results must contain RuleEvaluationResult values"
        )
    rule_id = raw_result.rule_id
    if not isinstance(rule_id, str) or not rule_id:
        raise RuleEvidenceError("result rule_id must be a non-empty string")
    if rule_id not in rules:
        raise RuleEvidenceError(f"result references unknown rule_id: {rule_id}")

    (
        rule,
        verification,
        method,
        severity,
        checkpoints,
        scope,
    ) = _definition_contract(rules[rule_id], rule_id)
    if raw_result.verification_method != method:
        raise RuleEvidenceError(
            f"result verification_method mismatch for {rule_id}"
        )
    if raw_result.severity != severity:
        raise RuleEvidenceError(f"result severity mismatch for {rule_id}")
    if raw_result.checkpoint != context.checkpoint:
        raise RuleEvidenceError(f"result checkpoint mismatch for {rule_id}")
    if (
        not isinstance(raw_result.resolved_scope, tuple)
        or not all(isinstance(path, str) for path in raw_result.resolved_scope)
        or raw_result.resolved_scope != scope
    ):
        raise RuleEvidenceError(f"result resolved_scope mismatch for {rule_id}")

    if method == "deterministic":
        expected_checker = verification.get("checker")
        if not isinstance(expected_checker, str) or not expected_checker:
            raise RuleEvidenceError(
                f"rules.{rule_id}.verification.checker is malformed"
            )
        if raw_result.checker != expected_checker:
            raise RuleEvidenceError(f"result checker mismatch for {rule_id}")
        checker_spec = rule_checkers.CHECKER_REGISTRY.get(expected_checker)
        if checker_spec is not None:
            if raw_result.checker_version != checker_spec.version:
                raise RuleEvidenceError(
                    f"result checker_version mismatch for {rule_id}"
                )
        elif raw_result.checker_version is not None:
            raise RuleEvidenceError(
                f"unknown checker version must be absent for {rule_id}"
            )
    elif raw_result.checker is not None or raw_result.checker_version is not None:
        raise RuleEvidenceError(
            f"result checker and checker_version must be absent for {rule_id}"
        )

    if raw_result.raw_status not in RAW_STATUSES:
        raise RuleEvidenceError(f"result raw_status is invalid for {rule_id}")

    if context.checkpoint not in checkpoints:
        if raw_result.raw_status != "NOT_APPLICABLE":
            raise RuleEvidenceError(
                f"result raw_status mismatch for non-applicable checkpoint on {rule_id}"
            )
    elif method in {"ai", "human"}:
        if raw_result.raw_status != "PENDING":
            raise RuleEvidenceError(
                f"result raw_status mismatch for {method} verification on {rule_id}"
            )
    elif raw_result.raw_status == "PENDING":
        raise RuleEvidenceError(
            f"result raw_status mismatch for deterministic verification on {rule_id}"
        )

    if not isinstance(raw_result.details, Mapping):
        raise RuleEvidenceError(f"result details must be a mapping for {rule_id}")
    details = _normalize_json(
        raw_result.details,
        location=f"results.{rule_id}.details",
    )
    if not isinstance(details, dict):
        raise RuleEvidenceError(f"result details must be an object for {rule_id}")
    if raw_result.failure_reason is not None and not isinstance(
        raw_result.failure_reason, str
    ):
        raise RuleEvidenceError(
            f"result failure_reason must be a string or null for {rule_id}"
        )

    return RuleResultEvidence(
        rule_id=rule_id,
        rule_sha256=canonical_sha256(rule),
        verification_method=method,
        checker=raw_result.checker,
        checker_version=raw_result.checker_version,
        severity=severity,
        resolved_scope=scope,
        raw_status=raw_result.raw_status,
        effective_status=raw_result.raw_status,
        details=details,
        artifacts=(),
        exception_id=None,
        failure_reason=raw_result.failure_reason,
    )


def _result_to_dict(result):
    return {
        "rule_id": result.rule_id,
        "rule_sha256": result.rule_sha256,
        "verification_method": result.verification_method,
        "checker": result.checker,
        "checker_version": result.checker_version,
        "severity": result.severity,
        "resolved_scope": list(result.resolved_scope),
        "raw_status": result.raw_status,
        "effective_status": result.effective_status,
        "details": _normalize_json(
            result.details, location=f"results.{result.rule_id}.details"
        ),
        "artifacts": list(result.artifacts),
        "exception_id": result.exception_id,
        "failure_reason": result.failure_reason,
    }


def _run_payload(
    *,
    project_id,
    git_head,
    base_commit,
    cli_version,
    checkpoint,
    rules_registry_sha256,
    exception_registry_sha256,
    evaluation_context_sha256,
    results,
    applied_exception_ids,
):
    return {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "project_id": project_id,
        "git_head": git_head,
        "base_commit": base_commit,
        "cli_version": cli_version,
        "checkpoint": checkpoint,
        "rules_registry_sha256": rules_registry_sha256,
        "exception_registry_sha256": exception_registry_sha256,
        "evaluation_context_sha256": evaluation_context_sha256,
        "results": [_result_to_dict(item) for item in results],
        "applied_exception_ids": list(applied_exception_ids),
    }


def build_rule_evidence(
    *,
    project_id,
    git_head,
    cli_version,
    rules_registry,
    exception_registry,
    context,
    results,
    base_commit=None,
):
    """Build deterministic Evidence v1 without I/O or checker re-evaluation."""
    _require_nonempty_string(project_id, "project_id")
    _require_commit(git_head, "git_head")
    _require_commit(base_commit, "base_commit", optional=True)
    _require_nonempty_string(cli_version, "cli_version")

    rules_registry = _require_mapping(rules_registry, "rules_registry")
    exception_registry = _require_mapping(
        exception_registry, "exception_registry"
    )
    rules = _require_mapping(rules_registry.get("rules"), "rules_registry.rules")
    context_payload = _context_payload(context)
    if not isinstance(results, Sequence) or isinstance(results, (str, bytes)):
        raise RuleEvidenceError("results must be a sequence")

    evidence_results = []
    seen_rule_ids = set()
    for raw_result in results:
        item = _result_evidence(raw_result, rules, context)
        if item.rule_id in seen_rule_ids:
            raise RuleEvidenceError(
                f"duplicate RuleEvaluationResult for rule_id: {item.rule_id}"
            )
        seen_rule_ids.add(item.rule_id)
        evidence_results.append(item)
    evidence_results.sort(key=lambda item: item.rule_id)
    evidence_results = tuple(evidence_results)

    rules_hash = canonical_sha256(rules_registry)
    exceptions_hash = canonical_sha256(exception_registry)
    context_hash = canonical_sha256(context_payload)
    payload = _run_payload(
        project_id=project_id,
        git_head=git_head,
        base_commit=base_commit,
        cli_version=cli_version,
        checkpoint=context.checkpoint,
        rules_registry_sha256=rules_hash,
        exception_registry_sha256=exceptions_hash,
        evaluation_context_sha256=context_hash,
        results=evidence_results,
        applied_exception_ids=(),
    )
    fingerprint = canonical_sha256(payload)
    return RuleEvidence(
        schema_version=EVIDENCE_SCHEMA_VERSION,
        project_id=project_id,
        git_head=git_head,
        base_commit=base_commit,
        cli_version=cli_version,
        checkpoint=context.checkpoint,
        rules_registry_sha256=rules_hash,
        exception_registry_sha256=exceptions_hash,
        evaluation_context_sha256=context_hash,
        results=evidence_results,
        applied_exception_ids=(),
        evidence_fingerprint=fingerprint,
    )


def rule_evidence_to_dict(evidence):
    """Return the Evidence v1 contract using JSON-native values only."""
    if not isinstance(evidence, RuleEvidence):
        raise RuleEvidenceError("evidence must be a RuleEvidence value")
    payload = _run_payload(
        project_id=evidence.project_id,
        git_head=evidence.git_head,
        base_commit=evidence.base_commit,
        cli_version=evidence.cli_version,
        checkpoint=evidence.checkpoint,
        rules_registry_sha256=evidence.rules_registry_sha256,
        exception_registry_sha256=evidence.exception_registry_sha256,
        evaluation_context_sha256=evidence.evaluation_context_sha256,
        results=evidence.results,
        applied_exception_ids=evidence.applied_exception_ids,
    )
    expected_fingerprint = canonical_sha256(payload)
    if evidence.evidence_fingerprint != expected_fingerprint:
        raise RuleEvidenceError(
            "evidence fingerprint does not match current evidence payload"
        )

    payload["evidence_fingerprint"] = evidence.evidence_fingerprint
    return _normalize_json(payload, location="evidence")


def rule_evidence_json(evidence):
    """Serialize Evidence v1 as canonical deterministic JSON."""
    return _canonical_json(rule_evidence_to_dict(evidence))
