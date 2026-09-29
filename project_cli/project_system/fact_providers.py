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
)


_PACKAGE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")


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


def evaluate_fact_provider(
    provider_id,
    project_root,
    *,
    evaluation_paths=None,
):
    """Execute one packaged provider over an unbounded or bounded path set."""
    if not isinstance(provider_id, str) or provider_id not in FACT_PROVIDER_REGISTRY:
        raise FactProviderError(f"unknown Fact Provider: {provider_id!r}")
    try:
        paths = canonicalize_evaluation_paths(evaluation_paths)
    except RuleScopeError as exc:
        raise FactProviderError(str(exc)) from exc

    spec = FACT_PROVIDER_REGISTRY[provider_id]
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

    if paths is not None:
        unexpected_inspected = sorted(set(inspected) - set(paths))
        if unexpected_inspected:
            raise FactProviderError(
                "provider reported inspected paths outside bounded evaluation_paths: "
                + ", ".join(unexpected_inspected)
            )

    inspected_set = set(inspected)
    uninspected_sources = sorted(
        {fact.source_path for fact in facts if fact.source_path not in inspected_set}
    )
    if uninspected_sources:
        raise FactProviderError(
            "provider returned facts for source paths not reported as inspected: "
            + ", ".join(uninspected_sources)
        )

    serialized = [dependency_fact_to_dict(fact) for fact in facts]
    return FactProviderResult(
        provider_id=spec.provider_id,
        provider_version=spec.version,
        facts=facts,
        fact_set_sha256=canonical_sha256(serialized),
        inspected_source_paths=inspected,
    )


from .dart_facts import extract_dart_dependency_facts  # noqa: E402


FACT_PROVIDER_REGISTRY = MappingProxyType(
    {
        "dart.imports": FactProviderSpec(
            provider_id="dart.imports",
            version="1",
            implementation=extract_dart_dependency_facts,
        ),
    }
)
