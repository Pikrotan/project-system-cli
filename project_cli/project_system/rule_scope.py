"""Shared canonical path and anchored glob semantics for Executable Rules."""

from collections.abc import Sequence
from fnmatch import fnmatchcase
from functools import lru_cache
from pathlib import PurePosixPath


class RuleScopeError(ValueError):
    """A Rule scope pattern or concrete evaluation path is malformed."""


def canonical_rule_path(value, label, *, pattern):
    """Validate one canonical repository-relative POSIX path or pattern."""
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or "\\" in value
        or ":" in value
        or "\x00" in value
    ):
        raise RuleScopeError(
            f"{label} must be a canonical repository-relative POSIX path"
        )
    if not pattern and any(character in value for character in "*?[]"):
        raise RuleScopeError(f"{label} must not contain glob syntax")
    pure = PurePosixPath(value)
    if (
        pure.is_absolute()
        or not pure.parts
        or any(part in {"", ".", ".."} for part in pure.parts)
        or pure.parts[0].casefold() == ".git"
        or pure.as_posix() != value
    ):
        raise RuleScopeError(
            f"{label} must be a canonical repository-relative POSIX path"
        )
    return value


def canonicalize_evaluation_paths(value):
    """Return a stable path-set tuple while preserving ``None`` as unbounded."""
    if value is None:
        return None
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise RuleScopeError("evaluation_paths must be a sequence of concrete paths")
    paths = (
        canonical_rule_path(
            item,
            f"evaluation_paths[{index}]",
            pattern=False,
        )
        for index, item in enumerate(value)
    )
    return tuple(sorted(set(paths)))


def scope_pattern_matches(
    pattern,
    path,
    *,
    pattern_label="scope pattern",
    path_label="evaluation path",
):
    """Match one anchored canonical path with segment-aware v1 glob syntax."""
    pattern = canonical_rule_path(pattern, pattern_label, pattern=True)
    path = canonical_rule_path(path, path_label, pattern=False)
    pattern_parts = PurePosixPath(pattern).parts
    path_parts = PurePosixPath(path).parts

    @lru_cache(maxsize=None)
    def matches(pattern_index, path_index):
        if pattern_index == len(pattern_parts):
            return path_index == len(path_parts)
        selected = pattern_parts[pattern_index]
        if selected == "**":
            return matches(pattern_index + 1, path_index) or (
                path_index < len(path_parts)
                and matches(pattern_index, path_index + 1)
            )
        return (
            path_index < len(path_parts)
            and fnmatchcase(path_parts[path_index], selected)
            and matches(pattern_index + 1, path_index + 1)
        )

    return matches(0, 0)
