"""Executable Rules v1 activation and strict registry loading."""

from dataclasses import dataclass
from datetime import datetime
import json
import os
import re
import stat
from pathlib import Path, PurePosixPath

from jsonschema import Draft202012Validator, FormatChecker
import yaml

from .frontmatter import StrictSafeLoader
from .rule_checkers import checker_contract_messages
from .utils import distribution_root, load_yaml


RULES_SCHEMA_VERSION = 1
RULES_REGISTRY = Path(".project/policies/rules.yaml")
RULE_EXCEPTIONS_REGISTRY = Path(".project/policies/rule_exceptions.yaml")
MAX_RULE_REGISTRY_BYTES = 1024 * 1024

_RFC3339_DATETIME_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)


def _is_rfc3339_datetime(value):
    if not isinstance(value, str) or not _RFC3339_DATETIME_RE.fullmatch(value):
        return False
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return False
    return parsed.tzinfo is not None


FORMAT_CHECKER = FormatChecker()
FORMAT_CHECKER.checks("date-time")(_is_rfc3339_datetime)


class RuleRegistryError(RuntimeError):
    pass


class RulesSafeLoader(StrictSafeLoader):
    """Strict mapping loader that preserves timestamps as strings."""


RulesSafeLoader.yaml_implicit_resolvers = {
    key: list(value)
    for key, value in StrictSafeLoader.yaml_implicit_resolvers.items()
}
for key, resolvers in list(RulesSafeLoader.yaml_implicit_resolvers.items()):
    RulesSafeLoader.yaml_implicit_resolvers[key] = [
        resolver
        for resolver in resolvers
        if resolver[0] != "tag:yaml.org,2002:timestamp"
    ]


@dataclass
class RuleLayer:
    active: bool
    issues: list[tuple[str, str, str]]
    rules_registry: dict | None = None
    exception_registry: dict | None = None


def _reject_aliases(text, label):
    try:
        for token in yaml.scan(text, Loader=yaml.SafeLoader):
            if isinstance(token, (yaml.tokens.AnchorToken, yaml.tokens.AliasToken)):
                raise RuleRegistryError(
                    f"YAML anchors and aliases are not allowed in {label}"
                )
    except RuleRegistryError:
        raise
    except Exception as exc:
        raise RuleRegistryError(f"cannot scan {label}: {exc}") from exc


def _schema_messages(document, schema_name):
    schema = json.loads(
        (distribution_root() / "schemas" / schema_name).read_text(encoding="utf-8")
    )
    validator = Draft202012Validator(
        schema,
        format_checker=FORMAT_CHECKER,
    )
    messages = []
    for error in sorted(
        validator.iter_errors(document),
        key=lambda item: list(item.path),
    ):
        location = ".".join(str(part) for part in error.path) or "<root>"
        messages.append(f"{location}: {error.message}")
    return messages


def _path_has_reparse(path):
    path = Path(path)
    if not path.exists() and not path.is_symlink():
        return False
    if path.is_symlink():
        return True
    try:
        attributes = getattr(os.lstat(path), "st_file_attributes", 0)
    except OSError:
        return True
    flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & flag)


def _ensure_registry_path(root, path, label):
    root = Path(root).resolve()
    path = Path(path)
    try:
        relative = path.absolute().relative_to(root)
    except ValueError as exc:
        raise RuleRegistryError(f"{label} is outside the project root") from exc

    current = root
    for part in relative.parts:
        current = current / part
        if _path_has_reparse(current):
            raise RuleRegistryError(
                f"{label} path contains a symlink or reparse point: {current}"
            )

    try:
        path.resolve(strict=True).relative_to(root)
    except (OSError, ValueError) as exc:
        raise RuleRegistryError(f"{label} resolves outside the project root") from exc


def _load_registry(path, label, schema_name):
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise RuleRegistryError(f"cannot read {label}: {exc}") from exc

    if len(raw) > MAX_RULE_REGISTRY_BYTES:
        raise RuleRegistryError(
            f"{label} exceeds {MAX_RULE_REGISTRY_BYTES} bytes"
        )

    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise RuleRegistryError(f"{label} must be UTF-8") from exc

    _reject_aliases(text, label)

    try:
        document = yaml.load(text, Loader=RulesSafeLoader)
    except Exception as exc:
        raise RuleRegistryError(f"cannot parse {label}: {exc}") from exc

    if not isinstance(document, dict):
        raise RuleRegistryError(f"{label} root must be a mapping/object")

    messages = _schema_messages(document, schema_name)
    return document, messages


def _safe_scope_path(value):
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or "\\" in value
        or ":" in value
        or "\x00" in value
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


def _rule_semantic_messages(registry):
    rules = registry["rules"]
    messages = []

    for rule_id, rule in rules.items():
        verification = rule["verification"]
        if verification["method"] == "deterministic":
            for message in checker_contract_messages(
                verification["checker"],
                verification.get("parameters"),
            ):
                messages.append(
                    f"rules.{rule_id}.verification: {message}"
                )

        scope = rule.get("scope")
        if scope is not None:
            for path in scope["paths"]:
                if not _safe_scope_path(path):
                    messages.append(
                        f"rules.{rule_id}.scope.paths: unsafe scope path: {path!r}"
                    )

        superseded_by = rule.get("superseded_by")
        if superseded_by is not None:
            if superseded_by == rule_id:
                messages.append(
                    f"rules.{rule_id}: rule cannot be superseded_by itself"
                )
            elif superseded_by not in rules:
                messages.append(
                    f"rules.{rule_id}: unknown superseded_by rule: {superseded_by}"
                )

    return messages

def _exception_semantic_messages(registry):
    messages = []

    for exception_id, exception in registry["exceptions"].items():
        for path in exception["scope"]["paths"]:
            if not _safe_scope_path(path):
                messages.append(
                    f"exceptions.{exception_id}.scope.paths: unsafe scope path: {path!r}"
                )

    return messages


def validate_rule_references(layer, objects):
    """Validate Rules references against an already-loaded knowledge object map."""
    if not layer.active:
        return []

    rules_location = RULES_REGISTRY.as_posix()
    exceptions_location = RULE_EXCEPTIONS_REGISTRY.as_posix()

    rules_blocked = any(
        level == "BLOCKING" and location == rules_location
        for level, location, _ in layer.issues
    )
    exceptions_blocked = any(
        level == "BLOCKING" and location == exceptions_location
        for level, location, _ in layer.issues
    )

    issues = []

    if not rules_blocked and isinstance(layer.rules_registry, dict):
        rules = layer.rules_registry["rules"]

        for rule_id, rule in rules.items():
            if rule_id in objects:
                issues.append(
                    (
                        "BLOCKING",
                        rules_location,
                        f"rule ID collides with canonical knowledge object ID: {rule_id}",
                    )
                )

            traceability = rule.get("traceability") or {}
            for object_id in traceability.get("object_ids", []) or []:
                if object_id not in objects:
                    issues.append(
                        (
                            "BLOCKING",
                            rules_location,
                            f"rules.{rule_id}: unknown traceability object_id: {object_id}",
                        )
                    )

    if (
        not rules_blocked
        and not exceptions_blocked
        and isinstance(layer.rules_registry, dict)
        and isinstance(layer.exception_registry, dict)
    ):
        rules = layer.rules_registry["rules"]

        for exception_id, exception in layer.exception_registry["exceptions"].items():
            rule_id = exception["rule_id"]
            if rule_id not in rules:
                issues.append(
                    (
                        "BLOCKING",
                        exceptions_location,
                        f"exceptions.{exception_id}: unknown rule_id: {rule_id}",
                    )
                )

            decision_id = exception["decision_id"]
            decision = objects.get(decision_id)
            if decision is None or decision.get("data", {}).get("type") != "decision":
                issues.append(
                    (
                        "BLOCKING",
                        exceptions_location,
                        f"exceptions.{exception_id}: unknown decision_id: {decision_id}",
                    )
                )

    return issues


def inspect_rules_layer(root, config=None):
    root = Path(root).resolve()
    config = load_yaml(root / "project.yaml") if config is None else config
    issues = []

    tooling = config.get("tooling", {}) if isinstance(config, dict) else {}
    if not isinstance(tooling, dict):
        tooling = {}

    marker_present = "rules_schema_version" in tooling
    marker = tooling.get("rules_schema_version")

    rules_path = root / RULES_REGISTRY
    exceptions_path = root / RULE_EXCEPTIONS_REGISTRY
    physical_present = rules_path.exists() or exceptions_path.exists()

    if not marker_present:
        if physical_present:
            issues.append(
                (
                    "ERROR",
                    ".project/policies",
                    "Rules files exist without tooling.rules_schema_version activation",
                )
            )
        return RuleLayer(False, issues)

    if type(marker) is not int or marker != RULES_SCHEMA_VERSION:
        issues.append(
            (
                "BLOCKING",
                "project.yaml",
                f"unsupported rules_schema_version: {marker!r}",
            )
        )
        return RuleLayer(True, issues)

    if not rules_path.is_file():
        issues.append(
            (
                "BLOCKING",
                RULES_REGISTRY.as_posix(),
                "activated Rules layer is missing its rules registry",
            )
        )

    if not exceptions_path.is_file():
        issues.append(
            (
                "BLOCKING",
                RULE_EXCEPTIONS_REGISTRY.as_posix(),
                "activated Rules layer is missing its exception registry",
            )
        )

    rules_registry = None
    exception_registry = None

    if rules_path.is_file():
        try:
            _ensure_registry_path(root, rules_path, "Rules registry")
            rules_registry, messages = _load_registry(
                rules_path,
                "Rules registry",
                "rules.schema.json",
            )
            issues.extend(
                ("BLOCKING", RULES_REGISTRY.as_posix(), message)
                for message in messages
            )
        except RuleRegistryError as exc:
            issues.append(
                ("BLOCKING", RULES_REGISTRY.as_posix(), str(exc))
            )

    if exceptions_path.is_file():
        try:
            _ensure_registry_path(root, exceptions_path, "Rule exceptions registry")
            exception_registry, messages = _load_registry(
                exceptions_path,
                "Rule exceptions registry",
                "rule-exceptions.schema.json",
            )
            issues.extend(
                ("BLOCKING", RULE_EXCEPTIONS_REGISTRY.as_posix(), message)
                for message in messages
            )
        except RuleRegistryError as exc:
            issues.append(
                (
                    "BLOCKING",
                    RULE_EXCEPTIONS_REGISTRY.as_posix(),
                    str(exc),
                )
            )

    if rules_registry is not None:
        rules_location = RULES_REGISTRY.as_posix()
        rules_invalid = any(
            level == "BLOCKING" and location == rules_location
            for level, location, _ in issues
        )
        if not rules_invalid:
            issues.extend(
                ("BLOCKING", rules_location, message)
                for message in _rule_semantic_messages(rules_registry)
            )

    if exception_registry is not None:
        exceptions_location = RULE_EXCEPTIONS_REGISTRY.as_posix()
        exceptions_invalid = any(
            level == "BLOCKING" and location == exceptions_location
            for level, location, _ in issues
        )
        if not exceptions_invalid:
            issues.extend(
                ("BLOCKING", exceptions_location, message)
                for message in _exception_semantic_messages(exception_registry)
            )


    return RuleLayer(
        True,
        issues,
        rules_registry=rules_registry,
        exception_registry=exception_registry,
    )
