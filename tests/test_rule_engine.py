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


def context(
    root,
    checkpoint="project_validate",
    objects=None,
    evaluation_paths=None,
):
    return RuleEvaluationContext(
        project_root=Path(root),
        checkpoint=checkpoint,
        objects={} if objects is None else objects,
        evaluation_paths=evaluation_paths,
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


def test_project_wide_context_preserves_scoped_rule_execution(tmp_path):
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


def test_bounded_exact_scope_intersection_executes_rule(tmp_path):
    (tmp_path / "README.md").write_text("project\n", encoding="utf-8")
    result = evaluate_rules(
        registry(**{"REPO-001": rule(scope=["docs/README.md"])}),
        context(tmp_path, evaluation_paths=("docs/README.md",)),
    )[0]
    assert result.raw_status == "PASS"
    assert result.resolved_scope == ("docs/README.md",)


def test_bounded_scope_without_intersection_is_not_applicable(tmp_path):
    result = evaluate_rules(
        registry(**{"REPO-001": rule(scope=["docs/**"])}),
        context(tmp_path, evaluation_paths=("src/main.py",)),
    )[0]
    assert result.raw_status == "NOT_APPLICABLE"
    assert result.details == {"reason": "scope_no_intersection"}


def test_scope_non_intersection_does_not_invoke_checker(tmp_path, monkeypatch):
    import project_system.rule_checkers as checker_module

    monkeypatch.setattr(
        checker_module,
        "evaluate_checker",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("scope-non-applicable rule reached checker")
        ),
    )
    result = evaluate_rules(
        registry(**{"REPO-001": rule(scope=["docs/**"])}),
        context(tmp_path, evaluation_paths=("src/main.py",)),
    )[0]
    assert result.raw_status == "NOT_APPLICABLE"


def test_empty_bounded_scope_skips_scoped_rule(tmp_path):
    result = evaluate_rules(
        registry(**{"REPO-001": rule(scope=["docs/**"])}),
        context(tmp_path, evaluation_paths=()),
    )[0]
    assert result.raw_status == "NOT_APPLICABLE"
    assert result.details["reason"] == "scope_no_intersection"


def test_empty_bounded_scope_still_executes_unscoped_rule(tmp_path):
    (tmp_path / "README.md").write_text("project\n", encoding="utf-8")
    result = evaluate_rules(
        registry(**{"REPO-001": rule()}),
        context(tmp_path, evaluation_paths=()),
    )[0]
    assert result.raw_status == "PASS"


def test_checkpoint_mismatch_precedes_scope_mismatch(tmp_path):
    result = evaluate_rules(
        registry(**{
            "REPO-001": rule(
                checkpoints=["sync_verify"],
                scope=["docs/**"],
            )
        }),
        context(
            tmp_path,
            checkpoint="project_validate",
            evaluation_paths=("src/main.py",),
        ),
    )[0]
    assert result.raw_status == "NOT_APPLICABLE"
    assert result.details == {"reason": "checkpoint_not_configured"}


@pytest.mark.parametrize(
    ("pattern", "path", "matches"),
    [
        ("docs/*.md", "docs/a.md", True),
        ("docs/*.md", "docs/nested/a.md", False),
        ("docs/file?.md", "docs/file1.md", True),
        ("docs/file[ab].md", "docs/filea.md", True),
        ("docs/file[ab].md", "docs/filec.md", False),
        ("docs/**/*.md", "docs/a.md", True),
        ("docs/**/*.md", "docs/nested/a.md", True),
        ("src/**/test?.dart", "src/test1.dart", True),
        ("src/**/test?.dart", "src/a/b/test2.dart", True),
    ],
)
def test_bounded_scope_uses_approved_segment_glob_semantics(
    tmp_path,
    pattern,
    path,
    matches,
):
    (tmp_path / "README.md").write_text("project\n", encoding="utf-8")
    result = evaluate_rules(
        registry(**{"REPO-001": rule(scope=[pattern])}),
        context(tmp_path, evaluation_paths=(path,)),
    )[0]
    assert (result.raw_status == "PASS") is matches
    if not matches:
        assert result.details["reason"] == "scope_no_intersection"


@pytest.mark.parametrize(
    "evaluation_paths",
    [
        "docs/a.md",
        (1,),
        ("",),
        (" docs/a.md",),
        ("docs\\a.md",),
        ("C:/docs/a.md",),
        ("docs/a\x00.md",),
        ("/docs/a.md",),
        ("./docs/a.md",),
        ("docs/../a.md",),
        ("docs/*.md",),
        (".git/config",),
        (".GIT/config",),
        ("docs//a.md",),
    ],
)
def test_malformed_concrete_evaluation_paths_fail_closed(
    tmp_path,
    evaluation_paths,
):
    with pytest.raises((TypeError, ValueError), match="evaluation_paths"):
        context(tmp_path, evaluation_paths=evaluation_paths)


def test_evaluation_paths_use_canonical_set_semantics(tmp_path):
    first = context(
        tmp_path,
        evaluation_paths=("docs/b.md", "docs/a.md", "docs/b.md"),
    )
    second = context(
        tmp_path,
        evaluation_paths=("docs/a.md", "docs/b.md"),
    )
    assert first.evaluation_paths == ("docs/a.md", "docs/b.md")
    assert first == second
    assert first.evaluation_paths is not None
    assert context(tmp_path).evaluation_paths is None


def test_object_layer_completeness_defaults_fail_closed(tmp_path):
    ctx = RuleEvaluationContext(
        project_root=tmp_path,
        checkpoint="project_validate",
        objects={},
    )

    assert ctx.object_layer_complete is False
