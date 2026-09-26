from project_system.init_project import init_project
from project_system.utils import dump_yaml
from project_system.validation import validate


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
