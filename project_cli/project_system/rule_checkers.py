"""Allowlisted deterministic checkers for Executable Rules v1 Stage 2A."""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import os
from pathlib import Path, PurePosixPath
import re
import stat
from types import MappingProxyType
from typing import Any

from .object_loader import TYPE_DIRECTORIES
from .rule_scope import RuleScopeError, canonical_rule_path, scope_pattern_matches


CHECKER_VERSION = "1"
_FIELD_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")
_GLOB_CHARACTERS = frozenset("*?[]")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class CheckerOutcome:
    """One raw checker outcome; exception resolution is intentionally absent."""

    raw_status: str
    details: dict[str, Any]
    failure_reason: str | None = None


@dataclass(frozen=True)
class CheckerSpec:
    """Packaged checker identity, contract validator, and implementation."""

    checker_id: str
    version: str
    parameter_validator: Callable[[object], tuple[str, ...]]
    implementation: Callable[[object, Mapping[str, object]], CheckerOutcome]
    outcome_validator: Callable[[str, object, object], tuple[str, ...]] | None = None


class _FilesystemInspectionError(RuntimeError):
    pass


def _parameter_shape_messages(parameters, *, required, allowed):
    if parameters is None:
        parameters = {}
    elif not isinstance(parameters, Mapping):
        return ("parameters must be a mapping/object",), None

    messages = []
    for name in sorted(set(parameters) - set(allowed)):
        messages.append(f"unknown parameter: {name}")
    for name in required:
        if name not in parameters:
            messages.append(f"missing required parameter: {name}")
    return tuple(messages), parameters


def _safe_repository_path(value):
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or "\\" in value
        or ":" in value
        or "\x00" in value
        or any(character in value for character in _GLOB_CHARACTERS)
    ):
        return False

    pure = PurePosixPath(value)
    if (
        pure.is_absolute()
        or not pure.parts
        or any(part in {"", ".", ".."} for part in pure.parts)
        or pure.parts[0].casefold() == ".git"
    ):
        return False
    return pure.as_posix() == value


def _repository_parameter_messages(parameters):
    messages, normalized = _parameter_shape_messages(
        parameters,
        required=("path",),
        allowed=("path",),
    )
    messages = list(messages)
    if normalized is not None and "path" in normalized:
        path = normalized["path"]
        if not _safe_repository_path(path):
            messages.append(f"unsafe repository path parameter: {path!r}")
    return tuple(messages)


def _knowledge_parameter_messages(parameters):
    messages, normalized = _parameter_shape_messages(
        parameters,
        required=("object_type", "field"),
        allowed=("object_type", "field"),
    )
    messages = list(messages)
    if normalized is None:
        return tuple(messages)

    if "object_type" in normalized:
        object_type = normalized["object_type"]
        if (
            not isinstance(object_type, str)
            or not object_type
            or object_type != object_type.strip()
            or object_type not in TYPE_DIRECTORIES
        ):
            messages.append(
                f"object_type must be a canonical knowledge object type: {object_type!r}"
            )

    if "field" in normalized:
        field = normalized["field"]
        if (
            not isinstance(field, str)
            or not field
            or field != field.strip()
            or not _FIELD_NAME_RE.fullmatch(field)
        ):
            messages.append(
                "field must be a top-level field name without dotted/nested expressions"
            )
    return tuple(messages)


def _pattern_sequence_messages(value, label):
    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes))
        or not value
    ):
        return (f"{label} must be a non-empty sequence",)
    messages = []
    normalized = []
    for index, pattern in enumerate(value):
        try:
            normalized.append(
                canonical_rule_path(
                    pattern,
                    f"{label}[{index}]",
                    pattern=True,
                )
            )
        except RuleScopeError as exc:
            messages.append(str(exc))
    if len(normalized) != len(set(normalized)):
        messages.append(f"{label} contains duplicates")
    return tuple(messages)


def _dependency_boundary_parameter_messages(parameters):
    from .fact_providers import FACT_PROVIDER_REGISTRY

    messages, normalized = _parameter_shape_messages(
        parameters,
        required=("provider", "source_paths", "forbidden_target_paths"),
        allowed=("provider", "source_paths", "forbidden_target_paths"),
    )
    messages = list(messages)
    if normalized is None:
        return tuple(messages)
    if "provider" in normalized:
        provider = normalized["provider"]
        if not isinstance(provider, str) or provider not in FACT_PROVIDER_REGISTRY:
            messages.append(f"unknown Fact Provider: {provider!r}")
    for name in ("source_paths", "forbidden_target_paths"):
        if name in normalized:
            messages.extend(_pattern_sequence_messages(normalized[name], name))
    return tuple(messages)


def _normalized_patterns(value):
    return tuple(sorted(set(value)))


def _dependency_violation(fact):
    return {
        "source_path": fact.source_path,
        "directive": fact.directive,
        "target_path": fact.target_path,
        "target_uri": fact.target_uri,
    }


def _dependency_boundary(context, parameters):
    from .fact_providers import (
        FACT_PROVIDER_REGISTRY,
        FactProviderError,
        evaluate_fact_provider,
        resolve_fact_provider_evaluation,
        validate_fact_provider_result,
    )
    from .rules import RULES_REGISTRY, RULE_EXCEPTIONS_REGISTRY

    provider_id = parameters["provider"]
    source_paths = _normalized_patterns(parameters["source_paths"])
    forbidden_target_paths = _normalized_patterns(
        parameters["forbidden_target_paths"]
    )
    spec = FACT_PROVIDER_REGISTRY[provider_id]
    try:
        provider_paths, mode = resolve_fact_provider_evaluation(
            provider_id,
            context.evaluation_paths,
            additional_global_input_patterns=(
                RULES_REGISTRY.as_posix(),
                RULE_EXCEPTIONS_REGISTRY.as_posix(),
            ),
        )
        provider_result = evaluate_fact_provider(
            provider_id,
            context.project_root,
            evaluation_paths=provider_paths,
        )
        provider_result = validate_fact_provider_result(
            provider_result,
            expected_provider_id=provider_id,
            bounded_evaluation_paths=provider_paths,
        )
    except FactProviderError:
        return CheckerOutcome(
            "ERROR",
            {
                "provider_id": provider_id,
                "provider_version": spec.version,
                "source_paths": list(source_paths),
                "forbidden_target_paths": list(forbidden_target_paths),
                "violations": [],
            },
            f"Fact Provider {provider_id!r} could not establish trustworthy dependency facts",
        )

    violations = []
    for fact in provider_result.facts:
        if fact.target_kind != "project":
            continue
        if not any(
            scope_pattern_matches(pattern, fact.source_path)
            for pattern in source_paths
        ):
            continue
        if any(
            scope_pattern_matches(pattern, fact.target_path)
            for pattern in forbidden_target_paths
        ):
            violations.append(_dependency_violation(fact))
    violations.sort(
        key=lambda item: (
            item["source_path"],
            item["directive"],
            item["target_path"],
            item["target_uri"],
        )
    )
    details = {
        "provider_id": provider_result.provider_id,
        "provider_version": provider_result.provider_version,
        "fact_set_sha256": provider_result.fact_set_sha256,
        "inspected_source_paths": list(provider_result.inspected_source_paths),
        "provider_evaluation_mode": mode,
        "source_paths": list(source_paths),
        "forbidden_target_paths": list(forbidden_target_paths),
        "violations": violations,
    }
    if violations:
        return CheckerOutcome(
            "FAIL",
            details,
            f"forbidden project dependency edges found: {len(violations)}",
        )
    return CheckerOutcome("PASS", details)


def _canonical_sequence_messages(value, label, *, pattern):
    if not isinstance(value, list):
        return [f"{label} must be a list"]
    messages = []
    normalized = []
    for index, item in enumerate(value):
        try:
            normalized.append(
                canonical_rule_path(
                    item,
                    f"{label}[{index}]",
                    pattern=pattern,
                )
            )
        except RuleScopeError as exc:
            messages.append(str(exc))
    if value != sorted(set(normalized)):
        messages.append(f"{label} must be sorted and deduplicated")
    return messages


def _dependency_boundary_outcome_messages(raw_status, details, failure_reason):
    from .fact_providers import FACT_PROVIDER_REGISTRY

    if raw_status not in {"PASS", "FAIL"}:
        return ()
    if not isinstance(details, Mapping):
        return ("dependency boundary details must be a mapping/object",)
    required = {
        "provider_id",
        "provider_version",
        "fact_set_sha256",
        "inspected_source_paths",
        "provider_evaluation_mode",
        "source_paths",
        "forbidden_target_paths",
        "violations",
    }
    messages = []
    if set(details) != required:
        messages.append("dependency boundary details fields are malformed")
    provider_id = details.get("provider_id")
    spec = FACT_PROVIDER_REGISTRY.get(provider_id) if isinstance(provider_id, str) else None
    if spec is None:
        messages.append("dependency boundary provider_id is unknown")
    elif details.get("provider_version") != spec.version:
        messages.append("dependency boundary provider_version is inconsistent")
    fact_hash = details.get("fact_set_sha256")
    if not isinstance(fact_hash, str) or not _SHA256_RE.fullmatch(fact_hash):
        messages.append("dependency boundary fact_set_sha256 is malformed")
    if details.get("provider_evaluation_mode") not in {
        "bounded",
        "project_wide",
        "project_wide_invalidation",
    }:
        messages.append("dependency boundary provider_evaluation_mode is malformed")
    messages.extend(
        _canonical_sequence_messages(
            details.get("inspected_source_paths"),
            "dependency boundary inspected_source_paths",
            pattern=False,
        )
    )
    for name in ("source_paths", "forbidden_target_paths"):
        messages.extend(
            _canonical_sequence_messages(
                details.get(name),
                f"dependency boundary {name}",
                pattern=True,
            )
        )
        if details.get(name) == []:
            messages.append(f"dependency boundary {name} must not be empty")
    violations = details.get("violations")
    if not isinstance(violations, list):
        messages.append("dependency boundary violations must be a list")
        violations = []
    normalized_violations = []
    inspected = details.get("inspected_source_paths")
    sources = details.get("source_paths")
    targets = details.get("forbidden_target_paths")
    for index, violation in enumerate(violations):
        label = f"dependency boundary violations[{index}]"
        if not isinstance(violation, Mapping) or set(violation) != {
            "source_path",
            "directive",
            "target_path",
            "target_uri",
        }:
            messages.append(f"{label} is malformed")
            continue
        try:
            source = canonical_rule_path(
                violation.get("source_path"), f"{label}.source_path", pattern=False
            )
            target = canonical_rule_path(
                violation.get("target_path"), f"{label}.target_path", pattern=False
            )
        except RuleScopeError as exc:
            messages.append(str(exc))
            continue
        directive = violation.get("directive")
        target_uri = violation.get("target_uri")
        if not isinstance(directive, str) or not directive:
            messages.append(f"{label}.directive must be a non-empty string")
        if not isinstance(target_uri, str) or not target_uri:
            messages.append(f"{label}.target_uri must be a non-empty string")
        if isinstance(inspected, list) and source not in inspected:
            messages.append(f"{label}.source_path was not inspected")
        if isinstance(sources, list):
            try:
                source_matches = any(
                    scope_pattern_matches(pattern, source) for pattern in sources
                )
            except RuleScopeError:
                source_matches = False
            if not source_matches:
                messages.append(f"{label}.source_path does not match source_paths")
        if isinstance(targets, list):
            try:
                target_matches = any(
                    scope_pattern_matches(pattern, target) for pattern in targets
                )
            except RuleScopeError:
                target_matches = False
            if not target_matches:
                messages.append(
                    f"{label}.target_path does not match forbidden_target_paths"
                )
        if isinstance(directive, str) and isinstance(target_uri, str):
            normalized_violations.append((source, directive, target, target_uri))
    if normalized_violations != sorted(set(normalized_violations)):
        messages.append("dependency boundary violations must be sorted and deduplicated")
    if raw_status == "PASS" and violations:
        messages.append("dependency boundary PASS must not contain violations")
    if raw_status == "FAIL" and not violations:
        messages.append("dependency boundary FAIL must contain violations")
    if raw_status == "PASS" and failure_reason is not None:
        messages.append("dependency boundary PASS must not have a failure reason")
    if raw_status == "FAIL" and (
        not isinstance(failure_reason, str) or not failure_reason
    ):
        messages.append("dependency boundary FAIL requires a failure reason")
    return tuple(messages)


def _path_has_reparse(path):
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode):
        return True
    attributes = getattr(info, "st_file_attributes", 0)
    flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & flag)


def _inspect_repository_path(project_root, relative_path):
    """Return whether an exact safe path exists without following child links."""
    try:
        root = Path(project_root).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise _FilesystemInspectionError(
            f"cannot resolve project root reliably: {exc}"
        ) from exc
    if not root.is_dir():
        raise _FilesystemInspectionError("project root is not a directory")

    current = root
    for part in PurePosixPath(relative_path).parts:
        current = current / part
        try:
            reparse = _path_has_reparse(current)
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise _FilesystemInspectionError(
                f"cannot inspect repository path {relative_path!r}: {exc}"
            ) from exc
        if reparse:
            raise _FilesystemInspectionError(
                f"repository path contains a symlink or reparse point: {relative_path}"
            )

    try:
        current.resolve(strict=True).relative_to(root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise _FilesystemInspectionError(
            f"repository path cannot be contained reliably: {relative_path!r}"
        ) from exc
    return True


def _required_path(context, parameters):
    relative_path = parameters["path"]
    try:
        exists = _inspect_repository_path(context.project_root, relative_path)
    except _FilesystemInspectionError as exc:
        return CheckerOutcome(
            "ERROR",
            {"path": relative_path},
            str(exc),
        )
    if exists:
        return CheckerOutcome("PASS", {"path": relative_path, "exists": True})
    return CheckerOutcome(
        "FAIL",
        {"path": relative_path, "exists": False},
        f"required path is missing: {relative_path}",
    )


def _forbidden_path(context, parameters):
    relative_path = parameters["path"]
    try:
        exists = _inspect_repository_path(context.project_root, relative_path)
    except _FilesystemInspectionError as exc:
        return CheckerOutcome(
            "ERROR",
            {"path": relative_path},
            str(exc),
        )
    if not exists:
        return CheckerOutcome("PASS", {"path": relative_path, "exists": False})
    return CheckerOutcome(
        "FAIL",
        {"path": relative_path, "exists": True},
        f"forbidden path exists: {relative_path}",
    )


def _required_field(context, parameters):
    objects = context.objects
    if (
        type(context.object_layer_complete) is not bool
        or not context.object_layer_complete
    ):
        return CheckerOutcome(
            "ERROR",
            {},
            "canonical object layer context is incomplete",
        )
    if not isinstance(objects, Mapping):
        return CheckerOutcome(
            "ERROR",
            {},
            "canonical object context must be a mapping",
        )

    object_type = parameters["object_type"]
    field = parameters["field"]
    matching = []
    violating = []
    for object_id, record in objects.items():
        if not isinstance(object_id, str) or not isinstance(record, Mapping):
            return CheckerOutcome(
                "ERROR",
                {"object_type": object_type, "field": field},
                "canonical object context contains an invalid record",
            )
        data = record.get("data")
        if not isinstance(data, Mapping) or not isinstance(data.get("type"), str):
            return CheckerOutcome(
                "ERROR",
                {"object_type": object_type, "field": field},
                f"canonical object context is incomplete for {object_id}",
            )
        if data["type"] != object_type:
            continue
        matching.append(object_id)
        if field not in data or data[field] is None:
            violating.append(object_id)

    details = {
        "object_type": object_type,
        "field": field,
        "matching_object_ids": sorted(matching),
        "violating_object_ids": sorted(violating),
    }
    if not matching:
        return CheckerOutcome("NOT_APPLICABLE", details)
    if violating:
        return CheckerOutcome(
            "FAIL",
            details,
            f"required field {field!r} is missing or null on matching objects",
        )
    return CheckerOutcome("PASS", details)


CHECKER_REGISTRY = MappingProxyType(
    {
        "architecture.dependency_boundary": CheckerSpec(
            "architecture.dependency_boundary",
            CHECKER_VERSION,
            _dependency_boundary_parameter_messages,
            _dependency_boundary,
            _dependency_boundary_outcome_messages,
        ),
        "repository.required_path": CheckerSpec(
            "repository.required_path",
            CHECKER_VERSION,
            _repository_parameter_messages,
            _required_path,
        ),
        "repository.forbidden_path": CheckerSpec(
            "repository.forbidden_path",
            CHECKER_VERSION,
            _repository_parameter_messages,
            _forbidden_path,
        ),
        "knowledge.required_field": CheckerSpec(
            "knowledge.required_field",
            CHECKER_VERSION,
            _knowledge_parameter_messages,
            _required_field,
        ),
    }
)


def checker_contract_messages(checker_id, parameters):
    spec = CHECKER_REGISTRY.get(checker_id)
    if spec is None:
        return (f"unknown deterministic checker: {checker_id!r}",)
    return spec.parameter_validator(parameters)


def checker_outcome_contract_messages(
    checker_id,
    raw_status,
    details,
    failure_reason,
):
    spec = CHECKER_REGISTRY.get(checker_id)
    if spec is None or spec.outcome_validator is None:
        return ()
    return spec.outcome_validator(raw_status, details, failure_reason)


def evaluate_checker(checker_id, context, parameters):
    """Evaluate one checker after fail-closed runtime contract validation.

    Unexpected implementation exceptions deliberately cross this boundary so the
    Rule Engine can convert them to one ERROR result without hiding engine bugs.
    """
    spec = CHECKER_REGISTRY.get(checker_id)
    if spec is None:
        return CheckerOutcome(
            "ERROR",
            {"checker": checker_id},
            f"unknown deterministic checker: {checker_id!r}",
        )
    messages = spec.parameter_validator(parameters)
    if messages:
        return CheckerOutcome(
            "ERROR",
            {"checker": checker_id, "contract_errors": list(messages)},
            "invalid checker parameters: " + "; ".join(messages),
        )
    outcome = spec.implementation(context, parameters)
    if not isinstance(outcome, CheckerOutcome):
        return CheckerOutcome(
            "ERROR",
            {"checker": checker_id},
            "checker returned a malformed outcome",
        )
    messages = checker_outcome_contract_messages(
        checker_id,
        outcome.raw_status,
        outcome.details,
        outcome.failure_reason,
    )
    if messages:
        return CheckerOutcome(
            "ERROR",
            {"checker": checker_id, "outcome_contract_errors": list(messages)},
            "checker returned malformed outcome: " + "; ".join(messages),
        )
    return outcome
