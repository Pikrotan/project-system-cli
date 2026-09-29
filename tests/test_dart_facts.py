import os
from pathlib import Path

import pytest

from project_system.fact_providers import FactProviderError, evaluate_fact_provider


def _write(root, relative, content):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _facts(root, *, evaluation_paths=None):
    return evaluate_fact_provider(
        "dart.imports",
        root,
        evaluation_paths=evaluation_paths,
    ).facts


def test_normalizes_relative_parent_own_package_external_and_sdk_dependencies(tmp_path):
    _write(tmp_path, "pubspec.yaml", "name: my_app\n")
    _write(
        tmp_path,
        "lib/domain/a.dart",
        """
import '../presentation/b.dart';
import 'package:my_app/domain/missing.dart';
import 'package:http/http.dart';
import 'dart:async';
export 'local.dart';
""",
    )

    facts = _facts(tmp_path)

    assert sorted(fact.target_kind for fact in facts) == [
        "dart_sdk",
        "package",
        "project",
        "project",
        "project",
    ]
    selected = {fact.target_uri: fact for fact in facts}
    assert selected["../presentation/b.dart"].target_path == "lib/presentation/b.dart"
    assert selected["package:my_app/domain/missing.dart"].target_path == "lib/domain/missing.dart"
    assert selected["package:http/http.dart"].target_package == "http"
    assert selected["package:http/http.dart"].target_path is None
    assert selected["dart:async"].target_kind == "dart_sdk"
    assert selected["local.dart"].directive == "export"


def test_conditional_import_and_export_emit_every_possible_uri_branch(tmp_path):
    _write(
        tmp_path,
        "lib/a.dart",
        """
import 'stub.dart'
    if (dart.library.io) 'io.dart'
    if (dart.library.html) 'web.dart';
export 'base.dart' if (dart.library.js_interop) 'web_export.dart';
""",
    )

    facts = _facts(tmp_path)

    assert [(fact.directive, fact.target_uri) for fact in facts] == [
        ("export", "base.dart"),
        ("export", "web_export.dart"),
        ("import", "io.dart"),
        ("import", "stub.dart"),
        ("import", "web.dart"),
    ]


def test_comments_and_arbitrary_strings_do_not_create_fake_dependencies(tmp_path):
    _write(
        tmp_path,
        "lib/a.dart",
        '''
// import 'line_fake.dart';
/*
  import 'block_fake.dart';
  /* export 'nested_fake.dart'; */
*/
const first = "import 'string_fake.dart';";
const second = r"export 'raw_fake.dart';";
const third = "${render("import 'interpolation_fake.dart';")}";
import 'real.dart';
''',
    )

    facts = _facts(tmp_path)
    assert [fact.target_uri for fact in facts] == ["real.dart"]


def test_multiline_directive_and_comments_between_tokens_are_supported(tmp_path):
    _write(
        tmp_path,
        "lib/a.dart",
        """
import /* before uri */ 'stub.dart'
  if /* before condition */ (dart.library.io && true) /* after condition */
  'io.dart'
  deferred as selected
  show value;
""",
    )
    facts = _facts(tmp_path)
    assert [fact.target_uri for fact in facts] == ["io.dart", "stub.dart"]


def test_duplicate_directives_are_deduplicated_and_sorted(tmp_path):
    _write(
        tmp_path,
        "lib/a.dart",
        "import 'z.dart';\nimport 'a.dart';\nimport 'z.dart';\n",
    )
    facts = _facts(tmp_path)
    assert [fact.target_uri for fact in facts] == ["a.dart", "z.dart"]


def test_missing_or_invalid_package_name_does_not_claim_package_ownership(tmp_path):
    _write(tmp_path, "pubspec.yaml", "name: Invalid-Name\n")
    _write(tmp_path, "lib/a.dart", "import 'package:my_app/a.dart';\n")
    fact = _facts(tmp_path)[0]
    assert fact.target_kind == "package"
    assert fact.target_package == "my_app"
    assert fact.target_path is None


def test_malformed_pubspec_fails_closed(tmp_path):
    _write(tmp_path, "pubspec.yaml", "name: [unterminated\n")
    _write(tmp_path, "lib/a.dart", "import 'a.dart';\n")
    with pytest.raises(FactProviderError, match="pubspec.yaml"):
        _facts(tmp_path)


@pytest.mark.parametrize(
    "content",
    [
        "name: my_app\nname: other_app\n",
        "defaults: &defaults {name: my_app}\nname: *defaults\n",
        "name: !!python/object/apply:os.system ['echo unsafe']\n",
    ],
)
def test_ambiguous_or_unsafe_pubspec_yaml_fails_closed(tmp_path, content):
    _write(tmp_path, "pubspec.yaml", content)
    _write(tmp_path, "lib/a.dart", "import 'a.dart';\n")
    with pytest.raises(FactProviderError, match="pubspec.yaml"):
        _facts(tmp_path)


@pytest.mark.parametrize(
    "uri",
    [
        "../../../outside.dart",
        "/absolute.dart",
        "package:http",
        "package:/http.dart",
        "package:http/../bad.dart",
        "package:http//bad.dart",
        "http://example.invalid/a.dart",
        "bad\\path.dart",
        "package:http/%2e%2e/bad.dart",
    ],
)
def test_unsafe_or_malformed_dependency_uri_fails_closed(tmp_path, uri):
    _write(tmp_path, "lib/a.dart", f"import '{uri}';\n")
    with pytest.raises(FactProviderError, match="URI|escapes"):
        _facts(tmp_path)


def test_invalid_utf8_source_fails_closed(tmp_path):
    path = tmp_path / "lib/a.dart"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"import 'a.dart';\xff")
    with pytest.raises(FactProviderError, match="UTF-8"):
        _facts(tmp_path)


def test_unreadable_source_fails_closed(tmp_path, monkeypatch):
    path = _write(tmp_path, "lib/a.dart", "import 'a.dart';\n")
    actual_read_bytes = Path.read_bytes

    def deny_selected(selected):
        if selected == path:
            raise PermissionError("simulated access denial")
        return actual_read_bytes(selected)

    monkeypatch.setattr(Path, "read_bytes", deny_selected)
    with pytest.raises(FactProviderError, match="cannot read Dart source"):
        _facts(tmp_path)


@pytest.mark.parametrize(
    "content",
    [
        "import 'package:$name/a.dart';\n",
        "import 'unterminated.dart'",
        "/* unterminated comment",
        "import prefix;\n",
    ],
)
def test_ambiguous_or_unsupported_directive_source_fails_closed(tmp_path, content):
    _write(tmp_path, "lib/a.dart", content)
    with pytest.raises(FactProviderError, match="Dart|directive|interpolation"):
        _facts(tmp_path)


def test_symlinked_bounded_source_fails_closed(tmp_path):
    outside = _write(tmp_path, "outside.dart", "import 'a.dart';\n")
    link = tmp_path / "lib/link.dart"
    link.parent.mkdir()
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("cannot create a symlink or junction")
    with pytest.raises(FactProviderError, match="symlink|reparse"):
        _facts(tmp_path, evaluation_paths=("lib/link.dart",))


def test_project_wide_discovery_rejects_nonignored_symlink_directory(tmp_path):
    outside = tmp_path / "outside"
    _write(outside, "a.dart", "import 'b.dart';\n")
    link = tmp_path / "linked"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("cannot create a symlink or junction")
    with pytest.raises(FactProviderError, match="symlink|reparse"):
        _facts(tmp_path)


def test_bounded_missing_dart_path_is_not_an_inspected_source(tmp_path):
    result = evaluate_fact_provider(
        "dart.imports",
        tmp_path,
        evaluation_paths=("lib/deleted.dart",),
    )
    assert result.facts == ()
    assert result.inspected_source_paths == ()

def test_part_directive_emits_project_dependency(tmp_path):
    _write(
        tmp_path,
        "lib/domain/library.dart",
        "part '../presentation/piece.dart';\n",
    )

    facts = _facts(tmp_path)

    assert len(facts) == 1
    assert facts[0].directive == "part"
    assert facts[0].target_kind == "project"
    assert facts[0].target_path == "lib/presentation/piece.dart"
    assert facts[0].target_uri == "../presentation/piece.dart"


def test_part_of_uri_emits_reverse_project_dependency_in_bounded_context(tmp_path):
    _write(
        tmp_path,
        "lib/presentation/piece.dart",
        "part of '../domain/library.dart';\n",
    )

    facts = _facts(
        tmp_path,
        evaluation_paths=("lib/presentation/piece.dart",),
    )

    assert len(facts) == 1
    assert facts[0].directive == "part_of"
    assert facts[0].target_kind == "project"
    assert facts[0].target_path == "lib/domain/library.dart"
    assert facts[0].target_uri == "../domain/library.dart"


def test_named_part_of_fails_closed_instead_of_losing_dependency(tmp_path):
    _write(
        tmp_path,
        "lib/piece.dart",
        "part of legacy.library;\n",
    )

    with pytest.raises(FactProviderError, match="part|library|unsupported"):
        _facts(
            tmp_path,
            evaluation_paths=("lib/piece.dart",),
        )


@pytest.mark.parametrize(
    "content",
    [
        "import 'a.dart' deferred;\n",
        "import 'a.dart' deferred show A;\n",
    ],
)
def test_deferred_import_requires_as_prefix(tmp_path, content):
    _write(tmp_path, "lib/a.dart", content)

    with pytest.raises(FactProviderError, match="deferred|prefix|unsupported"):
        _facts(tmp_path)
