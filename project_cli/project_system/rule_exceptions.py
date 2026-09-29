"""Pure governed exception resolution for Executable Rules v1.

The resolver consumes already loaded registries, raw Rule Engine results, and an
explicit clock value.  It performs no I/O and never changes raw results.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePath
import re

from .rule_engine import RuleEvaluationContext, RuleEvaluationResult
from .rule_scope import (
    RuleScopeError,
    canonical_rule_path,
    scope_pattern_matches as _shared_scope_pattern_matches,
)


_RFC3339_DATETIME_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)


class RuleExceptionResolutionError(RuntimeError):
    """Explicit inputs cannot produce a trustworthy exception resolution."""


@dataclass(frozen=True)
class RuleExceptionApplication:
    rule_id: str
    exception_id: str


@dataclass(frozen=True)
class RuleExceptionResolution:
    applications: tuple[RuleExceptionApplication, ...]


def _mapping(value, label):
    if not isinstance(value, Mapping):
        raise RuleExceptionResolutionError(f"{label} must be a mapping/object")
    return value


def _nonempty_string(value, label):
    if not isinstance(value, str) or not value or value != value.strip():
        raise RuleExceptionResolutionError(f"{label} must be a non-empty string")
    return value


def _parse_datetime(value, label):
    _nonempty_string(value, label)
    if not _RFC3339_DATETIME_RE.fullmatch(value):
        raise RuleExceptionResolutionError(
            f"{label} must be an RFC3339 date-time"
        )
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise RuleExceptionResolutionError(
            f"{label} must be an RFC3339 date-time"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RuleExceptionResolutionError(
            f"{label} must include an explicit timezone"
        )
    return parsed.astimezone(timezone.utc)


def _canonical_path(value, label, *, pattern):
    try:
        return canonical_rule_path(value, label, pattern=pattern)
    except RuleScopeError as exc:
        raise RuleExceptionResolutionError(str(exc)) from exc


def scope_pattern_matches(pattern, path):
    """Match one anchored repository path with segment-aware v1 glob syntax."""
    try:
        return _shared_scope_pattern_matches(
            pattern,
            path,
            path_label="violation path",
        )
    except RuleScopeError as exc:
        raise RuleExceptionResolutionError(str(exc)) from exc


def _object_path(context, object_id, record):
    record = _mapping(record, f"context.objects.{object_id}")
    raw_path = record.get("path")
    if not isinstance(raw_path, (str, PurePath)):
        raise RuleExceptionResolutionError(
            f"context.objects.{object_id}.path must be a path"
        )
    path = Path(raw_path)
    root = Path(context.project_root)
    if path.is_absolute():
        try:
            relative = path.relative_to(root)
        except ValueError as exc:
            raise RuleExceptionResolutionError(
                f"context.objects.{object_id}.path is outside project_root"
            ) from exc
        value = relative.as_posix()
    else:
        value = path.as_posix()
    return _canonical_path(
        value,
        f"context.objects.{object_id}.path",
        pattern=False,
    )


def _violation_paths(result, context):
    details = _mapping(result.details, f"results.{result.rule_id}.details")
    if result.checker in {
        "repository.required_path",
        "repository.forbidden_path",
    }:
        return (
            _canonical_path(
                details.get("path"),
                f"results.{result.rule_id}.details.path",
                pattern=False,
            ),
        )
    if result.checker == "knowledge.required_field":
        object_ids = details.get("violating_object_ids")
        if (
            not isinstance(object_ids, Sequence)
            or isinstance(object_ids, (str, bytes))
            or not object_ids
        ):
            raise RuleExceptionResolutionError(
                f"results.{result.rule_id}.details.violating_object_ids "
                "must be a non-empty sequence"
            )
        seen = set()
        paths = []
        objects = _mapping(context.objects, "context.objects")
        for index, object_id in enumerate(object_ids):
            _nonempty_string(
                object_id,
                f"results.{result.rule_id}.details.violating_object_ids[{index}]",
            )
            if object_id in seen:
                raise RuleExceptionResolutionError(
                    f"results.{result.rule_id} contains duplicate violating object ID"
                )
            seen.add(object_id)
            if object_id not in objects:
                raise RuleExceptionResolutionError(
                    f"results.{result.rule_id} references missing violating object: "
                    f"{object_id}"
                )
            paths.append(_object_path(context, object_id, objects[object_id]))
        return tuple(sorted(paths))
    if result.checker == "architecture.dependency_boundary":
        violations = details.get("violations")
        if (
            not isinstance(violations, Sequence)
            or isinstance(violations, (str, bytes))
            or not violations
        ):
            raise RuleExceptionResolutionError(
                f"results.{result.rule_id}.details.violations must be a non-empty sequence"
            )
        normalized = []
        expected_fields = {
            "source_path",
            "directive",
            "target_path",
            "target_uri",
        }
        for index, violation in enumerate(violations):
            label = f"results.{result.rule_id}.details.violations[{index}]"
            item = _mapping(violation, label)
            if set(item) != expected_fields:
                raise RuleExceptionResolutionError(
                    f"{label} must contain exactly source_path, directive, "
                    "target_path, and target_uri"
                )
            source = _canonical_path(
                item.get("source_path"),
                f"{label}.source_path",
                pattern=False,
            )
            target = _canonical_path(
                item.get("target_path"),
                f"{label}.target_path",
                pattern=False,
            )
            directive = _nonempty_string(
                item.get("directive"), f"{label}.directive"
            )
            target_uri = _nonempty_string(
                item.get("target_uri"), f"{label}.target_uri"
            )
            normalized.append((source, directive, target, target_uri))
        if normalized != sorted(set(normalized)):
            raise RuleExceptionResolutionError(
                f"results.{result.rule_id}.details.violations must be sorted "
                "and deduplicated"
            )
        return tuple(sorted({item[0] for item in normalized}))
    raise RuleExceptionResolutionError(
        f"results.{result.rule_id} uses unsupported checker for exception scope: "
        f"{result.checker!r}"
    )


def _validated_exceptions(exception_registry, rules, context):
    exceptions = _mapping(
        exception_registry.get("exceptions"),
        "exception_registry.exceptions",
    )
    objects = _mapping(context.objects, "context.objects")
    validated = []
    exception_ids = list(exceptions)
    for exception_id in exception_ids:
        _nonempty_string(exception_id, "exception_id")
    for exception_id in sorted(exception_ids):
        value = _mapping(
            exceptions[exception_id],
            f"exceptions.{exception_id}",
        )
        rule_id = _nonempty_string(
            value.get("rule_id"), f"exceptions.{exception_id}.rule_id"
        )
        if rule_id not in rules or not isinstance(rules[rule_id], Mapping):
            raise RuleExceptionResolutionError(
                f"exceptions.{exception_id} references unknown rule_id: {rule_id}"
            )
        state = value.get("state")
        if state not in {"active", "revoked"}:
            raise RuleExceptionResolutionError(
                f"exceptions.{exception_id}.state is invalid"
            )
        mode = value.get("mode")
        if mode not in {"temporary", "permanent"}:
            raise RuleExceptionResolutionError(
                f"exceptions.{exception_id}.mode is invalid"
            )
        _nonempty_string(
            value.get("approved_by"),
            f"exceptions.{exception_id}.approved_by",
        )
        _parse_datetime(
            value.get("approved_at"),
            f"exceptions.{exception_id}.approved_at",
        )
        decision_id = _nonempty_string(
            value.get("decision_id"),
            f"exceptions.{exception_id}.decision_id",
        )
        decision = objects.get(decision_id)
        if not isinstance(decision, Mapping):
            raise RuleExceptionResolutionError(
                f"exceptions.{exception_id} references missing decision: {decision_id}"
            )
        decision_data = decision.get("data")
        if (
            not isinstance(decision_data, Mapping)
            or decision_data.get("type") != "decision"
        ):
            raise RuleExceptionResolutionError(
                f"exceptions.{exception_id} decision_id is not a canonical decision: "
                f"{decision_id}"
            )
        scope = _mapping(value.get("scope"), f"exceptions.{exception_id}.scope")
        scope_paths = scope.get("paths")
        if (
            not isinstance(scope_paths, Sequence)
            or isinstance(scope_paths, (str, bytes))
            or not scope_paths
        ):
            raise RuleExceptionResolutionError(
                f"exceptions.{exception_id}.scope.paths must be a non-empty sequence"
            )
        patterns = tuple(
            _canonical_path(
                item,
                f"exceptions.{exception_id}.scope.paths[{index}]",
                pattern=True,
            )
            for index, item in enumerate(scope_paths)
        )
        if len(set(patterns)) != len(patterns):
            raise RuleExceptionResolutionError(
                f"exceptions.{exception_id}.scope.paths contains duplicates"
            )
        expires_at = None
        if mode == "temporary":
            expires_at = _parse_datetime(
                value.get("expires_at"),
                f"exceptions.{exception_id}.expires_at",
            )
        elif "expires_at" in value:
            raise RuleExceptionResolutionError(
                f"exceptions.{exception_id}.expires_at is invalid for permanent mode"
            )
        validated.append(
            (exception_id, rule_id, state, patterns, expires_at)
        )
    return tuple(validated)


def resolve_rule_exceptions(
    rules_registry,
    exception_registry,
    context,
    results,
    *,
    as_of,
):
    """Resolve eligible raw FAIL results to exception applications.

    ``as_of`` is mandatory and timezone-aware so expiry decisions are stable and
    testable.  More than one applicable exception for one result is an error.
    """
    if (
        not isinstance(as_of, datetime)
        or as_of.tzinfo is None
        or as_of.utcoffset() is None
    ):
        raise RuleExceptionResolutionError(
            "as_of must be a timezone-aware datetime"
        )
    as_of = as_of.astimezone(timezone.utc)
    if not isinstance(context, RuleEvaluationContext):
        raise RuleExceptionResolutionError(
            "context must be a RuleEvaluationContext"
        )
    rules_registry = _mapping(rules_registry, "rules_registry")
    exception_registry = _mapping(exception_registry, "exception_registry")
    rules = _mapping(rules_registry.get("rules"), "rules_registry.rules")
    if not isinstance(results, Sequence) or isinstance(results, (str, bytes)):
        raise RuleExceptionResolutionError("results must be a sequence")

    validated_exceptions = _validated_exceptions(
        exception_registry,
        rules,
        context,
    )
    applications = []
    seen_rule_ids = set()
    for index, result in enumerate(results):
        if not isinstance(result, RuleEvaluationResult):
            raise RuleExceptionResolutionError(
                f"results[{index}] must be a RuleEvaluationResult"
            )
        _nonempty_string(result.rule_id, f"results[{index}].rule_id")
        if result.rule_id in seen_rule_ids:
            raise RuleExceptionResolutionError(
                f"duplicate RuleEvaluationResult for rule_id: {result.rule_id}"
            )
        seen_rule_ids.add(result.rule_id)
        rule_definition = rules.get(result.rule_id)
        if not isinstance(rule_definition, Mapping):
            raise RuleExceptionResolutionError(
                f"result references unknown rule_id: {result.rule_id}"
            )
        if result.raw_status != "FAIL":
            continue
        if rule_definition.get("status") != "active":
            raise RuleExceptionResolutionError(
                f"result references non-active rule: {result.rule_id}"
            )
        if rule_definition.get("exception_policy") != "decision_required":
            continue

        violation_paths = _violation_paths(result, context)
        matching = []
        for exception_id, rule_id, state, patterns, expires_at in validated_exceptions:
            if rule_id != result.rule_id or state != "active":
                continue
            if expires_at is not None and as_of >= expires_at:
                continue
            if all(
                any(scope_pattern_matches(pattern, path) for pattern in patterns)
                for path in violation_paths
            ):
                matching.append(exception_id)
        if len(matching) > 1:
            raise RuleExceptionResolutionError(
                f"multiple applicable exceptions for rule {result.rule_id}: "
                + ", ".join(matching)
            )
        if matching:
            applications.append(
                RuleExceptionApplication(result.rule_id, matching[0])
            )

    applications.sort(key=lambda item: (item.rule_id, item.exception_id))
    return RuleExceptionResolution(tuple(applications))
