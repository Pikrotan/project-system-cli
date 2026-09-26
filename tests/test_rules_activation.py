from project_system.init_project import init_project
from project_system.utils import load_yaml, dump_yaml
from project_system.validation import validate


def fatal(issues):
    return [x for x in issues if x[0] in {"BLOCKING", "ERROR"}]


def test_rules_marker_requires_rules_registry(tmp_path):
    root = init_project("Demo", tmp_path / "demo")
    (root / ".project/policies/rules.yaml").unlink()

    issues = validate(root)

    assert any(
        level == "BLOCKING"
        and location == ".project/policies/rules.yaml"
        and "missing" in message
        for level, location, message in issues
    )


def test_rules_marker_requires_exception_registry(tmp_path):
    root = init_project("Demo", tmp_path / "demo")
    (root / ".project/policies/rule_exceptions.yaml").unlink()

    issues = validate(root)

    assert any(
        level == "BLOCKING"
        and location == ".project/policies/rule_exceptions.yaml"
        and "missing" in message
        for level, location, message in issues
    )


def test_rules_files_without_activation_marker_are_error(tmp_path):
    root = init_project("Demo", tmp_path / "demo")
    cfg = load_yaml(root / "project.yaml")
    cfg["tooling"].pop("rules_schema_version")
    (root / "project.yaml").write_text(
        dump_yaml(cfg),
        encoding="utf-8",
    )

    issues = validate(root)

    assert any(
        level == "ERROR"
        and "without tooling.rules_schema_version activation" in message
        for level, _, message in issues
    )


def test_project_without_rules_marker_or_files_remains_valid(tmp_path):
    root = init_project("Demo", tmp_path / "demo")
    cfg = load_yaml(root / "project.yaml")
    cfg["tooling"].pop("rules_schema_version")
    (root / "project.yaml").write_text(
        dump_yaml(cfg),
        encoding="utf-8",
    )
    (root / ".project/policies/rules.yaml").unlink()
    (root / ".project/policies/rule_exceptions.yaml").unlink()

    assert not fatal(validate(root))
