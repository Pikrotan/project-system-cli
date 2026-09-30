from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from types import MappingProxyType

from .rule_scope import (
    RuleScopeError,
    canonical_rule_path,
    canonicalize_evaluation_paths,
    scope_pattern_matches,
)


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_EVALUATION_MODES = frozenset(
    {"bounded", "project_wide", "project_wide_invalidation"}
)
_VERIFICATION_STATUSES = frozenset(
    {"PASS", "FAIL", "NOT_APPLICABLE"}
)
_FINDING_SEVERITIES = frozenset({"ERROR", "WARNING", "INFO"})


class VerificationAdapterError(RuntimeError):
    pass


@dataclass(frozen=True)
class VerificationFinding:
    path: str | None
    line: int | None
    column: int | None
    severity: str
    code: str
    message: str


@dataclass(frozen=True)
class VerificationAdapterResult:
    adapter_id: str
    adapter_version: str
    tool_name: str
    tool_version: str
    evaluation_mode: str
    verification_status: str
    inspected_paths: tuple[str, ...]
    findings: tuple[VerificationFinding, ...]
    exit_code: int
    stdout_sha256: str
    stderr_sha256: str
    result_sha256: str


@dataclass(frozen=True)
class VerificationAdapterSpec:
    adapter_id: str
    version: str
    implementation: Callable[
        [object, tuple[str, ...] | None, str],
        object,
    ]
    global_input_patterns: tuple[str, ...] = ()
    executes_project_code: bool = False


from . import dart_analyze_adapter


VERIFICATION_ADAPTER_REGISTRY = MappingProxyType(
    {
        "dart.analyze": VerificationAdapterSpec(
            adapter_id="dart.analyze",
            version="1",
            implementation=dart_analyze_adapter.run_dart_analyze,
            global_input_patterns=(
                "**/*.dart",
                "**/*.yaml",
                "**/pubspec.lock",
            ),
            executes_project_code=False,
        ),
    }
)


def _validated_adapter_spec(adapter_id):
    if (
        not isinstance(adapter_id, str)
        or adapter_id not in VERIFICATION_ADAPTER_REGISTRY
    ):
        raise VerificationAdapterError(
            f"unknown Verification Adapter: {adapter_id!r}"
        )

    spec = VERIFICATION_ADAPTER_REGISTRY[adapter_id]
    if not isinstance(spec, VerificationAdapterSpec):
        raise VerificationAdapterError(
            f"Verification Adapter {adapter_id!r} has malformed metadata"
        )
    if spec.adapter_id != adapter_id:
        raise VerificationAdapterError(
            f"Verification Adapter {adapter_id!r} identity is inconsistent"
        )
    if (
        not isinstance(spec.version, str)
        or not spec.version
        or spec.version != spec.version.strip()
    ):
        raise VerificationAdapterError(
            f"Verification Adapter {adapter_id!r} version is malformed"
        )
    if not callable(spec.implementation):
        raise VerificationAdapterError(
            f"Verification Adapter {adapter_id!r} implementation is malformed"
        )
    if type(spec.executes_project_code) is not bool:
        raise VerificationAdapterError(
            f"Verification Adapter {adapter_id!r} executes_project_code "
            "must be a boolean"
        )
    if not isinstance(spec.global_input_patterns, tuple):
        raise VerificationAdapterError(
            f"Verification Adapter {adapter_id!r} global input patterns "
            "are malformed"
        )

    patterns = []
    for index, pattern in enumerate(spec.global_input_patterns):
        try:
            patterns.append(
                canonical_rule_path(
                    pattern,
                    (
                        f"Verification Adapter {adapter_id!r} "
                        f"global_input_patterns[{index}]"
                    ),
                    pattern=True,
                )
            )
        except RuleScopeError as exc:
            raise VerificationAdapterError(str(exc)) from exc

    if tuple(sorted(set(patterns))) != spec.global_input_patterns:
        raise VerificationAdapterError(
            f"Verification Adapter {adapter_id!r} global input patterns "
            "must be sorted and unique"
        )

    return spec


def resolve_verification_adapter_evaluation(
    adapter_id,
    evaluation_paths,
    *,
    additional_global_input_patterns=(),
):
    """Resolve bounded adapter input or a deliberate complete invalidation."""
    spec = _validated_adapter_spec(adapter_id)

    try:
        paths = canonicalize_evaluation_paths(evaluation_paths)
    except RuleScopeError as exc:
        raise VerificationAdapterError(str(exc)) from exc

    if paths is None:
        return None, "project_wide"

    if (
        not isinstance(additional_global_input_patterns, Sequence)
        or isinstance(additional_global_input_patterns, (str, bytes))
    ):
        raise VerificationAdapterError(
            "additional global input patterns must be a sequence"
        )

    additional = []
    for index, pattern in enumerate(additional_global_input_patterns):
        try:
            additional.append(
                canonical_rule_path(
                    pattern,
                    f"additional_global_input_patterns[{index}]",
                    pattern=True,
                )
            )
        except RuleScopeError as exc:
            raise VerificationAdapterError(str(exc)) from exc

    patterns = tuple(
        sorted(set(spec.global_input_patterns + tuple(additional)))
    )

    if any(
        scope_pattern_matches(pattern, path)
        for pattern in patterns
        for path in paths
    ):
        return None, "project_wide_invalidation"

    return paths, "bounded"


def _finding_to_dict(finding):
    return {
        "path": finding.path,
        "line": finding.line,
        "column": finding.column,
        "severity": finding.severity,
        "code": finding.code,
        "message": finding.message,
    }


def _finding_sort_key(finding):
    return (
        "" if finding.path is None else finding.path,
        0 if finding.line is None else finding.line,
        0 if finding.column is None else finding.column,
        finding.severity,
        finding.code,
        finding.message,
    )


def _validated_finding(finding, *, label):
    if not isinstance(finding, VerificationFinding):
        raise VerificationAdapterError(f"{label} is malformed")

    path = finding.path
    if path is not None:
        try:
            canonical = canonical_rule_path(
                path,
                f"{label}.path",
                pattern=False,
            )
        except RuleScopeError as exc:
            raise VerificationAdapterError(str(exc)) from exc
        if canonical != path:
            raise VerificationAdapterError(
                f"{label}.path must be canonical"
            )

    if path is None and (
        finding.line is not None or finding.column is not None
    ):
        raise VerificationAdapterError(
            f"{label} coordinates require a concrete path"
        )

    for name, value in (
        ("line", finding.line),
        ("column", finding.column),
    ):
        if value is not None and (type(value) is not int or value <= 0):
            raise VerificationAdapterError(
                f"{label}.{name} must be a positive integer or null"
            )

    if finding.severity not in _FINDING_SEVERITIES:
        raise VerificationAdapterError(
            f"{label}.severity is unsupported"
        )

    for name, value in (
        ("code", finding.code),
        ("message", finding.message),
    ):
        if (
            not isinstance(value, str)
            or not value
            or value != value.strip()
        ):
            raise VerificationAdapterError(
                f"{label}.{name} must be a non-empty trimmed string"
            )

    return finding


def verification_result_sha256(result):
    if not isinstance(result, VerificationAdapterResult):
        raise VerificationAdapterError(
            "Verification Adapter result is malformed"
        )
    if not isinstance(result.findings, tuple):
        raise VerificationAdapterError(
            "Verification Adapter findings must be a tuple"
        )

    validated_findings = tuple(
        _validated_finding(
            finding,
            label=f"findings[{index}]",
        )
        for index, finding in enumerate(result.findings)
    )

    payload = {
        "adapter_id": result.adapter_id,
        "adapter_version": result.adapter_version,
        "tool_name": result.tool_name,
        "tool_version": result.tool_version,
        "evaluation_mode": result.evaluation_mode,
        "verification_status": result.verification_status,
        "inspected_paths": list(result.inspected_paths),
        "findings": [
            _finding_to_dict(finding)
            for finding in validated_findings
        ],
        "exit_code": result.exit_code,
        "stdout_sha256": result.stdout_sha256,
        "stderr_sha256": result.stderr_sha256,
    }

    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")

    return hashlib.sha256(encoded).hexdigest()


def evaluate_verification_adapter(
    adapter_id,
    project_root,
    *,
    evaluation_paths=None,
    additional_global_input_patterns=(),
):
    """Execute one packaged Verification Adapter through the trusted boundary."""
    spec = _validated_adapter_spec(adapter_id)

    resolved_paths, evaluation_mode = resolve_verification_adapter_evaluation(
        adapter_id,
        evaluation_paths,
        additional_global_input_patterns=additional_global_input_patterns,
    )

    try:
        root = Path(project_root).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise VerificationAdapterError(
            "project_root cannot be resolved reliably"
        ) from exc
    if not root.is_dir():
        raise VerificationAdapterError(
            "project_root must be an existing directory"
        )
    try:
        result = spec.implementation(
            root,
            resolved_paths,
            evaluation_mode,
        )
    except VerificationAdapterError:
        raise VerificationAdapterError(
            f"Verification Adapter {adapter_id!r} execution failed"
        ) from None
    except Exception:
        raise VerificationAdapterError(
            f"Verification Adapter {adapter_id!r} execution failed"
        ) from None

    try:
        return validate_verification_adapter_result(
            result,
            expected_adapter_id=adapter_id,
            bounded_evaluation_paths=resolved_paths,
            expected_evaluation_mode=evaluation_mode,
        )
    except VerificationAdapterError:
        raise VerificationAdapterError(
            f"Verification Adapter {adapter_id!r} returned an invalid result"
        ) from None


def validate_verification_adapter_result(
    result,
    *,
    expected_adapter_id,
    bounded_evaluation_paths=None,
    expected_evaluation_mode=None,
):
    """Validate normalized adapter output at the consumer trust boundary."""
    spec = _validated_adapter_spec(expected_adapter_id)

    if not isinstance(result, VerificationAdapterResult):
        raise VerificationAdapterError(
            "Verification Adapter returned a malformed result"
        )
    if result.adapter_id != expected_adapter_id:
        raise VerificationAdapterError(
            "Verification Adapter result identity is inconsistent"
        )
    if result.adapter_version != spec.version:
        raise VerificationAdapterError(
            "Verification Adapter result version is inconsistent"
        )

    for name, value in (
        ("tool_name", result.tool_name),
        ("tool_version", result.tool_version),
    ):
        if (
            not isinstance(value, str)
            or not value
            or value != value.strip()
        ):
            raise VerificationAdapterError(
                f"Verification Adapter result {name} is malformed"
            )

    if result.evaluation_mode not in _EVALUATION_MODES:
        raise VerificationAdapterError(
            "Verification Adapter result evaluation_mode is malformed"
        )

    if result.verification_status not in _VERIFICATION_STATUSES:
        raise VerificationAdapterError(
            "Verification Adapter result verification_status is malformed"
        )

    if expected_evaluation_mode is not None:
        if expected_evaluation_mode not in _EVALUATION_MODES:
            raise VerificationAdapterError(
                "expected evaluation_mode is malformed"
            )
        if result.evaluation_mode != expected_evaluation_mode:
            raise VerificationAdapterError(
                "Verification Adapter result evaluation_mode "
                "does not match authoritative evaluation mode"
            )

    if not isinstance(result.inspected_paths, tuple):
        raise VerificationAdapterError(
            "Verification Adapter inspected_paths must be a tuple"
        )

    try:
        inspected = canonicalize_evaluation_paths(
            result.inspected_paths
        )
        bounded = canonicalize_evaluation_paths(
            bounded_evaluation_paths
        )
    except RuleScopeError as exc:
        raise VerificationAdapterError(str(exc)) from exc

    if inspected != result.inspected_paths:
        raise VerificationAdapterError(
            "Verification Adapter inspected_paths must be sorted "
            "and deduplicated"
        )

    if (
        result.evaluation_mode
        in {"project_wide", "project_wide_invalidation"}
        and inspected
    ):
        raise VerificationAdapterError(
            "complete Verification Adapter evaluation "
            "must not enumerate inspected_paths"
        )

    if bounded is not None:
        unexpected = sorted(set(inspected) - set(bounded))
        if unexpected:
            raise VerificationAdapterError(
                "adapter reported inspected paths outside bounded "
                "evaluation_paths: "
                + ", ".join(unexpected)
            )

    if not isinstance(result.findings, tuple):
        raise VerificationAdapterError(
            "Verification Adapter findings must be a tuple"
        )

    validated_findings = tuple(
        _validated_finding(
            finding,
            label=f"findings[{index}]",
        )
        for index, finding in enumerate(result.findings)
    )

    normalized_findings = tuple(
        sorted(
            set(validated_findings),
            key=_finding_sort_key,
        )
    )
    if validated_findings != normalized_findings:
        raise VerificationAdapterError(
            "Verification Adapter findings must be sorted and deduplicated"
        )

    if result.evaluation_mode == "bounded":
        inspected_set = set(inspected)
        uninspected = sorted(
            {
                finding.path
                for finding in validated_findings
                if finding.path is not None
                and finding.path not in inspected_set
            }
        )
        if uninspected:
            raise VerificationAdapterError(
                "finding source paths were not reported as inspected: "
                + ", ".join(uninspected)
            )

    if type(result.exit_code) is not int or result.exit_code < 0:
        raise VerificationAdapterError(
            "Verification Adapter exit_code must be a non-negative integer"
        )

    for name, value in (
        ("stdout_sha256", result.stdout_sha256),
        ("stderr_sha256", result.stderr_sha256),
        ("result_sha256", result.result_sha256),
    ):
        if (
            not isinstance(value, str)
            or not _SHA256_RE.fullmatch(value)
        ):
            raise VerificationAdapterError(
                f"Verification Adapter {name} is malformed"
            )

    expected_hash = verification_result_sha256(result)
    if result.result_sha256 != expected_hash:
        raise VerificationAdapterError(
            "Verification Adapter result_sha256 is inconsistent"
        )

    return result
