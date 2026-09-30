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
    outcome_validator: Callable[[str, object, object, object], tuple[str, ...]] | None = None


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


def _dependency_boundary_outcome_messages(
    raw_status,
    details,
    failure_reason,
    parameters=None,
):
    if not isinstance(parameters, Mapping):
        return (
            "architecture dependency boundary parameters must be a mapping/object",
        )

    expected_provider = parameters.get("provider")
    if not isinstance(expected_provider, str) or not expected_provider:
        return (
            "architecture dependency boundary provider parameter is malformed",
        )

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
    if provider_id != expected_provider:
        messages.append(
            "architecture dependency boundary provider_id "
            "does not match Rule provider parameter"
        )
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



def _code_verification_parameter_messages(parameters):
    from .verification_adapters import VERIFICATION_ADAPTER_REGISTRY

    messages, normalized = _parameter_shape_messages(
        parameters,
        required=("adapter",),
        allowed=("adapter",),
    )
    messages = list(messages)
    if normalized is None:
        return tuple(messages)

    if "adapter" in normalized:
        adapter_id = normalized["adapter"]
        if (
            not isinstance(adapter_id, str)
            or adapter_id not in VERIFICATION_ADAPTER_REGISTRY
        ):
            messages.append(
                f"unknown Verification Adapter: {adapter_id!r}"
            )

    return tuple(messages)


def _verification_finding_payload(finding):
    return {
        "path": finding.path,
        "line": finding.line,
        "column": finding.column,
        "severity": finding.severity,
        "code": finding.code,
        "message": finding.message,
    }


def _verification_result_payload(result):
    return {
        "adapter_id": result.adapter_id,
        "adapter_version": result.adapter_version,
        "tool_name": result.tool_name,
        "tool_version": result.tool_version,
        "evaluation_mode": result.evaluation_mode,
        "verification_status": result.verification_status,
        "inspected_paths": list(result.inspected_paths),
        "findings": [
            _verification_finding_payload(finding)
            for finding in result.findings
        ],
        "exit_code": result.exit_code,
        "stdout_sha256": result.stdout_sha256,
        "stderr_sha256": result.stderr_sha256,
        "result_sha256": result.result_sha256,
    }


def _code_verification(context, parameters):
    from .rules import RULES_REGISTRY, RULE_EXCEPTIONS_REGISTRY
    from .verification_adapters import (
        VERIFICATION_ADAPTER_REGISTRY,
        VerificationAdapterError,
        evaluate_verification_adapter,
    )

    adapter_id = parameters["adapter"]
    spec = VERIFICATION_ADAPTER_REGISTRY[adapter_id]

    try:
        result = evaluate_verification_adapter(
            adapter_id,
            context.project_root,
            evaluation_paths=context.evaluation_paths,
            additional_global_input_patterns=(
                RULES_REGISTRY.as_posix(),
                RULE_EXCEPTIONS_REGISTRY.as_posix(),
            ),
        )
    except VerificationAdapterError:
        return CheckerOutcome(
            "ERROR",
            {
                "adapter_id": adapter_id,
                "adapter_version": spec.version,
            },
            f"Verification Adapter {adapter_id!r} could not establish "
            "a trustworthy verification result",
        )

    details = _verification_result_payload(result)

    if result.verification_status == "NOT_APPLICABLE":
        return CheckerOutcome("NOT_APPLICABLE", details)

    if result.verification_status == "PASS":
        return CheckerOutcome("PASS", details)

    if result.verification_status == "FAIL":
        concrete_paths = tuple(
            sorted(
                {
                    finding.path
                    for finding in result.findings
                    if finding.path is not None
                }
            )
        )
        if (
            not concrete_paths
            or any(finding.path is None for finding in result.findings)
        ):
            return CheckerOutcome(
                "ERROR",
                details,
                "Verification Adapter reported FAIL without complete "
                "concrete finding paths",
            )

        return CheckerOutcome(
            "FAIL",
            details,
            f"Verification Adapter {adapter_id!r} reported verification failure",
        )

    return CheckerOutcome(
        "ERROR",
        details,
        "Verification Adapter returned an unsupported semantic status",
    )


def _code_verification_outcome_messages(
    raw_status,
    details,
    failure_reason,
    parameters=None,
):
    from .verification_adapters import (
        VERIFICATION_ADAPTER_REGISTRY,
        VerificationAdapterError,
        VerificationAdapterResult,
        VerificationFinding,
        verification_result_sha256,
    )

    if raw_status not in {"PASS", "FAIL", "NOT_APPLICABLE"}:
        return ()

    if not isinstance(details, Mapping):
        return ("code verification details must be a mapping/object",)

    if not isinstance(parameters, Mapping):
        return ("code verification parameters must be a mapping/object",)

    expected_adapter = parameters.get("adapter")
    if not isinstance(expected_adapter, str) or not expected_adapter:
        return ("code verification adapter parameter is malformed",)

    required = {
        "adapter_id",
        "adapter_version",
        "tool_name",
        "tool_version",
        "evaluation_mode",
        "verification_status",
        "inspected_paths",
        "findings",
        "exit_code",
        "stdout_sha256",
        "stderr_sha256",
        "result_sha256",
    }

    messages = []

    if set(details) != required:
        messages.append("code verification details fields are malformed")

    adapter_id = details.get("adapter_id")
    if adapter_id != expected_adapter:
        messages.append(
            "code verification adapter_id does not match Rule adapter parameter"
        )
    spec = (
        VERIFICATION_ADAPTER_REGISTRY.get(adapter_id)
        if isinstance(adapter_id, str)
        else None
    )
    if spec is None:
        messages.append("code verification adapter_id is unknown")
    elif details.get("adapter_version") != spec.version:
        messages.append(
            "code verification adapter_version is inconsistent"
        )

    for name in ("tool_name", "tool_version"):
        value = details.get(name)
        if not isinstance(value, str) or not value:
            messages.append(
                f"code verification {name} must be a non-empty string"
            )

    if details.get("evaluation_mode") not in {
        "bounded",
        "project_wide",
        "project_wide_invalidation",
    }:
        messages.append(
            "code verification evaluation_mode is malformed"
        )

    if details.get("verification_status") != raw_status:
        messages.append(
            "code verification semantic status does not match raw status"
        )

    inspected = details.get("inspected_paths")
    if not isinstance(inspected, list):
        messages.append(
            "code verification inspected_paths must be a list"
        )
    else:
        normalized = []
        for index, value in enumerate(inspected):
            try:
                normalized.append(
                    canonical_rule_path(
                        value,
                        f"code verification inspected_paths[{index}]",
                        pattern=False,
                    )
                )
            except RuleScopeError as exc:
                messages.append(str(exc))
        if inspected != sorted(set(normalized)):
            messages.append(
                "code verification inspected_paths must be sorted "
                "and deduplicated"
            )

    findings = details.get("findings")
    normalized_findings = []
    if not isinstance(findings, list):
        messages.append("code verification findings must be a list")
        findings = []

    finding_fields = {
        "path",
        "line",
        "column",
        "severity",
        "code",
        "message",
    }

    for index, finding in enumerate(findings):
        label = f"code verification findings[{index}]"

        if not isinstance(finding, Mapping):
            messages.append(f"{label} must be a mapping/object")
            continue

        if set(finding) != finding_fields:
            messages.append(f"{label} fields are malformed")
            continue

        finding_path = finding.get("path")
        if finding_path is not None:
            try:
                finding_path = canonical_rule_path(
                    finding_path,
                    f"{label}.path",
                    pattern=False,
                )
            except RuleScopeError as exc:
                messages.append(str(exc))
                continue

        line = finding.get("line")
        column = finding.get("column")
        if line is not None and (
            type(line) is not int or line <= 0
        ):
            messages.append(
                f"{label}.line must be a positive integer or null"
            )
        if column is not None and (
            type(column) is not int or column <= 0
        ):
            messages.append(
                f"{label}.column must be a positive integer or null"
            )
        if finding_path is None and (
            line is not None or column is not None
        ):
            messages.append(
                f"{label} cannot contain line/column without path"
            )

        severity = finding.get("severity")
        if severity not in {"ERROR", "WARNING", "INFO"}:
            messages.append(f"{label}.severity is malformed")

        for name in ("code", "message"):
            value = finding.get(name)
            if not isinstance(value, str) or not value:
                messages.append(
                    f"{label}.{name} must be a non-empty string"
                )

        normalized_findings.append(
            (
                finding_path,
                line,
                column,
                severity,
                finding.get("code"),
                finding.get("message"),
            )
        )

    if normalized_findings != sorted(set(normalized_findings)):
        messages.append(
            "code verification findings must be sorted and deduplicated"
        )

    exit_code = details.get("exit_code")
    if type(exit_code) is not int or exit_code < 0:
        messages.append(
            "code verification exit_code must be a non-negative integer"
        )

    for name in (
        "stdout_sha256",
        "stderr_sha256",
        "result_sha256",
    ):
        value = details.get(name)
        if (
            not isinstance(value, str)
            or not _SHA256_RE.fullmatch(value)
        ):
            messages.append(
                f"code verification {name} is malformed"
            )

    hash_contract_shape_valid = (
        set(details) == required
        and isinstance(inspected, list)
        and isinstance(findings, list)
        and all(
            isinstance(finding, Mapping)
            and set(finding) == finding_fields
            for finding in findings
        )
    )

    if hash_contract_shape_valid:
        try:
            reconstructed = VerificationAdapterResult(
                adapter_id=details["adapter_id"],
                adapter_version=details["adapter_version"],
                tool_name=details["tool_name"],
                tool_version=details["tool_version"],
                evaluation_mode=details["evaluation_mode"],
                verification_status=details["verification_status"],
                inspected_paths=tuple(details["inspected_paths"]),
                findings=tuple(
                    VerificationFinding(
                        path=finding["path"],
                        line=finding["line"],
                        column=finding["column"],
                        severity=finding["severity"],
                        code=finding["code"],
                        message=finding["message"],
                    )
                    for finding in details["findings"]
                ),
                exit_code=details["exit_code"],
                stdout_sha256=details["stdout_sha256"],
                stderr_sha256=details["stderr_sha256"],
                result_sha256=details["result_sha256"],
            )
            expected_result_sha256 = verification_result_sha256(
                reconstructed
            )
        except (
            VerificationAdapterError,
            TypeError,
            ValueError,
        ):
            expected_result_sha256 = None

        if (
            expected_result_sha256 is not None
            and details.get("result_sha256")
            != expected_result_sha256
        ):
            messages.append(
                "code verification result_sha256 is inconsistent"
            )

    if raw_status == "FAIL":
        if not findings:
            messages.append(
                "code verification FAIL must contain findings"
            )
        elif any(
            isinstance(finding, Mapping)
            and finding.get("path") is None
            for finding in findings
        ):
            messages.append(
                "code verification FAIL requires concrete finding paths"
            )

    if raw_status in {"PASS", "NOT_APPLICABLE"}:
        if failure_reason is not None:
            messages.append(
                f"code verification {raw_status} must not have "
                "a failure reason"
            )

    if raw_status == "FAIL" and (
        not isinstance(failure_reason, str)
        or not failure_reason
    ):
        messages.append(
            "code verification FAIL requires a failure reason"
        )

    return tuple(messages)

CHECKER_REGISTRY = MappingProxyType(
    {
        "code.verification": CheckerSpec(
            "code.verification",
            CHECKER_VERSION,
            _code_verification_parameter_messages,
            _code_verification,
            _code_verification_outcome_messages,
        ),
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
    parameters=None,
):
    spec = CHECKER_REGISTRY.get(checker_id)
    if spec is None or spec.outcome_validator is None:
        return ()
    return spec.outcome_validator(
        raw_status,
        details,
        failure_reason,
        parameters,
    )


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

        parameters,
    )
    if messages:
        return CheckerOutcome(
            "ERROR",
            {"checker": checker_id, "outcome_contract_errors": list(messages)},
            "checker returned malformed outcome: " + "; ".join(messages),
        )
    return outcome
