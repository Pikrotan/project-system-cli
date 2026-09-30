from pathlib import Path
import shutil

import pytest

import project_system
import project_system.validation as validation_module
from project_system.cli import main
from project_system.frontmatter import read_object, write_object
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
    exception_policy="forbidden",
    scope=None,
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
        "exception_policy": exception_policy,
    }
    if traceability is not None:
        value["traceability"] = traceability
    if scope is not None:
        value["scope"] = {"paths": list(scope)}
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


def write_exceptions(root, exceptions):
    (root / EXCEPTIONS_PATH).write_text(
        dump_yaml(
            {
                "schema_version": 1,
                "profile": "project-system-rule-exceptions-v1",
                "exceptions": exceptions,
            }
        ),
        encoding="utf-8",
    )


def governed_exception(
    rule_id,
    decision_id,
    *,
    paths,
    state="active",
    mode="permanent",
    expires_at=None,
):
    value = {
        "rule_id": rule_id,
        "state": state,
        "mode": mode,
        "reason": "Approved validation exception.",
        "scope": {"paths": list(paths)},
        "decision_id": decision_id,
        "approved_by": "project-owner",
        "approved_at": "2026-09-27T10:00:00Z",
    }
    if expires_at is not None:
        value["expires_at"] = expires_at
    if state == "revoked":
        value.update(
            {
                "revoked_by": "project-owner",
                "revoked_at": "2026-09-27T11:00:00Z",
            }
        )
    return value


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


def architecture_rule(*, severity="BLOCKING", checkpoints=None, scope=None):
    selected = rule(
        checker="architecture.dependency_boundary",
        parameters={
            "provider": "dart.imports",
            "source_paths": ["lib/domain/**"],
            "forbidden_target_paths": ["lib/presentation/**"],
        },
        severity=severity,
        checkpoints=checkpoints,
        scope=scope,
    )
    selected["category"] = "architecture"
    return selected


def write_dart(root, relative, content):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_project_validate_safe_architecture_has_no_issue(tmp_path):
    root = init_project("Demo", tmp_path / "demo")
    write_rules(root, {"ARCH-001": architecture_rule()})
    write_dart(root, "lib/domain/a.dart", "import '../core/a.dart';\n")
    git_commit(root)

    report = validate_report(root)

    assert report.rule_evidence.results[0].raw_status == "PASS"
    assert not rule_issues(report, "ARCH-001")


def test_project_validate_forbidden_architecture_uses_configured_severity(tmp_path):
    root = init_project("Demo", tmp_path / "demo")
    write_rules(root, {"ARCH-001": architecture_rule(severity="BLOCKING")})
    write_dart(
        root,
        "lib/domain/a.dart",
        "import '../presentation/a.dart';\n",
    )
    git_commit(root)

    report = validate_report(root)

    assert report.rule_evidence.results[0].raw_status == "FAIL"
    assert rule_issues(report, "ARCH-001")[0][0] == "BLOCKING"


def test_project_validate_provider_error_is_error_regardless_of_rule_severity(tmp_path):
    root = init_project("Demo", tmp_path / "demo")
    write_rules(root, {"ARCH-001": architecture_rule(severity="INFO")})
    source = root / "lib/domain/a.dart"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"\xff")
    git_commit(root)

    report = validate_report(root)

    assert report.rule_evidence.results[0].raw_status == "ERROR"
    assert rule_issues(report, "ARCH-001")[0][0] == "ERROR"


def test_project_validate_rejects_architecture_rule_scope_before_evaluation(tmp_path):
    root = init_project("Demo", tmp_path / "demo")
    write_rules(
        root,
        {"ARCH-001": architecture_rule(scope=("lib/domain/**",))},
    )

    report = validate_report(root)

    assert report.rule_evidence is None
    assert any(
        level == "BLOCKING"
        and location == RULES_PATH.as_posix()
        and "must use verification.parameters.source_paths" in message
        for level, location, message in report.issues
    )


def test_sync_bounded_changed_dart_source_detects_forbidden_edge(tmp_path):
    root = init_project("Demo", tmp_path / "demo")
    write_rules(
        root,
        {"ARCH-001": architecture_rule(checkpoints=["sync_verify"])},
    )
    source = write_dart(
        root,
        "lib/domain/a.dart",
        "import '../core/a.dart';\n",
    )
    head = git_commit(root)
    source.write_text("import '../presentation/a.dart';\n", encoding="utf-8")

    report = validate_report(
        root,
        rule_checkpoint="sync_verify",
        rule_base_commit=head,
        rule_evaluation_paths=("lib/domain/a.dart",),
    )

    result = report.rule_evidence.results[0]
    assert result.raw_status == "FAIL"
    assert result.details["provider_evaluation_mode"] == "bounded"
    assert result.details["inspected_source_paths"] == ["lib/domain/a.dart"]


def test_sync_bounded_unrelated_path_does_not_scan_existing_violation(tmp_path):
    root = init_project("Demo", tmp_path / "demo")
    write_rules(
        root,
        {"ARCH-001": architecture_rule(checkpoints=["sync_verify"])},
    )
    write_dart(
        root,
        "lib/domain/legacy.dart",
        "import '../presentation/legacy.dart';\n",
    )
    head = git_commit(root)

    report = validate_report(
        root,
        rule_checkpoint="sync_verify",
        rule_base_commit=head,
        rule_evaluation_paths=("README.md",),
    )

    result = report.rule_evidence.results[0]
    assert result.raw_status == "PASS"
    assert result.details["provider_evaluation_mode"] == "bounded"
    assert result.details["inspected_source_paths"] == []


def test_sync_pubspec_change_reclassifies_and_detects_existing_edge(tmp_path):
    root = init_project("Demo", tmp_path / "demo")
    write_rules(
        root,
        {"ARCH-001": architecture_rule(checkpoints=["sync_verify"])},
    )
    write_dart(
        root,
        "lib/domain/a.dart",
        "import 'package:new_name/presentation/a.dart';\n",
    )
    pubspec = root / "pubspec.yaml"
    pubspec.write_text("name: old_name\n", encoding="utf-8")
    head = git_commit(root)

    before = validate_report(
        root,
        rule_checkpoint="sync_verify",
        rule_base_commit=head,
        rule_evaluation_paths=("lib/domain/a.dart",),
    )
    assert before.rule_evidence.results[0].raw_status == "PASS"

    pubspec.write_text("name: new_name\n", encoding="utf-8")

    report = validate_report(
        root,
        rule_checkpoint="sync_verify",
        rule_base_commit=head,
        rule_evaluation_paths=("pubspec.yaml",),
    )

    result = report.rule_evidence.results[0]
    assert result.raw_status == "FAIL"
    assert result.details["provider_evaluation_mode"] == "project_wide_invalidation"
    assert result.details["violations"][0]["target_path"] == "lib/presentation/a.dart"


def test_sync_rules_registry_change_forces_complete_architecture_evaluation(tmp_path):
    root = init_project("Demo", tmp_path / "demo")
    write_dart(
        root,
        "lib/domain/a.dart",
        "import '../presentation/a.dart';\n",
    )
    head = git_commit(root)
    write_rules(
        root,
        {"ARCH-001": architecture_rule(checkpoints=["sync_verify"])},
    )

    report = validate_report(
        root,
        rule_checkpoint="sync_verify",
        rule_base_commit=head,
        rule_evaluation_paths=(RULES_PATH.as_posix(),),
    )

    result = report.rule_evidence.results[0]
    assert result.raw_status == "FAIL"
    assert result.details["provider_evaluation_mode"] == "project_wide_invalidation"


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


def prepare_waivable_required_path(root, *, paths=("missing-required-file",), **exception_overrides):
    _, decision_id = create_object(
        root,
        "decision",
        "Approve validation exception",
        "governance",
        "project-owner",
    )
    write_rules(
        root,
        {
            "REPO-001": rule(
                parameters={"path": "missing-required-file"},
                severity="BLOCKING",
                exception_policy="decision_required",
            )
        },
    )
    selected = governed_exception(
        "REPO-001",
        decision_id,
        paths=paths,
        **exception_overrides,
    )
    write_exceptions(root, {"EXC-20260927-abcdef12": selected})
    return decision_id


def test_project_validate_waives_only_effective_status_for_approved_failure(
    tmp_path,
    monkeypatch,
):
    root = init_project("Demo", tmp_path / "demo")
    prepare_waivable_required_path(root)
    git_commit(root)
    monkeypatch.setattr(
        validation_module,
        "_utc_now",
        lambda: validation_module.datetime(
            2026, 9, 27, 12, 0, tzinfo=validation_module.timezone.utc
        ),
    )

    report = validate_report(root)

    item = report.rule_evidence.results[0]
    assert item.raw_status == "FAIL"
    assert item.effective_status == "WAIVED"
    assert item.exception_id == "EXC-20260927-abcdef12"
    assert report.rule_evidence.applied_exception_ids == (
        "EXC-20260927-abcdef12",
    )
    issue = rule_issues(report, "REPO-001")[0]
    assert issue[0] == "INFO"
    assert "WAIVED" in issue[2]
    assert "EXC-20260927-abcdef12" in issue[2]
    assert not [
        issue
        for issue in rule_issues(report, "REPO-001")
        if issue[0] in {"BLOCKING", "ERROR"}
    ]
    monkeypatch.setattr("project_system.cli.find_root", lambda: root)
    with pytest.raises(SystemExit) as exit_info:
        main(["validate"])
    assert exit_info.value.code == 0


@pytest.mark.parametrize(
    "exception_overrides",
    [
        {"paths": ("somewhere-else/**",)},
        {"paths": ("missing-required-file",), "state": "revoked"},
        {
            "paths": ("missing-required-file",),
            "mode": "temporary",
            "expires_at": "2026-09-27T12:00:00Z",
        },
    ],
)
def test_project_validate_keeps_fail_for_inapplicable_exception(
    tmp_path,
    monkeypatch,
    exception_overrides,
):
    root = init_project("Demo", tmp_path / "demo")
    exception_overrides = dict(exception_overrides)
    paths = exception_overrides.pop("paths")
    prepare_waivable_required_path(
        root,
        paths=paths,
        **exception_overrides,
    )
    git_commit(root)
    monkeypatch.setattr(
        validation_module,
        "_utc_now",
        lambda: validation_module.datetime(
            2026, 9, 27, 12, 0, tzinfo=validation_module.timezone.utc
        ),
    )

    report = validate_report(root)

    item = report.rule_evidence.results[0]
    assert item.raw_status == item.effective_status == "FAIL"
    assert item.exception_id is None
    assert rule_issues(report, "REPO-001")[0][0] == "BLOCKING"


def test_project_validate_rejects_ambiguous_applicable_exceptions(
    tmp_path,
    monkeypatch,
):
    root = init_project("Demo", tmp_path / "demo")
    decision_id = prepare_waivable_required_path(root)
    registry = load_yaml(root / EXCEPTIONS_PATH)
    registry["exceptions"]["EXC-20260927-abcdef13"] = governed_exception(
        "REPO-001",
        decision_id,
        paths=("missing-required-file",),
    )
    write_exceptions(root, registry["exceptions"])
    git_commit(root)
    monkeypatch.setattr(
        validation_module,
        "_utc_now",
        lambda: validation_module.datetime(
            2026, 9, 27, 12, 0, tzinfo=validation_module.timezone.utc
        ),
    )

    report = validate_report(root)

    assert report.rule_evidence is None
    assert any(
        level == "ERROR"
        and location == "rules_exceptions"
        and "multiple" in message
        for level, location, message in report.issues
    )


def test_project_validate_does_not_waive_error_or_pending(tmp_path, monkeypatch):
    root = init_project("Demo", tmp_path / "demo")
    _, decision_id = create_object(
        root,
        "decision",
        "Approve exception",
        "governance",
        "project-owner",
    )
    write_rules(
        root,
        {
            "REQ-001": rule(
                checker="knowledge.required_field",
                parameters={"object_type": "feature", "field": "owner"},
                exception_policy="decision_required",
            ),
            "PROC-001": rule(
                method="human",
                exception_policy="decision_required",
            ),
        },
    )
    write_exceptions(
        root,
        {
            "EXC-20260927-abcdef12": governed_exception(
                "REQ-001",
                decision_id,
                paths=("knowledge/**",),
            ),
            "EXC-20260927-abcdef13": governed_exception(
                "PROC-001",
                decision_id,
                paths=("docs/**",),
            ),
        },
    )
    (root / "knowledge/features/broken.md").write_text(
        "not frontmatter\n",
        encoding="utf-8",
    )
    git_commit(root)

    report = validate_report(root)
    by_id = {item.rule_id: item for item in report.rule_evidence.results}

    assert by_id["REQ-001"].raw_status == by_id["REQ-001"].effective_status == "ERROR"
    assert by_id["PROC-001"].raw_status == by_id["PROC-001"].effective_status == "PENDING"
    assert not report.rule_evidence.applied_exception_ids
    assert rule_issues(report, "REQ-001")[0][0] == "ERROR"
    monkeypatch.setattr("project_system.cli.find_root", lambda: root)
    with pytest.raises(SystemExit) as exit_info:
        main(["validate"])
    assert exit_info.value.code == 2


def test_knowledge_exception_requires_full_violation_scope(tmp_path):
    root = init_project("Demo", tmp_path / "demo")
    _, decision_id = create_object(
        root,
        "decision",
        "Approve partial exception",
        "governance",
        "project-owner",
    )
    first_path, _ = create_object(root, "feature", "First", "product", "owner")
    second_path, _ = create_object(root, "feature", "Second", "product", "owner")
    for path in (first_path, second_path):
        data, body = read_object(path)
        data.pop("owner")
        write_object(path, data, body)
    write_rules(
        root,
        {
            "REQ-001": rule(
                checker="knowledge.required_field",
                parameters={"object_type": "feature", "field": "owner"},
                exception_policy="decision_required",
            )
        },
    )
    write_exceptions(
        root,
        {
            "EXC-20260927-abcdef12": governed_exception(
                "REQ-001",
                decision_id,
                paths=(first_path.relative_to(root).as_posix(),),
            )
        },
    )
    git_commit(root)

    report = validate_report(root)

    item = report.rule_evidence.results[0]
    assert item.raw_status == item.effective_status == "FAIL"
    assert item.exception_id is None


def test_validation_clock_is_sampled_once_per_run_and_not_fingerprinted(
    tmp_path,
    monkeypatch,
):
    root = init_project("Demo", tmp_path / "demo")
    prepare_waivable_required_path(
        root,
        mode="temporary",
        expires_at="2026-09-27T13:00:00Z",
    )
    git_commit(root)

    times = iter(
        [
            validation_module.datetime(
                2026, 9, 27, 11, 0, tzinfo=validation_module.timezone.utc
            ),
            validation_module.datetime(
                2026, 9, 27, 11, 30, tzinfo=validation_module.timezone.utc
            ),
        ]
    )
    calls = []

    def fake_utc_now():
        calls.append(None)
        return next(times)

    monkeypatch.setattr(validation_module, "_utc_now", fake_utc_now)

    first = validate_report(root)
    second = validate_report(root)

    assert len(calls) == 2
    assert first.rule_evidence.results[0].effective_status == "WAIVED"
    assert second.rule_evidence.results[0].effective_status == "WAIVED"
    assert (
        first.rule_evidence.evidence_fingerprint
        == second.rule_evidence.evidence_fingerprint
    )


def test_validate_report_supports_explicit_sync_checkpoint_and_base_commit(tmp_path):
    root = init_project("Demo", tmp_path / "demo")
    write_rules(
        root,
        {
            "REPO-001": rule(
                checkpoints=["sync_verify"],
                severity="BLOCKING",
            )
        },
    )
    head = git_commit(root)

    default_report = validate_report(root)
    sync_report = validate_report(
        root,
        rule_checkpoint="sync_verify",
        rule_base_commit=head,
    )

    assert default_report.rule_evidence.checkpoint == "project_validate"
    assert default_report.rule_evidence.base_commit is None
    assert default_report.rule_evidence.results[0].raw_status == "NOT_APPLICABLE"
    assert sync_report.rule_evidence.checkpoint == "sync_verify"
    assert sync_report.rule_evidence.git_head == head
    assert sync_report.rule_evidence.base_commit == head
    assert sync_report.rule_evidence.results[0].raw_status == "PASS"
    assert validate(root) == list(default_report.issues)


def test_validate_report_passes_explicit_bounded_paths_without_changing_defaults(
    tmp_path,
    monkeypatch,
):
    root = init_project("Demo", tmp_path / "demo")
    write_rules(
        root,
        {"REPO-001": rule(scope=["docs/**"], severity="INFO")},
    )
    git_commit(root)
    captured = []
    actual_evaluate = validation_module.evaluate_rules

    def capture_context(registry, evaluation_context):
        captured.append(evaluation_context)
        return actual_evaluate(registry, evaluation_context)

    monkeypatch.setattr(validation_module, "evaluate_rules", capture_context)
    default_report = validate_report(root)
    bounded_report = validate_report(
        root,
        rule_evaluation_paths=("src/main.py",),
    )
    public_issues = validate(root)

    assert captured[0].evaluation_paths is None
    assert captured[1].evaluation_paths == ("src/main.py",)
    assert captured[2].evaluation_paths is None
    assert default_report.rule_evidence.results[0].raw_status == "PASS"
    assert bounded_report.rule_evidence.results[0].raw_status == "NOT_APPLICABLE"
    assert bounded_report.rule_evidence.results[0].details["reason"] == "scope_no_intersection"
    assert public_issues == list(default_report.issues)


def test_validate_report_rejects_unknown_rule_checkpoint(tmp_path):
    root = init_project("Demo", tmp_path / "demo")
    with pytest.raises(ValueError, match="checkpoint"):
        validate_report(root, rule_checkpoint="unknown_checkpoint")


def test_validate_report_uses_explicit_exception_clock_without_sampling_wall_clock(
    tmp_path,
    monkeypatch,
):
    root = init_project("Demo", tmp_path / "demo")
    prepare_waivable_required_path(
        root,
        mode="temporary",
        expires_at="2026-09-27T12:00:00Z",
    )
    head = git_commit(root)
    monkeypatch.setattr(
        validation_module,
        "_utc_now",
        lambda: (_ for _ in ()).throw(AssertionError("wall clock sampled")),
    )

    report = validate_report(
        root,
        rule_checkpoint="project_validate",
        rule_base_commit=head,
        rule_as_of=validation_module.datetime(
            2026,
            9,
            27,
            11,
            59,
            tzinfo=validation_module.timezone.utc,
        ),
    )

    assert report.rule_evidence.results[0].effective_status == "WAIVED"
    assert report.rule_evidence.base_commit == head

def test_sync_exception_registry_revocation_rechecks_existing_architecture_violation(
    tmp_path,
):
    root = init_project("Demo", tmp_path / "demo")

    _, decision_id = create_object(
        root,
        "decision",
        "Approve architecture exception",
        "governance",
        "project-owner",
    )

    selected_rule = architecture_rule(checkpoints=["sync_verify"])
    selected_rule["exception_policy"] = "decision_required"
    write_rules(root, {"ARCH-001": selected_rule})

    write_dart(
        root,
        "lib/domain/legacy.dart",
        "import '../presentation/legacy.dart';\n",
    )

    write_exceptions(
        root,
        {
            "EXC-20260927-abcdef12": governed_exception(
                "ARCH-001",
                decision_id,
                paths=("lib/domain/legacy.dart",),
            )
        },
    )

    head = git_commit(root)

    write_exceptions(
        root,
        {
            "EXC-20260927-abcdef12": governed_exception(
                "ARCH-001",
                decision_id,
                paths=("lib/domain/legacy.dart",),
                state="revoked",
            )
        },
    )

    report = validate_report(
        root,
        rule_checkpoint="sync_verify",
        rule_base_commit=head,
        rule_evaluation_paths=(EXCEPTIONS_PATH.as_posix(),),
        rule_as_of=validation_module.datetime(
            2026, 9, 27, 11, 30, tzinfo=validation_module.timezone.utc
        ),
    )

    result = report.rule_evidence.results[0]

    assert result.raw_status == "FAIL"
    assert result.effective_status == "FAIL"
    assert result.details["provider_evaluation_mode"] == "project_wide_invalidation"
    assert result.details["inspected_source_paths"] == ["lib/domain/legacy.dart"]
    assert rule_issues(report, "ARCH-001")[0][0] == "BLOCKING"


def test_sync_temporary_architecture_exception_rechecks_project_for_time_expiry(
    tmp_path,
):
    root = init_project("Demo", tmp_path / "demo")

    _, decision_id = create_object(
        root,
        "decision",
        "Approve temporary architecture exception",
        "governance",
        "project-owner",
    )

    selected_rule = architecture_rule(checkpoints=["sync_verify"])
    selected_rule["exception_policy"] = "decision_required"
    write_rules(root, {"ARCH-001": selected_rule})

    write_dart(
        root,
        "lib/domain/legacy.dart",
        "import '../presentation/legacy.dart';\n",
    )

    write_exceptions(
        root,
        {
            "EXC-20260927-abcdef12": governed_exception(
                "ARCH-001",
                decision_id,
                paths=("lib/domain/legacy.dart",),
                mode="temporary",
                expires_at="2026-09-27T12:00:00Z",
            )
        },
    )

    head = git_commit(root)

    (root / "README.md").write_text(
        "unrelated bounded change\n",
        encoding="utf-8",
    )

    before_expiry = validate_report(
        root,
        rule_checkpoint="sync_verify",
        rule_base_commit=head,
        rule_evaluation_paths=("README.md",),
        rule_as_of=validation_module.datetime(
            2026, 9, 27, 11, 59, 59, tzinfo=validation_module.timezone.utc
        ),
    )

    before_result = before_expiry.rule_evidence.results[0]

    assert before_result.raw_status == "FAIL"
    assert before_result.effective_status == "WAIVED"
    assert before_result.details["provider_evaluation_mode"] == "project_wide"
    assert before_result.details["inspected_source_paths"] == [
        "lib/domain/legacy.dart"
    ]

    at_expiry = validate_report(
        root,
        rule_checkpoint="sync_verify",
        rule_base_commit=head,
        rule_evaluation_paths=("README.md",),
        rule_as_of=validation_module.datetime(
            2026, 9, 27, 12, 0, tzinfo=validation_module.timezone.utc
        ),
    )

    expired_result = at_expiry.rule_evidence.results[0]

    assert expired_result.raw_status == "FAIL"
    assert expired_result.effective_status == "FAIL"
    assert expired_result.details["provider_evaluation_mode"] == "project_wide"
    assert expired_result.details["inspected_source_paths"] == [
        "lib/domain/legacy.dart"
    ]
    assert rule_issues(at_expiry, "ARCH-001")[0][0] == "BLOCKING"



def test_temporary_code_verification_exception_forces_complete_evaluation():
    from types import SimpleNamespace

    from project_system import validation as validation_module

    layer = SimpleNamespace(
        rules_registry={
            "rules": {
                "CODE-001": {
                    "status": "active",
                    "exception_policy": "decision_required",
                    "verification": {
                        "method": "deterministic",
                        "checker": "code.verification",
                        "parameters": {
                            "adapter": "dart.analyze",
                        },
                    },
                },
            },
        },
        exception_registry={
            "exceptions": {
                "EXC-20260930-code0001": {
                    "rule_id": "CODE-001",
                    "state": "active",
                    "mode": "temporary",
                },
            },
        },
    )

    assert (
        validation_module._temporary_exception_complete_evaluation_rule_ids(
            layer
        )
        == ("CODE-001",)
    )
