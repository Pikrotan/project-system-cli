from pathlib import Path

import pytest

from project_system.fact_providers import FactProviderError
from project_system.rule_checkers import (
    CHECKER_REGISTRY,
    checker_contract_messages,
    evaluate_checker,
)
from project_system.rule_engine import RuleEvaluationContext


def context(
    root,
    objects=None,
    object_layer_complete=True,
    evaluation_paths=None,
):
    return RuleEvaluationContext(
        project_root=Path(root),
        checkpoint="project_validate",
        objects={} if objects is None else objects,
        object_layer_complete=object_layer_complete,
        evaluation_paths=evaluation_paths,
    )


def object_record(object_id, object_type="feature", **fields):
    data = {"id": object_id, "type": object_type, **fields}
    return {
        "data": data,
        "path": Path("knowledge") / f"{object_id}.md",
        "body": "",
    }


def test_checker_registry_contains_exact_v1_catalog():
    assert set(CHECKER_REGISTRY) == {
        "architecture.dependency_boundary",
        "repository.required_path",
        "repository.forbidden_path",
        "knowledge.required_field",
    }
    assert {spec.version for spec in CHECKER_REGISTRY.values()} == {"1"}


def dependency_parameters(**overrides):
    value = {
        "provider": "dart.imports",
        "source_paths": ["lib/domain/**"],
        "forbidden_target_paths": ["lib/presentation/**"],
    }
    value.update(overrides)
    return value


@pytest.mark.parametrize(
    ("parameters", "expected"),
    [
        ({}, "missing required parameter: provider"),
        (dependency_parameters(provider="python.imports"), "unknown Fact Provider"),
        (dependency_parameters(source_paths=[]), "source_paths must be a non-empty sequence"),
        (dependency_parameters(source_paths=["../lib/**"]), "source_paths[0]"),
        (
            dependency_parameters(forbidden_target_paths=[r"lib\presentation\**"]),
            "forbidden_target_paths[0]",
        ),
        (
            dependency_parameters(source_paths=["lib/**", "lib/**"]),
            "source_paths contains duplicates",
        ),
        (dependency_parameters(command="dart analyze"), "unknown parameter: command"),
    ],
)
def test_dependency_boundary_parameter_contract(parameters, expected):
    assert any(
        expected in message
        for message in checker_contract_messages(
            "architecture.dependency_boundary",
            parameters,
        )
    )


def _write(root, relative, content):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_dependency_boundary_forbidden_project_edge_fails(tmp_path):
    _write(
        tmp_path,
        "lib/domain/a.dart",
        "import '../presentation/b.dart';\n",
    )

    outcome = evaluate_checker(
        "architecture.dependency_boundary",
        context(tmp_path),
        dependency_parameters(),
    )

    assert outcome.raw_status == "FAIL"
    assert outcome.details["provider_id"] == "dart.imports"
    assert outcome.details["provider_version"] == "1"
    assert outcome.details["provider_evaluation_mode"] == "project_wide"
    assert outcome.details["violations"] == [
        {
            "source_path": "lib/domain/a.dart",
            "directive": "import",
            "target_path": "lib/presentation/b.dart",
            "target_uri": "../presentation/b.dart",
        }
    ]


@pytest.mark.parametrize("directive", ["import", "export", "part", "part_of"])
def test_dependency_boundary_all_normalized_directives_participate(
    tmp_path,
    monkeypatch,
    directive,
):
    import project_system.fact_providers as provider_module
    from project_system.fact_providers import (
        DependencyFact,
        FactProviderResult,
        dependency_fact_to_dict,
    )
    from project_system.rule_evidence import canonical_sha256

    fact = DependencyFact(
        source_path="lib/domain/a.dart",
        directive=directive,
        target_kind="project",
        target_path="lib/presentation/b.dart",
        target_package=None,
        target_uri="dependency.dart",
    )
    monkeypatch.setattr(
        provider_module,
        "evaluate_fact_provider",
        lambda *args, **kwargs: FactProviderResult(
            provider_id="dart.imports",
            provider_version="1",
            facts=(fact,),
            fact_set_sha256=canonical_sha256([dependency_fact_to_dict(fact)]),
            inspected_source_paths=("lib/domain/a.dart",),
        ),
    )

    outcome = evaluate_checker(
        "architecture.dependency_boundary",
        context(tmp_path),
        dependency_parameters(),
    )
    assert outcome.raw_status == "FAIL"
    assert outcome.details["violations"][0]["directive"] == directive


def test_dependency_boundary_safe_project_external_and_sdk_targets_pass(tmp_path):
    _write(
        tmp_path,
        "lib/domain/a.dart",
        "\n".join(
            (
                "import '../core/b.dart';",
                "import 'package:external/presentation.dart';",
                "import 'dart:async';",
            )
        ),
    )

    outcome = evaluate_checker(
        "architecture.dependency_boundary",
        context(tmp_path),
        dependency_parameters(),
    )

    assert outcome.raw_status == "PASS"
    assert outcome.details["violations"] == []


def test_dependency_boundary_bounded_source_only_inspects_changed_path(tmp_path):
    _write(tmp_path, "lib/domain/safe.dart", "import '../core/a.dart';\n")
    _write(
        tmp_path,
        "lib/domain/legacy.dart",
        "import '../presentation/b.dart';\n",
    )

    outcome = evaluate_checker(
        "architecture.dependency_boundary",
        context(tmp_path, evaluation_paths=("lib/domain/safe.dart",)),
        dependency_parameters(),
    )

    assert outcome.raw_status == "PASS"
    assert outcome.details["provider_evaluation_mode"] == "bounded"
    assert outcome.details["inspected_source_paths"] == ["lib/domain/safe.dart"]


def test_dependency_boundary_unrelated_bounded_path_does_not_scan_project(tmp_path):
    _write(
        tmp_path,
        "lib/domain/legacy.dart",
        "import '../presentation/b.dart';\n",
    )
    _write(tmp_path, "README.md", "changed\n")

    outcome = evaluate_checker(
        "architecture.dependency_boundary",
        context(tmp_path, evaluation_paths=("README.md",)),
        dependency_parameters(),
    )

    assert outcome.raw_status == "PASS"
    assert outcome.details["provider_evaluation_mode"] == "bounded"
    assert outcome.details["inspected_source_paths"] == []


@pytest.mark.parametrize(
    "invalidation_path",
    [
        "pubspec.yaml",
        ".project/policies/rules.yaml",
        ".project/policies/rule_exceptions.yaml",
    ],
)
def test_dependency_boundary_global_invalidation_scans_project(
    tmp_path,
    invalidation_path,
):
    _write(tmp_path, "pubspec.yaml", "name: demo\n")
    _write(
        tmp_path,
        "lib/domain/a.dart",
        "import 'package:demo/presentation/b.dart';\n",
    )

    outcome = evaluate_checker(
        "architecture.dependency_boundary",
        context(tmp_path, evaluation_paths=(invalidation_path,)),
        dependency_parameters(),
    )

    assert outcome.raw_status == "FAIL"
    assert outcome.details["provider_evaluation_mode"] == "project_wide_invalidation"
    assert outcome.details["inspected_source_paths"] == ["lib/domain/a.dart"]


def test_dependency_boundary_fact_provider_error_is_checker_error(
    tmp_path,
    monkeypatch,
):
    import project_system.fact_providers as provider_module

    def fail(*args, **kwargs):
        raise FactProviderError("unsafe provider input")

    monkeypatch.setattr(provider_module, "evaluate_fact_provider", fail)
    outcome = evaluate_checker(
        "architecture.dependency_boundary",
        context(tmp_path),
        dependency_parameters(),
    )

    assert outcome.raw_status == "ERROR"
    assert "trustworthy dependency facts" in outcome.failure_reason


def test_dependency_boundary_malformed_provider_result_is_checker_error(
    tmp_path,
    monkeypatch,
):
    import project_system.fact_providers as provider_module
    from project_system.fact_providers import FactProviderResult

    monkeypatch.setattr(
        provider_module,
        "evaluate_fact_provider",
        lambda *args, **kwargs: FactProviderResult(
            provider_id="dart.imports",
            provider_version="1",
            facts=(),
            fact_set_sha256="0" * 64,
            inspected_source_paths=(),
        ),
    )

    outcome = evaluate_checker(
        "architecture.dependency_boundary",
        context(tmp_path),
        dependency_parameters(),
    )

    assert outcome.raw_status == "ERROR"
    assert outcome.details["violations"] == []


def test_dependency_boundary_violation_order_is_deterministic(tmp_path):
    _write(
        tmp_path,
        "lib/domain/z.dart",
        "export '../presentation/z.dart';\n",
    )
    _write(
        tmp_path,
        "lib/domain/a.dart",
        "import '../presentation/a.dart';\n",
    )

    outcome = evaluate_checker(
        "architecture.dependency_boundary",
        context(tmp_path),
        dependency_parameters(),
    )

    assert [item["source_path"] for item in outcome.details["violations"]] == [
        "lib/domain/a.dart",
        "lib/domain/z.dart",
    ]


def test_required_path_existing_file_passes(tmp_path):
    (tmp_path / "README.md").write_text("project\n", encoding="utf-8")
    outcome = evaluate_checker(
        "repository.required_path",
        context(tmp_path),
        {"path": "README.md"},
    )
    assert outcome.raw_status == "PASS"
    assert outcome.failure_reason is None


def test_required_path_missing_file_fails(tmp_path):
    outcome = evaluate_checker(
        "repository.required_path",
        context(tmp_path),
        {"path": "missing.txt"},
    )
    assert outcome.raw_status == "FAIL"
    assert "missing.txt" in outcome.failure_reason


def test_forbidden_path_absent_passes(tmp_path):
    outcome = evaluate_checker(
        "repository.forbidden_path",
        context(tmp_path),
        {"path": "build"},
    )
    assert outcome.raw_status == "PASS"


def test_forbidden_path_present_fails(tmp_path):
    (tmp_path / "build").mkdir()
    outcome = evaluate_checker(
        "repository.forbidden_path",
        context(tmp_path),
        {"path": "build"},
    )
    assert outcome.raw_status == "FAIL"
    assert "build" in outcome.failure_reason


@pytest.mark.parametrize(
    "checker_id",
    ["repository.required_path", "repository.forbidden_path"],
)
def test_repository_checker_reparse_ambiguity_is_error(
    tmp_path,
    monkeypatch,
    checker_id,
):
    import project_system.rule_checkers as checker_module

    target = tmp_path / "ambiguous"
    target.mkdir()
    original = checker_module._path_has_reparse

    def fake_reparse(path):
        return Path(path) == target or original(path)

    monkeypatch.setattr(checker_module, "_path_has_reparse", fake_reparse)
    outcome = evaluate_checker(
        checker_id,
        context(tmp_path),
        {"path": "ambiguous"},
    )
    assert outcome.raw_status == "ERROR"
    assert "symlink or reparse point" in outcome.failure_reason


@pytest.mark.parametrize(
    "checker_id",
    ["repository.required_path", "repository.forbidden_path"],
)
def test_repository_checker_symlink_escape_is_error(tmp_path, checker_id):
    root = tmp_path / "project"
    root.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    link = root / "linked"
    try:
        link.symlink_to(external, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable on this platform: {exc}")

    outcome = evaluate_checker(
        checker_id,
        context(root),
        {"path": "linked"},
    )
    assert outcome.raw_status == "ERROR"
    assert "symlink or reparse point" in outcome.failure_reason


def test_required_field_all_matching_objects_pass(tmp_path):
    objects = {
        "FEAT-001": object_record("FEAT-001", owner="one"),
        "FEAT-002": object_record("FEAT-002", owner="two"),
    }
    outcome = evaluate_checker(
        "knowledge.required_field",
        context(tmp_path, objects),
        {"object_type": "feature", "field": "owner"},
    )
    assert outcome.raw_status == "PASS"


def test_required_field_missing_field_fails(tmp_path):
    objects = {"FEAT-001": object_record("FEAT-001")}
    outcome = evaluate_checker(
        "knowledge.required_field",
        context(tmp_path, objects),
        {"object_type": "feature", "field": "owner"},
    )
    assert outcome.raw_status == "FAIL"
    assert outcome.details["violating_object_ids"] == ["FEAT-001"]


def test_required_field_explicit_null_fails(tmp_path):
    objects = {"FEAT-001": object_record("FEAT-001", owner=None)}
    outcome = evaluate_checker(
        "knowledge.required_field",
        context(tmp_path, objects),
        {"object_type": "feature", "field": "owner"},
    )
    assert outcome.raw_status == "FAIL"


@pytest.mark.parametrize("value", [False, 0, "", []])
def test_required_field_uses_presence_not_truthiness(tmp_path, value):
    objects = {"FEAT-001": object_record("FEAT-001", required=value)}
    outcome = evaluate_checker(
        "knowledge.required_field",
        context(tmp_path, objects),
        {"object_type": "feature", "field": "required"},
    )
    assert outcome.raw_status == "PASS"


def test_required_field_without_matching_objects_is_not_applicable(tmp_path):
    objects = {
        "REQ-001": object_record(
            "REQ-001",
            object_type="requirement",
            owner="one",
        )
    }
    outcome = evaluate_checker(
        "knowledge.required_field",
        context(tmp_path, objects),
        {"object_type": "feature", "field": "owner"},
    )
    assert outcome.raw_status == "NOT_APPLICABLE"


def test_required_field_incomplete_object_context_is_error(tmp_path):
    outcome = evaluate_checker(
        "knowledge.required_field",
        context(tmp_path, {"FEAT-001": {"path": Path("broken.md")}}),
        {"object_type": "feature", "field": "owner"},
    )
    assert outcome.raw_status == "ERROR"


def test_required_field_explicitly_incomplete_object_layer_is_error(tmp_path):
    objects = {"FEAT-001": object_record("FEAT-001", owner="one")}
    outcome = evaluate_checker(
        "knowledge.required_field",
        context(tmp_path, objects, object_layer_complete=False),
        {"object_type": "feature", "field": "owner"},
    )
    assert outcome.raw_status == "ERROR"
    assert "incomplete" in outcome.failure_reason
