from pathlib import Path

import pytest

from project_system.rule_checkers import CHECKER_REGISTRY, evaluate_checker
from project_system.rule_engine import RuleEvaluationContext


def context(root, objects=None, object_layer_complete=True):
    return RuleEvaluationContext(
        project_root=Path(root),
        checkpoint="project_validate",
        objects={} if objects is None else objects,
        object_layer_complete=object_layer_complete,
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
        "repository.required_path",
        "repository.forbidden_path",
        "knowledge.required_field",
    }
    assert {spec.version for spec in CHECKER_REGISTRY.values()} == {"1"}


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
