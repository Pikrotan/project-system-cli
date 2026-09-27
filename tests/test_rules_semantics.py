from project_system.init_project import init_project
from project_system.utils import dump_yaml
from project_system.validation import validate

import pytest


RULES_PATH = ".project/policies/rules.yaml"


def rule(*, scope=None, superseded_by=None):
    data = {
        "title": "Architecture rule",
        "status": "active",
        "category": "architecture",
        "description": "Example rule.",
        "verification": {"method": "ai"},
        "enforcement": {
            "severity": "ERROR",
            "checkpoints": ["project_validate"],
        },
        "exception_policy": "forbidden",
    }
    if scope is not None:
        data["scope"] = {"paths": scope}
    if superseded_by is not None:
        data["superseded_by"] = superseded_by
    return data


_MISSING = object()


def deterministic_rule(checker, parameters=_MISSING):
    data = rule()
    verification = {
        "method": "deterministic",
        "checker": checker,
    }
    if parameters is not _MISSING:
        verification["parameters"] = parameters
    data["verification"] = verification
    return data


def write_rules(root, rules):
    (root / RULES_PATH).write_text(
        dump_yaml(
            {
                "schema_version": 1,
                "profile": "project-system-rules-v1",
                "rules": rules,
            }
        ),
        encoding="utf-8",
    )


def blocking_messages(root):
    return [
        message
        for level, location, message in validate(root)
        if level == "BLOCKING" and location == RULES_PATH
    ]


def test_rule_scope_rejects_absolute_path(tmp_path):
    root = init_project("Demo", tmp_path / "demo")
    write_rules(root, {"ARCH-001": rule(scope=["/etc/passwd"])})

    assert any("unsafe scope path" in m for m in blocking_messages(root))


def test_rule_scope_rejects_parent_traversal(tmp_path):
    root = init_project("Demo", tmp_path / "demo")
    write_rules(root, {"ARCH-001": rule(scope=["lib/../secret"])})

    assert any("unsafe scope path" in m for m in blocking_messages(root))


def test_rule_scope_rejects_backslashes(tmp_path):
    root = init_project("Demo", tmp_path / "demo")
    write_rules(root, {"ARCH-001": rule(scope=[r"lib\domain/**"])})

    assert any("unsafe scope path" in m for m in blocking_messages(root))


def test_rule_scope_rejects_git_scope(tmp_path):
    root = init_project("Demo", tmp_path / "demo")
    write_rules(root, {"ARCH-001": rule(scope=[".git/**"])})

    assert any("unsafe scope path" in m for m in blocking_messages(root))


def test_rule_superseded_by_cannot_reference_itself(tmp_path):
    root = init_project("Demo", tmp_path / "demo")
    write_rules(
        root,
        {"ARCH-001": rule(superseded_by="ARCH-001")},
    )

    assert any("superseded_by itself" in m for m in blocking_messages(root))


def test_rule_superseded_by_must_reference_existing_rule(tmp_path):
    root = init_project("Demo", tmp_path / "demo")
    write_rules(
        root,
        {"ARCH-001": rule(superseded_by="ARCH-999")},
    )

    assert any("unknown superseded_by" in m for m in blocking_messages(root))


def test_normalized_repository_relative_scope_is_valid(tmp_path):
    root = init_project("Demo", tmp_path / "demo")
    write_rules(
        root,
        {"ARCH-001": rule(scope=["lib/**", "docs/architecture/*.md"])},
    )

    assert not blocking_messages(root)


@pytest.mark.parametrize(
    ("checker", "parameters", "expected"),
    [
        ("repository.unknown", {"path": "README.md"}, "unknown deterministic checker"),
        ("repository.required_path", _MISSING, "missing required parameter: path"),
        (
            "repository.required_path",
            {"path": "README.md", "unexpected": True},
            "unknown parameter: unexpected",
        ),
        (
            "repository.required_path",
            {"path": "../outside"},
            "unsafe repository path parameter",
        ),
        (
            "repository.required_path",
            {"path": "/absolute"},
            "unsafe repository path parameter",
        ),
        (
            "repository.required_path",
            {"path": r"docs\file.md"},
            "unsafe repository path parameter",
        ),
        (
            "repository.required_path",
            {"path": "C:/project/file.md"},
            "unsafe repository path parameter",
        ),
        (
            "repository.required_path",
            {"path": ".git/config"},
            "unsafe repository path parameter",
        ),
        (
            "repository.required_path",
            {"path": "docs/*.md"},
            "unsafe repository path parameter",
        ),
        (
            "repository.required_path",
            {"path": "bad\x00path"},
            "unsafe repository path parameter",
        ),
        ("repository.forbidden_path", {}, "missing required parameter: path"),
        (
            "repository.forbidden_path",
            {"path": "build", "glob": "**"},
            "unknown parameter: glob",
        ),
        (
            "knowledge.required_field",
            {"field": "owner"},
            "missing required parameter: object_type",
        ),
        (
            "knowledge.required_field",
            {"object_type": "feature"},
            "missing required parameter: field",
        ),
        (
            "knowledge.required_field",
            {"object_type": "feature", "field": "owner", "nested": True},
            "unknown parameter: nested",
        ),
        (
            "knowledge.required_field",
            {"object_type": "feature", "field": "metadata.owner"},
            "top-level field name",
        ),
        (
            "knowledge.required_field",
            {"object_type": "unknown", "field": "owner"},
            "canonical knowledge object type",
        ),
        (
            "knowledge.required_field",
            {"object_type": "feature", "field": ""},
            "top-level field name",
        ),
    ],
)
def test_checker_specific_contracts_fail_closed(
    tmp_path,
    checker,
    parameters,
    expected,
):
    root = init_project("Demo", tmp_path / "demo")
    selected = deterministic_rule(checker, parameters)
    write_rules(root, {"REPO-001": selected})

    assert any(expected in message for message in blocking_messages(root))


def test_valid_checker_parameters_are_accepted(tmp_path):
    root = init_project("Demo", tmp_path / "demo")
    write_rules(
        root,
        {
            "REPO-001": deterministic_rule(
                "repository.required_path",
                {"path": "README.md"},
            ),
            "REPO-002": deterministic_rule(
                "repository.forbidden_path",
                {"path": "build"},
            ),
            "REQ-001": deterministic_rule(
                "knowledge.required_field",
                {"object_type": "feature", "field": "owner"},
            ),
        },
    )

    assert not blocking_messages(root)
