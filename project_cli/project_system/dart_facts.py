"""Bounded lexical extraction of normalized Dart dependency facts."""

from collections.abc import Mapping
from dataclasses import dataclass
import os
from pathlib import Path, PurePosixPath
import re
import stat

import yaml

from .fact_providers import DependencyFact, FactProviderError
from .frontmatter import StrictSafeLoader
from .rule_scope import RuleScopeError, canonical_rule_path


MAX_DART_SOURCE_BYTES = 4 * 1024 * 1024
MAX_PUBSPEC_BYTES = 1024 * 1024
IGNORED_DIRECTORY_NAMES = frozenset(
    {".git", ".generated", ".dart_tool", ".pub-cache", "build"}
)
_IDENTIFIER_RE = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")
_PACKAGE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_DART_SDK_URI_RE = re.compile(r"^dart:[a-z][a-z0-9_.-]*(?:/[A-Za-z0-9_.-]+)*$")


@dataclass(frozen=True)
class _Token:
    kind: str
    value: str
    offset: int
    has_escape: bool = False
    interpolated: bool = False


def _path_has_reparse(path):
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise FactProviderError(f"cannot inspect repository path {path}: {exc}") from exc
    if stat.S_ISLNK(info.st_mode):
        return True
    attributes = getattr(info, "st_file_attributes", 0)
    flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & flag)


def _project_root(value):
    selected = Path(value)
    if selected.is_symlink() or (
        selected.exists() and getattr(selected, "is_junction", lambda: False)()
    ):
        raise FactProviderError("project root is a symlink or reparse point")
    try:
        root = selected.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise FactProviderError(f"cannot resolve project root: {exc}") from exc
    if not root.is_dir():
        raise FactProviderError("project root is not a directory")
    if _path_has_reparse(root):
        raise FactProviderError("project root is a symlink or reparse point")
    return root


def _safe_existing_file(root, relative, *, label):
    try:
        canonical_rule_path(relative, label, pattern=False)
    except RuleScopeError as exc:
        raise FactProviderError(str(exc)) from exc
    current = root
    parts = PurePosixPath(relative).parts
    for index, part in enumerate(parts):
        current = current / part
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise FactProviderError(f"cannot inspect {label} {relative!r}: {exc}") from exc
        attributes = getattr(info, "st_file_attributes", 0)
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if stat.S_ISLNK(info.st_mode) or attributes & reparse_flag:
            raise FactProviderError(
                f"{label} contains a symlink or reparse point: {relative}"
            )
        if index < len(parts) - 1 and not stat.S_ISDIR(info.st_mode):
            raise FactProviderError(f"{label} parent is not a directory: {relative}")
    if not stat.S_ISREG(info.st_mode):
        raise FactProviderError(f"{label} is not a regular file: {relative}")
    try:
        current.resolve(strict=True).relative_to(root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise FactProviderError(f"{label} escapes project root: {relative}") from exc
    return current


def _discover_dart_paths(root):
    discovered = []
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
        except OSError as exc:
            raise FactProviderError(f"cannot enumerate Dart source directory: {exc}") from exc
        for entry in entries:
            if entry.name in IGNORED_DIRECTORY_NAMES:
                continue
            path = Path(entry.path)
            if _path_has_reparse(path):
                raise FactProviderError(
                    f"Dart source discovery encountered a symlink or reparse point: {path}"
                )
            try:
                if entry.is_dir(follow_symlinks=False):
                    pending.append(path)
                elif entry.is_file(follow_symlinks=False) and entry.name.endswith(".dart"):
                    discovered.append(path.relative_to(root).as_posix())
            except OSError as exc:
                raise FactProviderError(f"cannot inspect Dart source candidate: {path}") from exc
    return tuple(sorted(discovered))


def _source_paths(root, evaluation_paths):
    candidates = (
        _discover_dart_paths(root)
        if evaluation_paths is None
        else tuple(path for path in evaluation_paths if path.endswith(".dart"))
    )
    selected = []
    for relative in candidates:
        path = _safe_existing_file(root, relative, label="Dart source path")
        if path is not None:
            selected.append((relative, path))
    return tuple(selected)


def _read_utf8(path, relative, *, maximum, label):
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise FactProviderError(f"cannot read {label} {relative!r}: {exc}") from exc
    if len(raw) > maximum:
        raise FactProviderError(f"{label} {relative!r} exceeds {maximum} bytes")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise FactProviderError(f"{label} {relative!r} must be UTF-8") from exc


def _package_name(root):
    path = _safe_existing_file(root, "pubspec.yaml", label="pubspec.yaml")
    if path is None:
        return None
    text = _read_utf8(
        path,
        "pubspec.yaml",
        maximum=MAX_PUBSPEC_BYTES,
        label="pubspec.yaml",
    )
    try:
        for token in yaml.scan(text, Loader=yaml.SafeLoader):
            if isinstance(token, (yaml.tokens.AnchorToken, yaml.tokens.AliasToken)):
                raise ValueError("YAML anchors and aliases are not allowed")
        document = yaml.load(text, Loader=StrictSafeLoader)
    except Exception as exc:
        raise FactProviderError(f"cannot parse pubspec.yaml safely: {exc}") from exc
    if not isinstance(document, Mapping):
        return None
    name = document.get("name")
    return name if isinstance(name, str) and _PACKAGE_NAME_RE.fullmatch(name) else None


def _scan_string(text, offset, *, raw, quote_offset):
    quote = text[quote_offset]
    triple = text.startswith(quote * 3, quote_offset)
    width = 3 if triple else 1
    index = quote_offset + width
    content = []
    has_escape = False
    interpolated = False
    while index < len(text):
        if text.startswith(quote * width, index):
            return (
                _Token(
                    "STRING",
                    "".join(content),
                    offset,
                    has_escape=has_escape,
                    interpolated=interpolated,
                ),
                index + width,
            )
        character = text[index]
        if not triple and character in "\r\n":
            raise FactProviderError(
                f"unterminated Dart string literal at offset {offset}"
            )
        if not raw and character == "\\":
            has_escape = True
            if index + 1 >= len(text):
                raise FactProviderError(
                    f"unterminated Dart string escape at offset {offset}"
                )
            content.extend((character, text[index + 1]))
            index += 2
            continue
        if not raw and character == "$":
            interpolated = True
            if index + 1 < len(text) and text[index + 1] == "{":
                end = _scan_interpolation(text, index + 2, offset)
                content.append(text[index:end])
                index = end
                continue
            identifier = _IDENTIFIER_RE.match(text, index + 1)
            if identifier:
                content.append(text[index:identifier.end()])
                index = identifier.end()
                continue
        content.append(character)
        index += 1
    raise FactProviderError(f"unterminated Dart string literal at offset {offset}")


def _scan_interpolation(text, index, string_offset):
    depth = 1
    while index < len(text):
        if text.startswith("//", index):
            newline = text.find("\n", index + 2)
            index = len(text) if newline < 0 else newline + 1
            continue
        if text.startswith("/*", index):
            comment_depth = 1
            index += 2
            while index < len(text) and comment_depth:
                if text.startswith("/*", index):
                    comment_depth += 1
                    index += 2
                elif text.startswith("*/", index):
                    comment_depth -= 1
                    index += 2
                else:
                    index += 1
            if comment_depth:
                raise FactProviderError(
                    f"unterminated Dart block comment in string interpolation at "
                    f"offset {string_offset}"
                )
            continue
        character = text[index]
        if character in {"r", "R"} and index + 1 < len(text) and text[index + 1] in {"'", '"'}:
            _, index = _scan_string(
                text,
                index,
                raw=True,
                quote_offset=index + 1,
            )
            continue
        if character in {"'", '"'}:
            _, index = _scan_string(
                text,
                index,
                raw=False,
                quote_offset=index,
            )
            continue
        if character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                return index + 1
        index += 1
    raise FactProviderError(
        f"unterminated Dart string interpolation at offset {string_offset}"
    )


def _tokens(text):
    tokens = []
    index = 0
    while index < len(text):
        character = text[index]
        if character.isspace():
            index += 1
            continue
        if text.startswith("//", index):
            newline = text.find("\n", index + 2)
            index = len(text) if newline < 0 else newline + 1
            continue
        if text.startswith("/*", index):
            start = index
            depth = 1
            index += 2
            while index < len(text) and depth:
                if text.startswith("/*", index):
                    depth += 1
                    index += 2
                elif text.startswith("*/", index):
                    depth -= 1
                    index += 2
                else:
                    index += 1
            if depth:
                raise FactProviderError(
                    f"unterminated Dart block comment at offset {start}"
                )
            continue
        if character in {"r", "R"} and index + 1 < len(text) and text[index + 1] in {"'", '"'}:
            token, index = _scan_string(
                text,
                index,
                raw=True,
                quote_offset=index + 1,
            )
            tokens.append(token)
            continue
        if character in {"'", '"'}:
            token, index = _scan_string(
                text,
                index,
                raw=False,
                quote_offset=index,
            )
            tokens.append(token)
            continue
        match = _IDENTIFIER_RE.match(text, index)
        if match:
            tokens.append(_Token("IDENT", match.group(0), index))
            index = match.end()
            continue
        tokens.append(_Token("SYMBOL", character, index))
        index += 1
    return tuple(tokens)


def _directive_uri(token, directive):
    if token.kind != "STRING":
        raise FactProviderError(
            f"Dart {directive} directive requires a string URI at offset {token.offset}"
        )
    if token.interpolated:
        raise FactProviderError(
            f"Dart {directive} directive URI interpolation is unsupported"
        )
    if token.has_escape:
        raise FactProviderError(
            f"Dart {directive} directive URI escapes are unsupported"
        )
    if not token.value:
        raise FactProviderError(f"Dart {directive} directive URI is empty")
    return token.value


def _expect_identifier(tokens, index, label):
    if index >= len(tokens) or tokens[index].kind != "IDENT":
        raise FactProviderError(f"Dart directive requires {label}")
    return index + 1


def _parse_directive(tokens, start):
    directive = tokens[start].value
    index = start + 1
    if index >= len(tokens):
        raise FactProviderError(f"unterminated Dart {directive} directive")
    uris = [_directive_uri(tokens[index], directive)]
    index += 1

    while index < len(tokens) and tokens[index].kind == "IDENT" and tokens[index].value == "if":
        index += 1
        if index >= len(tokens) or tokens[index].value != "(":
            raise FactProviderError(f"Dart {directive} conditional requires '('")
        depth = 1
        index += 1
        condition_start = index
        while index < len(tokens) and depth:
            if tokens[index].value == "(":
                depth += 1
            elif tokens[index].value == ")":
                depth -= 1
            if depth:
                index += 1
        if depth or index == condition_start:
            raise FactProviderError(f"malformed Dart {directive} conditional")
        index += 1
        if index >= len(tokens):
            raise FactProviderError(f"Dart {directive} conditional URI is missing")
        uris.append(_directive_uri(tokens[index], directive))
        index += 1

    deferred = False
    if directive == "import" and index < len(tokens) and tokens[index].value == "deferred":
        deferred = True
        index += 1
        if index >= len(tokens) or tokens[index].value != "as":
            raise FactProviderError(
                "Dart deferred import requires an 'as' prefix"
            )

    if directive == "import" and index < len(tokens) and tokens[index].value == "as":
        index = _expect_identifier(tokens, index + 1, "an import prefix")
    elif deferred:
        raise FactProviderError("Dart deferred import requires an 'as' prefix")

    while index < len(tokens) and tokens[index].value in {"show", "hide"}:
        index = _expect_identifier(tokens, index + 1, "a combinator identifier")
        while index < len(tokens) and tokens[index].value == ",":
            index = _expect_identifier(tokens, index + 1, "a combinator identifier")

    if index >= len(tokens) or tokens[index].value != ";":
        raise FactProviderError(f"unsupported or unterminated Dart {directive} directive")
    return tuple(uris), index + 1


def _parse_part_directive(tokens, start):
    index = start + 1
    if index >= len(tokens):
        raise FactProviderError("unterminated Dart part directive")

    if tokens[index].kind == "IDENT" and tokens[index].value == "of":
        index += 1
        if index >= len(tokens):
            raise FactProviderError("unterminated Dart part of directive")
        if tokens[index].kind != "STRING":
            raise FactProviderError(
                "named Dart part of directives are unsupported; URI form is required"
            )
        uri = _directive_uri(tokens[index], "part of")
        index += 1
        directive = "part_of"
    else:
        uri = _directive_uri(tokens[index], "part")
        index += 1
        directive = "part"

    if index >= len(tokens) or tokens[index].value != ";":
        raise FactProviderError(
            f"unsupported or unterminated Dart {directive.replace('_', ' ')} directive"
        )
    return directive, uri, index + 1


def _dependency_uris(text):
    tokens = _tokens(text)
    selected = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if (
            token.kind == "IDENT"
            and token.value in {"import", "export"}
            and (index == 0 or tokens[index - 1].value != ".")
        ):
            uris, index = _parse_directive(tokens, index)
            selected.extend((token.value, uri) for uri in uris)
            continue

        if (
            token.kind == "IDENT"
            and token.value == "part"
            and (index == 0 or tokens[index - 1].value != ".")
        ):
            directive, uri, index = _parse_part_directive(tokens, index)
            selected.append((directive, uri))
            continue

        index += 1
    return tuple(selected)


def _canonical_package_path(value, uri):
    try:
        return canonical_rule_path(value, f"package URI path in {uri!r}", pattern=False)
    except RuleScopeError as exc:
        raise FactProviderError(f"malformed package URI {uri!r}: {exc}") from exc


def _project_relative_target(source_path, uri):
    if (
        not uri
        or uri.startswith("/")
        or "\\" in uri
        or "\x00" in uri
        or ":" in uri
        or "?" in uri
        or "#" in uri
        or "%" in uri
        or any(character.isspace() for character in uri)
    ):
        raise FactProviderError(f"malformed relative dependency URI: {uri!r}")
    parts = list(PurePosixPath(source_path).parent.parts)
    segments = uri.split("/")
    if any(segment == "" for segment in segments):
        raise FactProviderError(f"malformed relative dependency URI: {uri!r}")
    for segment in segments:
        if segment == ".":
            continue
        if segment == "..":
            if not parts:
                raise FactProviderError(
                    f"relative dependency URI escapes project root: {uri!r}"
                )
            parts.pop()
        else:
            parts.append(segment)
    target = "/".join(parts)
    try:
        return canonical_rule_path(target, f"dependency URI target {uri!r}", pattern=False)
    except RuleScopeError as exc:
        raise FactProviderError(f"malformed relative dependency URI {uri!r}: {exc}") from exc


def _dependency_fact(source_path, directive, uri, own_package):
    if (
        any(character.isspace() for character in uri)
        or "\\" in uri
        or "\x00" in uri
        or "%" in uri
    ):
        raise FactProviderError(f"malformed dependency URI: {uri!r}")
    if uri.startswith("dart:"):
        if not _DART_SDK_URI_RE.fullmatch(uri):
            raise FactProviderError(f"malformed Dart SDK URI: {uri!r}")
        return DependencyFact(source_path, directive, "dart_sdk", None, None, uri)
    if uri.startswith("package:"):
        remainder = uri[len("package:"):]
        if "/" not in remainder:
            raise FactProviderError(f"malformed package URI: {uri!r}")
        package, path = remainder.split("/", 1)
        if not _PACKAGE_NAME_RE.fullmatch(package):
            raise FactProviderError(f"malformed package URI: {uri!r}")
        path = _canonical_package_path(path, uri)
        if package == own_package:
            target = _canonical_package_path(f"lib/{path}", uri)
            return DependencyFact(source_path, directive, "project", target, None, uri)
        return DependencyFact(source_path, directive, "package", None, package, uri)
    if ":" in uri:
        raise FactProviderError(f"unsupported dependency URI scheme: {uri!r}")
    target = _project_relative_target(source_path, uri)
    return DependencyFact(source_path, directive, "project", target, None, uri)


def extract_dart_dependency_facts(project_root, evaluation_paths):
    """Return normalized facts and inspected source paths for ``dart.imports``."""
    root = _project_root(project_root)
    sources = _source_paths(root, evaluation_paths)
    if not sources:
        return (), ()
    own_package = _package_name(root)
    facts = []
    inspected = []
    for relative, path in sources:
        text = _read_utf8(
            path,
            relative,
            maximum=MAX_DART_SOURCE_BYTES,
            label="Dart source",
        )
        for directive, uri in _dependency_uris(text):
            facts.append(_dependency_fact(relative, directive, uri, own_package))
        inspected.append(relative)
    return tuple(facts), tuple(inspected)
