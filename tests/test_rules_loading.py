from project_system.init_project import init_project
from project_system.validation import validate


def blocking_for(issues, location):
    return [
        message
        for level, issue_location, message in issues
        if level == "BLOCKING" and issue_location == location
    ]


def test_rules_registry_rejects_duplicate_yaml_keys(tmp_path):
    root = init_project("Demo", tmp_path / "demo")
    path = root / ".project/policies/rules.yaml"
    path.write_text(
        "schema_version: 1\n"
        "schema_version: 1\n"
        "profile: project-system-rules-v1\n"
        "rules: {}\n",
        encoding="utf-8",
    )

    messages = blocking_for(validate(root), ".project/policies/rules.yaml")

    assert any("duplicate YAML key" in message for message in messages)


def test_rules_registry_rejects_malformed_yaml(tmp_path):
    root = init_project("Demo", tmp_path / "demo")
    path = root / ".project/policies/rules.yaml"
    path.write_text(
        "schema_version: 1\n"
        "profile: [\n",
        encoding="utf-8",
    )

    messages = blocking_for(validate(root), ".project/policies/rules.yaml")

    assert any("cannot parse" in message for message in messages)


def test_rules_registry_is_schema_validated(tmp_path):
    root = init_project("Demo", tmp_path / "demo")
    path = root / ".project/policies/rules.yaml"
    path.write_text(
        "schema_version: 1\n"
        "profile: project-system-rules-v1\n"
        "rules: {}\n"
        "unexpected: true\n",
        encoding="utf-8",
    )

    messages = blocking_for(validate(root), ".project/policies/rules.yaml")

    assert any("unexpected" in message for message in messages)


def test_exception_registry_validates_datetime_format(tmp_path):
    root = init_project("Demo", tmp_path / "demo")
    path = root / ".project/policies/rule_exceptions.yaml"
    path.write_text(
        "schema_version: 1\n"
        "profile: project-system-rule-exceptions-v1\n"
        "exceptions:\n"
        "  EXC-20260926-deadbeef:\n"
        "    rule_id: ARCH-001\n"
        "    state: active\n"
        "    mode: temporary\n"
        "    reason: Temporary migration exception.\n"
        "    scope:\n"
        "      paths:\n"
        "        - lib/**\n"
        "    decision_id: DEC-20260926-deadbeef\n"
        "    approved_by: owner\n"
        "    approved_at: not-a-date\n"
        "    expires_at: 2026-10-01T00:00:00Z\n",
        encoding="utf-8",
    )

    messages = blocking_for(
        validate(root),
        ".project/policies/rule_exceptions.yaml",
    )

    assert any("not-a-date" in message for message in messages), messages


def test_rules_registry_cannot_be_external_symlink(tmp_path):
    import pytest

    root = init_project("Demo", tmp_path / "demo")
    registry = root / ".project/policies/rules.yaml"
    external = tmp_path / "external-rules.yaml"

    external.write_text(
        "schema_version: 1\n"
        "profile: project-system-rules-v1\n"
        "rules: {}\n",
        encoding="utf-8",
    )
    registry.unlink()

    try:
        registry.symlink_to(external)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable on this platform: {exc}")

    messages = blocking_for(validate(root), ".project/policies/rules.yaml")

    assert any(
        "symlink" in message or "outside" in message
        for message in messages
    )


def test_rules_registry_rejects_detected_reparse_path(tmp_path, monkeypatch):
    import project_system.rules as rules_module

    root = init_project("Demo", tmp_path / "demo")
    registry = root / ".project/policies/rules.yaml"

    original = rules_module._path_has_reparse

    def fake_reparse(path):
        if path == registry:
            return True
        return original(path)

    monkeypatch.setattr(rules_module, "_path_has_reparse", fake_reparse)

    messages = blocking_for(validate(root), ".project/policies/rules.yaml")

    assert any(
        "symlink or reparse point" in message
        for message in messages
    )
