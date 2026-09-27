from pathlib import Path

import pytest

from project_system.rule_engine import RuleEvaluationContext, evaluate_rules


def rule(
    *,
    method="deterministic",
    checker="repository.required_path",
    parameters=None,
    status="active",
    checkpoints=None,
    severity="ERROR",
    scope=None,
):
    verification = {"method": method}
    if method == "deterministic":
        verification["checker"] = checker
        verification["parameters"] = (
            {"path": "README.md"} if parameters is None else parameters
        )
    data = {
        "title": "Rule",
        "status": status,
        "category": "repository",
        "description": "Rule engine test.",
        "verification": verification,
        "enforcement": {
            "severity": severity,
            "checkpoints": checkpoints or ["project_validate"],
        },
        "exception_policy": "forbidden",
    }
    if scope is not None:
        data["scope"] = {"paths": scope}
    return data


def registry(**rules):
    return {
        "schema_version": 1,
        "profile": "project-system-rules-v1",
        "rules": rules,
    }


def context(root, checkpoint="project_validate", objects=None):
    return RuleEvaluationContext(
        project_root=Path(root),
        checkpoint=checkpoint,
        objects={} if objects is None else objects,
    )


def test_active_deterministic_rule_executes(tmp_path):
    (tmp_path / "README.md").write_text("project\n", encoding="utf-8")
    results = evaluate_rules(
        registry(**{"REPO-001": rule()}),
        context(tmp_path),
    )
    assert len(results) == 1
    assert results[0].raw_status == "PASS"
    assert results[0].checker == "repository.required_path"
    assert results[0].checker_version == "1"


@pytest.mark.parametrize("status", ["draft", "deprecated"])
def test_non_active_rule_is_omitted(status, tmp_path):
    results = evaluate_rules(
        registry(**{"REPO-001": rule(status=status)}),
        context(tmp_path),
    )
    assert results == []


def test_checkpoint_mismatch_is_explicitly_not_applicable(tmp_path):
    results = evaluate_rules(
        registry(**{"REPO-001": rule(checkpoints=["sync_verify"])}),
        context(tmp_path, checkpoint="project_validate"),
    )
    assert results[0].raw_status == "NOT_APPLICABLE"
    assert results[0].details["reason"] == "checkpoint_not_configured"


@pytest.mark.parametrize("method", ["ai", "human"])
def test_nondeterministic_verification_is_pending(method, tmp_path):
    results = evaluate_rules(
        registry(**{"PROC-001": rule(method=method)}),
        context(tmp_path),
    )
    result = results[0]
    assert result.raw_status == "PENDING"
    assert result.verification_method == method
    assert result.checker is None
    assert result.checker_version is None


def test_unexpected_checker_exception_becomes_error(tmp_path, monkeypatch):
    import project_system.rule_checkers as checker_module

    def explode(*args, **kwargs):
        raise RuntimeError("checker exploded")

    monkeypatch.setattr(checker_module, "_inspect_repository_path", explode)
    result = evaluate_rules(
        registry(**{"REPO-001": rule()}),
        context(tmp_path),
    )[0]
    assert result.raw_status == "ERROR"
    assert "checker exploded" in result.failure_reason


def test_unknown_checker_runtime_fallback_is_error(tmp_path):
    result = evaluate_rules(
        registry(
            **{
                "REPO-001": rule(checker="repository.not_registered"),
            }
        ),
        context(tmp_path),
    )[0]
    assert result.raw_status == "ERROR"
    assert result.checker_version is None
    assert "unknown deterministic checker" in result.failure_reason


def test_checkpoint_mismatch_precedes_unknown_checker_runtime_fallback(tmp_path):
    result = evaluate_rules(
        registry(
            **{
                "REPO-001": rule(
                    checker="repository.not_registered",
                    checkpoints=["sync_verify"],
                ),
            }
        ),
        context(tmp_path, checkpoint="project_validate"),
    )[0]
    assert result.raw_status == "NOT_APPLICABLE"
    assert result.details["reason"] == "checkpoint_not_configured"


def test_severity_and_scope_are_carried_without_task_interpretation(tmp_path):
    (tmp_path / "README.md").write_text("project\n", encoding="utf-8")
    scope = ["src/**", "docs/*.md"]
    result = evaluate_rules(
        registry(
            **{
                "REPO-001": rule(severity="WARNING", scope=scope),
            }
        ),
        context(tmp_path),
    )[0]
    assert result.severity == "WARNING"
    assert result.resolved_scope == tuple(scope)
    assert result.raw_status == "PASS"


def test_object_layer_completeness_defaults_fail_closed(tmp_path):
    ctx = RuleEvaluationContext(
        project_root=tmp_path,
        checkpoint="project_validate",
        objects={},
    )

    assert ctx.object_layer_complete is False