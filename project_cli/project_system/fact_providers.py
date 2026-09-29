"""Packaged, allowlisted Fact Provider boundary for Executable Rules."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
import re
from types import MappingProxyType

from .rule_evidence import canonical_sha256
from .rule_scope import (
    RuleScopeError,
    canonical_rule_path,
    canonicalize_evaluation_paths,
    scope_pattern_matches,
)


_PACKAGE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class FactProviderError(RuntimeError):
    """A trustworthy normalized fact set cannot be produced."""


@dataclass(frozen=True)
class DependencyFact:
    """One language-neutral declared dependency edge."""

    source_path: str
    directive: str
    target_kind: str
    target_path: str | None
    target_package: str | None
    target_uri: str


@dataclass(frozen=True)
class FactProviderResult:
    """Deterministic output of one packaged Fact Provider."""

    provider_id: str
    provider_version: str
    facts: tuple[DependencyFact, ...]
    fact_set_sha256: str
    inspected_source_paths: tuple[str, ...]


@dataclass(frozen=True)
class FactProviderSpec:
    """Allowlisted provider identity and implementation."""

    provider_id: str
    version: str
    implementation: Callable[
        [Path, tuple[str, ...] | None],
        tuple[Sequence[DependencyFact], Sequence[str]],
    ]
    global_input_patterns: tuple[str, ...] = ()


def dependency_fact_to_dict(fact):
    if not isinstance(fact, DependencyFact):
        raise FactProviderError("provider returned a non-DependencyFact value")
    return {
        "source_path": fact.source_path,
        "directive": fact.directive,
        "target_kind": fact.target_kind,
        "target_path": fact.target_path,
        "target_package": fact.target_package,
        "target_uri": fact.target_uri,
    }


def _fact_sort_key(fact):
    return (
        fact.source_path,
        fact.directive,
        fact.target_kind,
        fact.target_path or "",
        fact.target_package or "",
        fact.target_uri,
    )


def _validated_fact(fact):
    if not isinstance(fact, DependencyFact):
        raise FactProviderError("provider returned a non-DependencyFact value")
    if not isinstance(fact.source_path, str):
        raise FactProviderError("dependency fact source_path must be a string")
    if not isinstance(fact.directive, str):
        raise FactProviderError("dependency fact directive must be a string")
    if not isinstance(fact.target_kind, str):
        raise FactProviderError("dependency fact target_kind must be a string")
    if fact.target_path is not None and not isinstance(fact.target_path, str):
        raise FactProviderError("dependency fact target_path must be a string or null")
    if fact.target_package is not None and not isinstance(fact.target_package, str):
        raise FactProviderError("dependency fact target_package must be a string or null")
    if not isinstance(fact.target_uri, str):
        raise FactProviderError("dependency fact target_uri must be a string")
    try:
        canonical_rule_path(fact.source_path, "fact source_path", pattern=False)
        if fact.target_path is not None:
            canonical_rule_path(fact.target_path, "fact target_path", pattern=False)
    except RuleScopeError as exc:
        raise FactProviderError(str(exc)) from exc
    if fact.directive not in {"import", "export", "part", "part_of"}:
        raise FactProviderError(
            "dependency fact directive must be import, export, part, or part_of"
        )
    if fact.target_kind not in {"project", "package", "dart_sdk"}:
        raise FactProviderError("dependency fact target_kind is unsupported")
    if (
        not isinstance(fact.target_uri, str)
        or not fact.target_uri
        or fact.target_uri != fact.target_uri.strip()
        or "\\" in fact.target_uri
        or "\x00" in fact.target_uri
    ):
        raise FactProviderError("dependency fact target_uri is malformed")
    if fact.target_kind == "project":
        if fact.target_path is None or fact.target_package is not None:
            raise FactProviderError("project dependency fact identity is malformed")
    elif fact.target_kind == "package":
        if (
            fact.target_path is not None
            or not isinstance(fact.target_package, str)
            or not _PACKAGE_NAME_RE.fullmatch(fact.target_package)
        ):
            raise FactProviderError("package dependency fact identity is malformed")
    elif fact.target_path is not None or fact.target_package is not None:
        raise FactProviderError("Dart SDK dependency fact identity is malformed")
    return fact


def _validated_provider_spec(provider_id):
    if not isinstance(provider_id, str) or provider_id not in FACT_PROVIDER_REGISTRY:
        raise FactProviderError(f"unknown Fact Provider: {provider_id!r}")
    spec = FACT_PROVIDER_REGISTRY[provider_id]
    if not isinstance(spec, FactProviderSpec):
        raise FactProviderError(f"Fact Provider {provider_id!r} has malformed metadata")
    if spec.provider_id != provider_id:
        raise FactProviderError(f"Fact Provider {provider_id!r} identity is inconsistent")
    if (
        not isinstance(spec.version, str)
        or not spec.version
        or spec.version != spec.version.strip()
    ):
        raise FactProviderError(f"Fact Provider {provider_id!r} version is malformed")
    if not isinstance(spec.global_input_patterns, tuple):
        raise FactProviderError(
            f"Fact Provider {provider_id!r} global input patterns are malformed"
        )
    patterns = []
    for index, pattern in enumerate(spec.global_input_patterns):
        try:
            patterns.append(
                canonical_rule_path(
                    pattern,
                    f"Fact Provider {provider_id!r} global_input_patterns[{index}]",
                    pattern=True,
                )
            )
        except RuleScopeError as exc:
            raise FactProviderError(str(exc)) from exc
    if tuple(sorted(set(patterns))) != spec.global_input_patterns:
        raise FactProviderError(
            f"Fact Provider {provider_id!r} global input patterns must be sorted and unique"
        )
    return spec


def resolve_fact_provider_evaluation(
    provider_id,
    evaluation_paths,
    *,
    additional_global_input_patterns=(),
):
    """Resolve bounded paths or a deliberate project-wide invalidation."""
    spec = _validated_provider_spec(provider_id)
    try:
        paths = canonicalize_evaluation_paths(evaluation_paths)
    except RuleScopeError as exc:
        raise FactProviderError(str(exc)) from exc
    if paths is None:
        return None, "project_wide"
    if (
        not isinstance(additional_global_input_patterns, Sequence)
        or isinstance(additional_global_input_patterns, (str, bytes))
    ):
        raise FactProviderError(
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
            raise FactProviderError(str(exc)) from exc
    patterns = tuple(sorted(set(spec.global_input_patterns + tuple(additional))))
    if any(
        scope_pattern_matches(pattern, path)
        for pattern in patterns
        for path in paths
    ):
        return None, "project_wide_invalidation"
    return paths, "bounded"


def validate_fact_provider_result(
    result,
    *,
    expected_provider_id,
    bounded_evaluation_paths=None,
):
    """Validate normalized provider evidence at a consumer trust boundary."""
    spec = _validated_provider_spec(expected_provider_id)
    if not isinstance(result, FactProviderResult):
        raise FactProviderError("Fact Provider returned a malformed result")
    if result.provider_id != expected_provider_id:
        raise FactProviderError("Fact Provider result identity is inconsistent")
    if result.provider_version != spec.version:
        raise FactProviderError("Fact Provider result version is inconsistent")
    if not isinstance(result.facts, tuple):
        raise FactProviderError("Fact Provider result facts must be a tuple")
    if not isinstance(result.inspected_source_paths, tuple):
        raise FactProviderError(
            "Fact Provider inspected source paths must be a tuple"
        )
    normalized_facts = tuple(
        sorted({_validated_fact(fact) for fact in result.facts}, key=_fact_sort_key)
    )
    if result.facts != normalized_facts:
        raise FactProviderError(
            "Fact Provider result facts must be sorted and deduplicated"
        )
    try:
        inspected = canonicalize_evaluation_paths(result.inspected_source_paths)
        bounded = canonicalize_evaluation_paths(bounded_evaluation_paths)
    except RuleScopeError as exc:
        raise FactProviderError(str(exc)) from exc
    if inspected != result.inspected_source_paths:
        raise FactProviderError(
            "Fact Provider inspected source paths must be sorted and deduplicated"
        )
    if bounded is not None:
        unexpected = sorted(set(inspected) - set(bounded))
        if unexpected:
            raise FactProviderError(
                "provider reported inspected paths outside bounded evaluation_paths: "
                + ", ".join(unexpected)
            )
    inspected_set = set(inspected)
    uninspected_sources = sorted(
        {fact.source_path for fact in normalized_facts if fact.source_path not in inspected_set}
    )
    if uninspected_sources:
        raise FactProviderError(
            "provider returned facts for source paths not reported as inspected: "
            + ", ".join(uninspected_sources)
        )
    serialized = [dependency_fact_to_dict(fact) for fact in normalized_facts]
    expected_hash = canonical_sha256(serialized)
    if (
        not isinstance(result.fact_set_sha256, str)
        or not _SHA256_RE.fullmatch(result.fact_set_sha256)
        or result.fact_set_sha256 != expected_hash
    ):
        raise FactProviderError("Fact Provider result fact_set_sha256 is inconsistent")
    return result


def evaluate_fact_provider(
    provider_id,
    project_root,
    *,
    evaluation_paths=None,
):
    """Execute one packaged provider over an unbounded or bounded path set."""
    spec = _validated_provider_spec(provider_id)
    try:
        paths = canonicalize_evaluation_paths(evaluation_paths)
    except RuleScopeError as exc:
        raise FactProviderError(str(exc)) from exc

    try:
        raw_facts, raw_inspected = spec.implementation(Path(project_root), paths)
    except FactProviderError:
        raise
    except Exception as exc:
        raise FactProviderError(
            f"Fact Provider {provider_id!r} failed unexpectedly: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    if (
        not isinstance(raw_facts, Sequence)
        or isinstance(raw_facts, (str, bytes))
        or not isinstance(raw_inspected, Sequence)
        or isinstance(raw_inspected, (str, bytes))
    ):
        raise FactProviderError("Fact Provider returned a malformed result")

    facts = tuple(sorted({_validated_fact(fact) for fact in raw_facts}, key=_fact_sort_key))
    try:
        inspected = canonicalize_evaluation_paths(tuple(raw_inspected))
    except RuleScopeError as exc:
        raise FactProviderError(f"provider returned unsafe inspected paths: {exc}") from exc

    serialized = [dependency_fact_to_dict(fact) for fact in facts]
    result = FactProviderResult(
        provider_id=spec.provider_id,
        provider_version=spec.version,
        facts=facts,
        fact_set_sha256=canonical_sha256(serialized),
        inspected_source_paths=inspected,
    )
    return validate_fact_provider_result(
        result,
        expected_provider_id=provider_id,
        bounded_evaluation_paths=paths,
    )


from .dart_facts import extract_dart_dependency_facts  # noqa: E402


FACT_PROVIDER_REGISTRY = MappingProxyType(
    {
        "dart.imports": FactProviderSpec(
            provider_id="dart.imports",
            version="1",
            implementation=extract_dart_dependency_facts,
            global_input_patterns=("pubspec.yaml",),
        ),
    }
)
