"""Allowlisted deterministic checkers for Executable Rules v1 Stage 2A."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import os
from pathlib import Path, PurePosixPath
import re
import stat
from types import MappingProxyType
from typing import Any

from .object_loader import TYPE_DIRECTORIES


CHECKER_VERSION = "1"
_FIELD_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")
_GLOB_CHARACTERS = frozenset("*?[]")


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
    return spec.implementation(context, parameters)
