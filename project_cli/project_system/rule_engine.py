"""Pure Executable Rules v1 Stage 2A evaluation core."""

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import rule_checkers


SUPPORTED_CHECKPOINTS = frozenset(
    {"project_validate", "task_verify", "sync_verify"}
)


@dataclass(frozen=True)
class RuleEvaluationContext:
    """Minimal explicit context for Stage 2A checker evaluation."""

    project_root: Path
    checkpoint: str
    objects: Mapping[str, object]
    object_layer_complete: bool = False

    def __post_init__(self):
        object.__setattr__(self, "project_root", Path(self.project_root))


@dataclass(frozen=True)
class RuleEvaluationResult:
    """One raw active-rule result without exceptions or persisted evidence."""

    rule_id: str
    verification_method: str
    checker: str | None
    checker_version: str | None
    severity: str
    checkpoint: str
    resolved_scope: tuple[str, ...]
    raw_status: str
    details: dict[str, Any]
    failure_reason: str | None


def _result(
    *,
    rule_id,
    method,
    checker,
    checker_version,
    severity,
    context,
    scope,
    status,
    details=None,
    failure_reason=None,
):
    return RuleEvaluationResult(
        rule_id=rule_id,
        verification_method=method,
        checker=checker,
        checker_version=checker_version,
        severity=severity,
        checkpoint=context.checkpoint,
        resolved_scope=scope,
        raw_status=status,
        details=dict(details or {}),
        failure_reason=failure_reason,
    )


def evaluate_rules(rules_registry, context):
    """Evaluate active rules in stable Rule-ID order.

    Draft and deprecated definitions are omitted rather than reported as PASS.
    Stage 2A copies ``scope.paths`` into ``resolved_scope`` (an empty tuple means
    project-wide) but intentionally does not interpret scope as task-aware
    applicability. Checker-specific targets come only from validated parameters.
    """
    if context.checkpoint not in SUPPORTED_CHECKPOINTS:
        raise ValueError(f"unsupported rule checkpoint: {context.checkpoint!r}")
    if not isinstance(rules_registry, Mapping):
        raise TypeError("rules registry must be a mapping")
    rules = rules_registry.get("rules")
    if not isinstance(rules, Mapping):
        raise ValueError("rules registry must contain a rules mapping")

    results = []
    for rule_id in sorted(rules):
        rule = rules[rule_id]
        if rule["status"] != "active":
            continue

        verification = rule["verification"]
        method = verification["method"]
        enforcement = rule["enforcement"]
        severity = enforcement["severity"]
        scope = tuple((rule.get("scope") or {}).get("paths", ()))
        checker = verification.get("checker") if method == "deterministic" else None
        spec = rule_checkers.CHECKER_REGISTRY.get(checker) if checker else None
        checker_version = spec.version if spec is not None else None

        if context.checkpoint not in enforcement["checkpoints"]:
            results.append(
                _result(
                    rule_id=rule_id,
                    method=method,
                    checker=checker,
                    checker_version=checker_version,
                    severity=severity,
                    context=context,
                    scope=scope,
                    status="NOT_APPLICABLE",
                    details={"reason": "checkpoint_not_configured"},
                )
            )
            continue

        if method == "deterministic" and spec is None:
            results.append(
                _result(
                    rule_id=rule_id,
                    method=method,
                    checker=checker,
                    checker_version=None,
                    severity=severity,
                    context=context,
                    scope=scope,
                    status="ERROR",
                    details={"checker": checker},
                    failure_reason=f"unknown deterministic checker: {checker!r}",
                )
            )
            continue

        if method in {"ai", "human"}:
            results.append(
                _result(
                    rule_id=rule_id,
                    method=method,
                    checker=None,
                    checker_version=None,
                    severity=severity,
                    context=context,
                    scope=scope,
                    status="PENDING",
                    details={"reason": f"{method}_verification_required"},
                )
            )
            continue

        if method != "deterministic":
            results.append(
                _result(
                    rule_id=rule_id,
                    method=method,
                    checker=checker,
                    checker_version=checker_version,
                    severity=severity,
                    context=context,
                    scope=scope,
                    status="ERROR",
                    failure_reason=f"unsupported verification method: {method!r}",
                )
            )
            continue

        try:
            outcome = rule_checkers.evaluate_checker(
                checker,
                context,
                verification.get("parameters"),
            )
        except Exception as exc:
            outcome = rule_checkers.CheckerOutcome(
                "ERROR",
                {"checker": checker},
                f"checker raised {type(exc).__name__}: {exc}",
            )

        results.append(
            _result(
                rule_id=rule_id,
                method=method,
                checker=checker,
                checker_version=checker_version,
                severity=severity,
                context=context,
                scope=scope,
                status=outcome.raw_status,
                details=outcome.details,
                failure_reason=outcome.failure_reason,
            )
        )
    return results
