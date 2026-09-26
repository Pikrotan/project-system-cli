from project_system.init_project import init_project
from project_system.objects import create_object
from project_system.utils import dump_yaml
from project_system.validation import validate


RULES_PATH = ".project/policies/rules.yaml"
EXCEPTIONS_PATH = ".project/policies/rule_exceptions.yaml"


def rule(*, object_ids=None):
    data = {
        "title": "Reference rule",
        "status": "active",
        "category": "process",
        "description": "Reference validation test.",
        "verification": {"method": "ai"},
        "enforcement": {
            "severity": "ERROR",
            "checkpoints": ["project_validate"],
        },
        "exception_policy": "decision_required",
    }
    if object_ids is not None:
        data["traceability"] = {"object_ids": object_ids}
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


def write_exception(root, *, rule_id, decision_id, scope=None):
    (root / EXCEPTIONS_PATH).write_text(
        dump_yaml(
            {
                "schema_version": 1,
                "profile": "project-system-rule-exceptions-v1",
                "exceptions": {
                    "EXC-20260927-deadbeef": {
                        "rule_id": rule_id,
                        "state": "active",
                        "mode": "temporary",
                        "reason": "Temporary governed exception.",
                        "scope": {"paths": scope or ["lib/**"]},
                        "decision_id": decision_id,
                        "approved_by": "owner",
                        "approved_at": "2026-09-27T00:00:00Z",
                        "expires_at": "2099-01-01T00:00:00Z",
                    }
                },
            }
        ),
        encoding="utf-8",
    )


def blocking(root, location):
    return [
        message
        for level, issue_location, message in validate(root)
        if level == "BLOCKING" and issue_location == location
    ]


def test_traceability_object_id_must_exist(tmp_path):
    root = init_project("Demo", tmp_path / "demo")
    write_rules(
        root,
        {"PROC-001": rule(object_ids=["FEAT-20260927-deadbeef"])},
    )

    assert any(
        "unknown traceability object_id" in message
        for message in blocking(root, RULES_PATH)
    )


def test_existing_traceability_object_id_is_valid(tmp_path):
    root = init_project("Demo", tmp_path / "demo")
    _, object_id = create_object(
        root, "feature", "Feature", "product", "owner"
    )
    write_rules(root, {"PROC-001": rule(object_ids=[object_id])})

    assert not blocking(root, RULES_PATH)


def test_exception_rule_id_must_exist(tmp_path):
    root = init_project("Demo", tmp_path / "demo")
    _, decision_id = create_object(
        root, "decision", "Decision", "product", "owner"
    )
    write_rules(root, {"PROC-001": rule()})
    write_exception(
        root,
        rule_id="PROC-999",
        decision_id=decision_id,
    )

    assert any(
        "unknown rule_id" in message
        for message in blocking(root, EXCEPTIONS_PATH)
    )


def test_exception_decision_id_must_exist_as_decision(tmp_path):
    root = init_project("Demo", tmp_path / "demo")
    write_rules(root, {"PROC-001": rule()})
    write_exception(
        root,
        rule_id="PROC-001",
        decision_id="DEC-20260927-deadbeef",
    )

    assert any(
        "unknown decision_id" in message
        for message in blocking(root, EXCEPTIONS_PATH)
    )


def test_exception_scope_must_be_safe(tmp_path):
    root = init_project("Demo", tmp_path / "demo")
    _, decision_id = create_object(
        root, "decision", "Decision", "product", "owner"
    )
    write_rules(root, {"PROC-001": rule()})
    write_exception(
        root,
        rule_id="PROC-001",
        decision_id=decision_id,
        scope=["../secret"],
    )

    assert any(
        "unsafe scope path" in message
        for message in blocking(root, EXCEPTIONS_PATH)
    )


def test_valid_exception_references_are_accepted(tmp_path):
    root = init_project("Demo", tmp_path / "demo")
    _, decision_id = create_object(
        root, "decision", "Decision", "product", "owner"
    )
    write_rules(root, {"PROC-001": rule()})
    write_exception(
        root,
        rule_id="PROC-001",
        decision_id=decision_id,
    )

    assert not blocking(root, RULES_PATH)
    assert not blocking(root, EXCEPTIONS_PATH)
