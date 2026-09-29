from pathlib import Path

import pytest

from project_system.fact_providers import (
    FACT_PROVIDER_REGISTRY,
    FactProviderError,
    dependency_fact_to_dict,
    evaluate_fact_provider,
)


def _write(root, relative, content):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_dart_provider_is_allowlisted_with_stable_identity():
    spec = FACT_PROVIDER_REGISTRY["dart.imports"]
    assert spec.provider_id == "dart.imports"
    assert spec.version == "1"
    assert callable(spec.implementation)


def test_unknown_provider_fails_closed(tmp_path):
    with pytest.raises(FactProviderError, match="unknown Fact Provider"):
        evaluate_fact_provider("python.imports", tmp_path)


def test_provider_result_is_language_neutral_and_deterministic(tmp_path):
    _write(tmp_path, "lib/a.dart", "import 'b.dart';\n")

    result = evaluate_fact_provider(
        "dart.imports",
        tmp_path,
        evaluation_paths=("lib/a.dart", "lib/a.dart"),
    )

    assert result.provider_id == "dart.imports"
    assert result.provider_version == "1"
    assert result.inspected_source_paths == ("lib/a.dart",)
    assert len(result.fact_set_sha256) == 64
    assert dependency_fact_to_dict(result.facts[0]) == {
        "source_path": "lib/a.dart",
        "directive": "import",
        "target_kind": "project",
        "target_path": "lib/b.dart",
        "target_package": None,
        "target_uri": "b.dart",
    }


def test_empty_bounded_source_set_inspects_nothing(tmp_path):
    path = tmp_path / "lib/bad.dart"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"\xff")

    result = evaluate_fact_provider(
        "dart.imports",
        tmp_path,
        evaluation_paths=(),
    )

    assert result.facts == ()
    assert result.inspected_source_paths == ()


def test_non_dart_bounded_paths_are_not_inspected(tmp_path):
    _write(tmp_path, "README.md", "not Dart")
    result = evaluate_fact_provider(
        "dart.imports",
        tmp_path,
        evaluation_paths=("README.md",),
    )
    assert result.facts == ()
    assert result.inspected_source_paths == ()


def test_project_wide_discovery_is_sorted_and_ignores_generated_outputs(tmp_path):
    _write(tmp_path, "lib/z.dart", "export 'a.dart';\n")
    _write(tmp_path, "lib/a.dart", "import 'z.dart';\n")
    _write(tmp_path, ".generated/fake.dart", "import 'bad.dart';\n")
    _write(tmp_path, "build/fake.dart", "import 'bad.dart';\n")
    _write(tmp_path, ".dart_tool/fake.dart", "import 'bad.dart';\n")

    result = evaluate_fact_provider("dart.imports", tmp_path)

    assert result.inspected_source_paths == ("lib/a.dart", "lib/z.dart")
    assert tuple(fact.source_path for fact in result.facts) == (
        "lib/a.dart",
        "lib/z.dart",
    )


def test_fact_hash_is_checkout_location_independent(tmp_path):
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    _write(first_root, "lib/a.dart", "import 'b.dart';\n")
    _write(second_root, "lib/a.dart", "import 'b.dart';\n")

    first = evaluate_fact_provider("dart.imports", first_root)
    second = evaluate_fact_provider("dart.imports", second_root)

    assert first.fact_set_sha256 == second.fact_set_sha256
    assert first.facts == second.facts


def test_dependency_change_changes_fact_hash(tmp_path):
    path = _write(tmp_path, "lib/a.dart", "import 'b.dart';\n")
    first = evaluate_fact_provider("dart.imports", tmp_path)
    path.write_text("import 'c.dart';\n", encoding="utf-8")
    second = evaluate_fact_provider("dart.imports", tmp_path)
    assert first.fact_set_sha256 != second.fact_set_sha256


@pytest.mark.parametrize(
    "paths",
    [
        ("../outside.dart",),
        ("/absolute.dart",),
        ("C:/absolute.dart",),
        ("lib\\a.dart",),
        ("lib/*.dart",),
    ],
)
def test_bounded_paths_reuse_stage7_canonical_path_contract(tmp_path, paths):
    with pytest.raises((FactProviderError, ValueError), match="evaluation_paths"):
        evaluate_fact_provider(
            "dart.imports",
            tmp_path,
            evaluation_paths=paths,
        )

def test_bounded_provider_cannot_report_inspected_path_outside_requested_set(
    tmp_path,
    monkeypatch,
):
    import project_system.fact_providers as provider_module
    from types import MappingProxyType

    def fake_provider(project_root, evaluation_paths):
        assert evaluation_paths == ("lib/a.dart",)
        return (), ("lib/b.dart",)

    monkeypatch.setattr(
        provider_module,
        "FACT_PROVIDER_REGISTRY",
        MappingProxyType(
            {
                "test.dependencies": provider_module.FactProviderSpec(
                    provider_id="test.dependencies",
                    version="1",
                    implementation=fake_provider,
                )
            }
        ),
    )

    with pytest.raises(FactProviderError, match="inspected|evaluation"):
        provider_module.evaluate_fact_provider(
            "test.dependencies",
            tmp_path,
            evaluation_paths=("lib/a.dart",),
        )


def test_provider_boundary_wraps_malformed_fact_as_fact_provider_error(
    tmp_path,
    monkeypatch,
):
    import project_system.fact_providers as provider_module
    from types import MappingProxyType

    malformed = provider_module.DependencyFact(
        source_path="lib/a.dart",
        directive="import",
        target_kind=["project"],
        target_path="lib/b.dart",
        target_package=None,
        target_uri="b.dart",
    )

    def fake_provider(project_root, evaluation_paths):
        return (malformed,), ("lib/a.dart",)

    monkeypatch.setattr(
        provider_module,
        "FACT_PROVIDER_REGISTRY",
        MappingProxyType(
            {
                "test.dependencies": provider_module.FactProviderSpec(
                    provider_id="test.dependencies",
                    version="1",
                    implementation=fake_provider,
                )
            }
        ),
    )

    with pytest.raises(FactProviderError):
        provider_module.evaluate_fact_provider(
            "test.dependencies",
            tmp_path,
            evaluation_paths=("lib/a.dart",),
        )

def test_provider_fact_source_must_be_reported_as_inspected(
    tmp_path,
    monkeypatch,
):
    import project_system.fact_providers as provider_module
    from types import MappingProxyType

    fact = provider_module.DependencyFact(
        source_path="lib/a.dart",
        directive="import",
        target_kind="project",
        target_path="lib/b.dart",
        target_package=None,
        target_uri="b.dart",
    )

    def fake_provider(project_root, evaluation_paths):
        return (fact,), ()

    monkeypatch.setattr(
        provider_module,
        "FACT_PROVIDER_REGISTRY",
        MappingProxyType(
            {
                "test.dependencies": provider_module.FactProviderSpec(
                    provider_id="test.dependencies",
                    version="1",
                    implementation=fake_provider,
                )
            }
        ),
    )

    with pytest.raises(FactProviderError, match="inspected"):
        provider_module.evaluate_fact_provider(
            "test.dependencies",
            tmp_path,
            evaluation_paths=("lib/a.dart",),
        )
