from pathlib import Path
import shutil

import pytest

import project_system
import project_system.validation as validation_module
from project_system.cli import main
from project_system.init_project import init_project
from project_system.objects import create_object
from project_system.process_runner import run_process
from project_system.rule_evidence import RuleEvidenceError, canonical_sha256
from project_system.utils import distribution_root, dump_yaml, load_yaml
from project_system.validation import validate, validate_report


RULES_PATH = Path(".project/policies/rules.yaml")
EXCEPTIONS_PATH = Path(".project/policies/rule_exceptions.yaml")


def rule(
    *,
    method="deterministic",
    checker="repository.required_path",
    parameters=None,
    severity="ERROR",
    checkpoints=None,
    traceability=None,
):
    verification = {"method": method}
    if method == "deterministic":
        verification["checker"] = checker
        verification["parameters"] = (
            {"path": "README.md"} if parameters is None else parameters
        )
    value = {
        "title": "Validation integration rule",
        "status": "active",
        "category": "repository",
        "description": "Exercise the canonical project validation gate.",
        "verification": verification,
        "enforcement": {
            "severity": severity,
            "checkpoints": checkpoints or ["project_validate"],
        },
        "exception_policy": "forbidden",
    }
    if traceability is not None:
        value["traceability"] = traceability
    return value


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


def git_commit(root):
    commands = [
        ["git", "init", "-q"],
        ["git", "config", "user.name", "Project System Tests"],
        [
            "git",
            "config",
            "user.email",
            "project-system-tests@example.invalid",
        ],
        ["git", "add", "--all"],
        ["git", "commit", "-q", "-m", "initial"],
    ]
    for command in commands:
        run_process(command, cwd=root, check=True, capture_output=True, text=True)
    return run_process(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def rule_issues(report, rule_id):
    return [issue for issue in report.issues if issue[1] == rule_id]


def test_empty_rules_preserve_validate_compatibility_without_git(tmp_path, monkeypatch):
    root = init_project("Demo", tmp_path / "demo")

    def unexpected_git(*args, **kwargs):
        raise AssertionError("empty Rules registry must not require Git")

    monkeypatch.setattr(validation_module, "_git_head", unexpected_git)
    report = validate_report(root)
    issues = validate(root)

    assert isinstance(issues, list)
    assert all(isinstance(issue, tuple) and len(issue) == 3 for issue in issues)
    assert issues == list(report.issues)
    assert report.rule_evidence is None
    assert not [issue for issue in issues if issue[0] in {"BLOCKING", "ERROR"}]


@pytest.mark.parametrize("status", ["draft", "deprecated"])
def test_no_active_rules_do_not_require_git(tmp_path, monkeypatch, status):
    root = init_project("Demo", tmp_path / "demo")
    selected = rule()
    selected["status"] = status
    write_rules(root, {"REPO-001": selected})

    monkeypatch.setattr(
        validation_module,
        "_git_head",
        lambda root: (_ for _ in ()).throw(AssertionError("Git was called")),
    )

    assert validate_report(root).rule_evidence is None


def test_deterministic_pass_produces_evidence_without_issue(tmp_path):
    root = init_project("Demo", tmp_path / "demo")
    write_rules(root, {"REPO-001": rule()})
    git_commit(root)

    report = validate_report(root)

    assert report.rule_evidence.results[0].raw_status == "PASS"
    assert report.rule_evidence.results[0].effective_status == "PASS"
    assert not rule_issues(report, "REPO-001")


@pytest.mark.parametrize("severity", ["BLOCKING", "ERROR", "WARNING", "INFO"])
def test_deterministic_fail_uses_configured_severity(tmp_path, severity):
    root = init_project("Demo", tmp_path / f"demo-{severity.lower()}")
    write_rules(
        root,
        {
            "REPO-001": rule(
                parameters={"path": "missing-required-file"},
                severity=severity,
            )
        },
    )
    git_commit(root)

    report = validate_report(root)

    evidence = report.rule_evidence.results[0]
    assert evidence.raw_status == evidence.effective_status == "FAIL"
    assert rule_issues(report, "REPO-001")[0][0] == severity
    blocks = severity in {"BLOCKING", "ERROR"}
    assert (rule_issues(report, "REPO-001")[0][0] in {"BLOCKING", "ERROR"}) is blocks


@pytest.mark.parametrize("severity", ["INFO", "WARNING"])
def test_runtime_error_is_always_validation_error(tmp_path, severity):
    root = init_project("Demo", tmp_path / f"demo-{severity.lower()}")
    write_rules(
        root,
        {
            "REQ-001": rule(
                checker="knowledge.required_field",
                parameters={"object_type": "feature", "field": "owner"},
                severity=severity,
            )
        },
    )
    malformed = root / "knowledge/features/FEAT-20260927-deadbeef-broken.md"
    malformed.write_text("not frontmatter\n", encoding="utf-8")
    git_commit(root)

    report = validate_report(root)

    assert report.rule_evidence.results[0].raw_status == "ERROR"
    assert rule_issues(report, "REQ-001")[0][0] == "ERROR"
    assert "ERROR" in rule_issues(report, "REQ-001")[0][2]


@pytest.mark.parametrize("method", ["ai", "human"])
@pytest.mark.parametrize("severity", ["BLOCKING", "ERROR", "WARNING", "INFO"])
def test_pending_rule_uses_configured_severity(tmp_path, method, severity):
    root = init_project("Demo", tmp_path / f"demo-{method}-{severity.lower()}")
    write_rules(
        root,
        {"PROC-001": rule(method=method, severity=severity)},
    )
    git_commit(root)

    report = validate_report(root)

    assert report.rule_evidence.results[0].raw_status == "PENDING"
    issue = rule_issues(report, "PROC-001")[0]
    assert issue[0] == severity
    assert "PENDING" in issue[2]
    assert "verification_required" in issue[2]


def test_not_applicable_remains_in_evidence_without_issue(tmp_path):
    root = init_project("Demo", tmp_path / "demo")
    write_rules(
        root,
        {
            "REPO-001": rule(checkpoints=["sync_verify"]),
        },
    )
    git_commit(root)

    report = validate_report(root)

    assert report.rule_evidence.results[0].raw_status == "NOT_APPLICABLE"
    assert not rule_issues(report, "REPO-001")


def test_invalid_rule_definition_prevents_runtime_evaluation(tmp_path, monkeypatch):
    root = init_project("Demo", tmp_path / "demo")
    write_rules(
        root,
        {"REPO-001": rule(checker="repository.not_registered")},
    )

    monkeypatch.setattr(
        validation_module,
        "evaluate_rules",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("invalid definition reached Rule Engine")
        ),
    )
    report = validate_report(root)

    assert report.rule_evidence is None
    assert any(
        level == "BLOCKING" and "unknown deterministic checker" in message
        for level, _, message in report.issues
    )


def test_reference_blocker_prevents_runtime_evaluation(tmp_path, monkeypatch):
    root = init_project("Demo", tmp_path / "demo")
    write_rules(
        root,
        {
            "PROC-001": rule(
                method="ai",
                traceability={"object_ids": ["FEAT-20260927-deadbeef"]},
            )
        },
    )

    monkeypatch.setattr(
        validation_module,
        "evaluate_rules",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("blocked reference reached Rule Engine")
        ),
    )
    report = validate_report(root)

    assert report.rule_evidence is None
    assert any(
        level == "BLOCKING" and "unknown traceability object_id" in message
        for level, _, message in report.issues
    )


def test_invalid_project_identity_prevents_runtime_evaluation(tmp_path, monkeypatch):
    root = init_project("Demo", tmp_path / "demo")
    write_rules(root, {"REPO-001": rule()})
    config = load_yaml(root / "project.yaml")
    config["project"]["id"] = "Invalid Project Identity"
    (root / "project.yaml").write_text(dump_yaml(config), encoding="utf-8")

    monkeypatch.setattr(
        validation_module,
        "evaluate_rules",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("invalid project identity reached Rule Engine")
        ),
    )
    report = validate_report(root)

    assert report.rule_evidence is None
    assert any(
        level == "BLOCKING" and location == "project.yaml"
        for level, location, _ in report.issues
    )


def test_incomplete_object_layer_is_passed_fail_closed(tmp_path, monkeypatch):
    root = init_project("Demo", tmp_path / "demo")
    write_rules(
        root,
        {
            "REQ-001": rule(
                checker="knowledge.required_field",
                parameters={"object_type": "feature", "field": "owner"},
                severity="INFO",
            )
        },
    )
    (root / "knowledge/features/legacy.yaml").write_text(
        "id: FEAT-20260927-deadbeef\n",
        encoding="utf-8",
    )
    git_commit(root)
    captured = {}
    actual_evaluate = validation_module.evaluate_rules

    def capture_context(registry, evaluation_context):
        captured["context"] = evaluation_context
        return actual_evaluate(registry, evaluation_context)

    monkeypatch.setattr(validation_module, "evaluate_rules", capture_context)
    report = validate_report(root)

    assert captured["context"].object_layer_complete is False
    assert report.rule_evidence.results[0].raw_status == "ERROR"
    assert rule_issues(report, "REQ-001")[0][0] == "ERROR"


def test_duplicate_object_identity_makes_context_incomplete(tmp_path, monkeypatch):
    root = init_project("Demo", tmp_path / "demo")
    path, _ = create_object(root, "feature", "First", "product", "owner")
    duplicate = path.with_name(path.stem + "-duplicate.md")
    duplicate.write_bytes(path.read_bytes())
    write_rules(
        root,
        {
            "REQ-001": rule(
                checker="knowledge.required_field",
                parameters={"object_type": "feature", "field": "owner"},
            )
        },
    )
    git_commit(root)
    captured = {}
    actual_evaluate = validation_module.evaluate_rules

    def capture_context(registry, evaluation_context):
        captured["context"] = evaluation_context
        return actual_evaluate(registry, evaluation_context)

    monkeypatch.setattr(validation_module, "evaluate_rules", capture_context)
    report = validate_report(root)

    assert captured["context"].object_layer_complete is False
    assert report.rule_evidence.results[0].raw_status == "ERROR"
    assert any("duplicate ID" in message for _, _, message in report.issues)


def test_evidence_binds_git_project_cli_and_loaded_registries(tmp_path):
    root = init_project("Bound Project", tmp_path / "bound")
    write_rules(root, {"REPO-001": rule()})
    head = git_commit(root)

    report = validate_report(root)
    evidence = report.rule_evidence

    assert evidence.git_head == head
    assert evidence.project_id == load_yaml(root / "project.yaml")["project"]["id"]
    assert evidence.cli_version == project_system.__version__
    assert evidence.base_commit is None
    assert evidence.rules_registry_sha256 == canonical_sha256(
        load_yaml(root / RULES_PATH)
    )
    assert evidence.exception_registry_sha256 == canonical_sha256(
        load_yaml(root / EXCEPTIONS_PATH)
    )


def test_active_rules_without_git_head_fail_closed(tmp_path):
    root = init_project("Demo", tmp_path / "demo")
    write_rules(root, {"REPO-001": rule()})
    run_process(
        ["git", "init", "-q"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )

    report = validate_report(root)

    assert report.rule_evidence is None
    assert any(
        severity == "ERROR" and location == "rules_evidence"
        for severity, location, _ in report.issues
    )


def test_evidence_integrity_failure_is_validation_error(tmp_path, monkeypatch):
    root = init_project("Demo", tmp_path / "demo")
    write_rules(root, {"REPO-001": rule()})
    git_commit(root)

    monkeypatch.setattr(
        validation_module,
        "build_rule_evidence",
        lambda **kwargs: (_ for _ in ()).throw(RuleEvidenceError("tampered")),
    )
    report = validate_report(root)

    assert report.rule_evidence is None
    assert any(
        severity == "ERROR"
        and location == "rules_evidence"
        and "tampered" in message
        for severity, location, message in report.issues
    )


@pytest.mark.parametrize(
    ("severity", "expected_exit"),
    [("BLOCKING", 2), ("WARNING", 0)],
)
def test_existing_cli_gate_enforces_rule_issue_severity(
    tmp_path,
    monkeypatch,
    severity,
    expected_exit,
):
    root = init_project("Demo", tmp_path / f"demo-{severity.lower()}")
    write_rules(
        root,
        {
            "REPO-001": rule(
                parameters={"path": "missing-required-file"},
                severity=severity,
            )
        },
    )
    git_commit(root)
    monkeypatch.setattr("project_system.cli.find_root", lambda: root)

    with pytest.raises(SystemExit) as exit_info:
        main(["validate"])

    assert exit_info.value.code == expected_exit


def test_validation_does_not_persist_rule_evidence(tmp_path):
    root = init_project("Demo", tmp_path / "demo")
    write_rules(root, {"REPO-001": rule()})
    git_commit(root)

    report = validate_report(root)

    assert report.rule_evidence is not None
    assert not (root / ".generated/evidence").exists()
    assert not list((root / ".generated").rglob("*evidence*.json"))


def test_packaged_ci_workflow_delegates_only_to_project_validate():
    workflow = (
        distribution_root()
        / "github_templates/workflows/project-validate.yml"
    ).read_text(encoding="utf-8")

    assert "project validate" in workflow
    assert "project rules check" not in workflow
    assert "repository.required_path" not in workflow


def test_evidence_context_fingerprint_is_checkout_location_independent(
    tmp_path,
    monkeypatch,
):
    root_a = init_project("Portable Project", tmp_path / "checkout-a")
    create_object(root_a, "feature", "Search", "product", "owner")
    write_rules(
        root_a,
        {
            "REQ-001": rule(
                checker="knowledge.required_field",
                parameters={"object_type": "feature", "field": "owner"},
            )
        },
    )

    root_b = tmp_path / "checkout-b"
    shutil.copytree(root_a, root_b)

    fixed_head = "a" * 40
    monkeypatch.setattr(validation_module, "_git_head", lambda root: fixed_head)

    report_a = validate_report(root_a)
    report_b = validate_report(root_b)

    assert report_a.rule_evidence is not None
    assert report_b.rule_evidence is not None
    assert (
        report_a.rule_evidence.evaluation_context_sha256
        == report_b.rule_evidence.evaluation_context_sha256
    )
    assert (
        report_a.rule_evidence.evidence_fingerprint
        == report_b.rule_evidence.evidence_fingerprint
    )