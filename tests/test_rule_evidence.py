from dataclasses import replace
from pathlib import Path

import pytest

from project_system.rule_engine import RuleEvaluationContext, RuleEvaluationResult
from project_system.rule_evidence import (
    RuleEvidenceError,
    build_rule_evidence,
    canonical_sha256,
    rule_evidence_json,
    rule_evidence_to_dict,
)


GIT_HEAD = "1" * 40
BASE_COMMIT = "2" * 40


def rule(
    *,
    method="deterministic",
    checker="repository.required_path",
    severity="ERROR",
    scope=None,
    description="Evidence test rule.",
):
    verification = {"method": method}
    if method == "deterministic":
        verification.update(
            {
                "checker": checker,
                "parameters": {"path": "README.md"},
            }
        )
    value = {
        "title": "Evidence rule",
        "status": "active",
        "category": "repository",
        "description": description,
        "verification": verification,
        "enforcement": {
            "severity": severity,
            "checkpoints": ["project_validate"],
        },
        "exception_policy": "forbidden",
    }
    if scope is not None:
        value["scope"] = {"paths": list(scope)}
    return value


def rules_registry(rules=None):
    return {
        "schema_version": 1,
        "profile": "project-system-rules-v1",
        "rules": rules or {"REPO-001": rule()},
    }


def exception_registry(exceptions=None):
    return {
        "schema_version": 1,
        "profile": "project-system-rule-exceptions-v1",
        "exceptions": exceptions or {},
    }


def context(root, *, checkpoint="project_validate", objects=None, complete=True):
    return RuleEvaluationContext(
        project_root=Path(root),
        checkpoint=checkpoint,
        objects={} if objects is None else objects,
        object_layer_complete=complete,
    )


def result(
    *,
    rule_id="REPO-001",
    method="deterministic",
    checker="repository.required_path",
    checker_version="1",
    severity="ERROR",
    checkpoint="project_validate",
    scope=(),
    status="PASS",
    details=None,
    failure_reason=None,
):
    if method in {"ai", "human"}:
        checker = None
        checker_version = None
    return RuleEvaluationResult(
        rule_id=rule_id,
        verification_method=method,
        checker=checker,
        checker_version=checker_version,
        severity=severity,
        checkpoint=checkpoint,
        resolved_scope=tuple(scope),
        raw_status=status,
        details={} if details is None else details,
        failure_reason=failure_reason,
    )


def build(root, *, registry=None, exceptions=None, ctx=None, results=None, **identity):
    return build_rule_evidence(
        project_id=identity.pop("project_id", "project-system-cli"),
        git_head=identity.pop("git_head", GIT_HEAD),
        base_commit=identity.pop("base_commit", BASE_COMMIT),
        cli_version=identity.pop("cli_version", "0.11.0"),
        rules_registry=registry or rules_registry(),
        exception_registry=exceptions or exception_registry(),
        context=ctx or context(root),
        results=[result()] if results is None else results,
        **identity,
    )


def test_canonical_hash_ignores_mapping_insertion_order():
    assert canonical_sha256({"a": 1, "b": 2}) == canonical_sha256(
        {"b": 2, "a": 1}
    )


def test_canonical_hash_normalizes_tuple_like_list():
    assert canonical_sha256({"items": (1, 2)}) == canonical_sha256(
        {"items": [1, 2]}
    )


def test_canonical_hash_normalizes_path_to_posix():
    assert canonical_sha256({"path": Path("docs") / "file.md"}) == canonical_sha256(
        {"path": "docs/file.md"}
    )


@pytest.mark.parametrize(
    "value",
    [object(), {"values": {"a", "b"}}, {1: "not-a-string-key"}],
)
def test_canonical_hash_rejects_unsupported_values(value):
    with pytest.raises(RuleEvidenceError):
        canonical_sha256(value)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_canonical_hash_rejects_non_finite_float(value):
    with pytest.raises(RuleEvidenceError):
        canonical_sha256({"value": value})


def test_registry_hashes_are_semantic_and_order_independent(tmp_path):
    first_rules = rules_registry()
    reordered_rules = {
        "rules": first_rules["rules"],
        "profile": first_rules["profile"],
        "schema_version": first_rules["schema_version"],
    }
    first_exceptions = exception_registry()
    reordered_exceptions = {
        "exceptions": {},
        "profile": first_exceptions["profile"],
        "schema_version": 1,
    }
    first = build(tmp_path, registry=first_rules, exceptions=first_exceptions)
    second = build(
        tmp_path,
        registry=reordered_rules,
        exceptions=reordered_exceptions,
    )
    assert first.rules_registry_sha256 == second.rules_registry_sha256
    assert first.exception_registry_sha256 == second.exception_registry_sha256


def test_registry_hashes_change_with_semantic_content(tmp_path):
    baseline = build(tmp_path)
    changed_rule = build(
        tmp_path,
        registry=rules_registry(
            {"REPO-001": rule(description="Materially changed description.")}
        ),
    )
    changed_exception = build(
        tmp_path,
        exceptions=exception_registry(
            {"EXC-20260927-abcdef12": {"reason": "Different definition"}}
        ),
    )
    assert baseline.rules_registry_sha256 != changed_rule.rules_registry_sha256
    assert (
        baseline.exception_registry_sha256
        != changed_exception.exception_registry_sha256
    )


def test_rule_hash_is_semantic_and_changes_with_definition(tmp_path):
    first = build(tmp_path)
    reordered_definition = dict(reversed(list(rule().items())))
    reordered = build(
        tmp_path,
        registry=rules_registry({"REPO-001": reordered_definition}),
    )
    changed = build(
        tmp_path,
        registry=rules_registry(
            {"REPO-001": rule(description="Changed semantic definition.")}
        ),
    )
    assert first.results[0].rule_sha256 == reordered.results[0].rule_sha256
    assert first.results[0].rule_sha256 != changed.results[0].rule_sha256


def test_missing_referenced_rule_is_rejected(tmp_path):
    with pytest.raises(RuleEvidenceError, match="unknown rule_id"):
        build(
            tmp_path,
            registry=rules_registry({"OTHER-001": rule()}),
            results=[result()],
        )


def test_context_fingerprint_is_semantic_and_excludes_project_root(tmp_path):
    objects = {
        "REQ-20260927-abcdef12": {
            "path": Path("knowledge/requirements/REQ-20260927-abcdef12.md"),
            "data": {"type": "requirement", "title": "Required"},
        }
    }
    first = build(tmp_path / "checkout-a", ctx=context(tmp_path / "checkout-a", objects=objects))
    second = build(tmp_path / "checkout-b", ctx=context(tmp_path / "checkout-b", objects=objects))
    assert first.evaluation_context_sha256 == second.evaluation_context_sha256


@pytest.mark.parametrize("changed", ["checkpoint", "objects", "complete"])
def test_context_fingerprint_changes_with_semantic_context(tmp_path, changed):
    baseline_context = context(tmp_path, objects={"OBJ": {"data": {"value": 1}}})
    baseline = build(tmp_path, ctx=baseline_context)
    if changed == "checkpoint":
        altered_context = context(
            tmp_path,
            checkpoint="sync_verify",
            objects=baseline_context.objects,
        )
        altered_results = [result(checkpoint="sync_verify", status="NOT_APPLICABLE")]
    elif changed == "objects":
        altered_context = context(tmp_path, objects={"OBJ": {"data": {"value": 2}}})
        altered_results = [result()]
    else:
        altered_context = context(
            tmp_path,
            objects=baseline_context.objects,
            complete=False,
        )
        altered_results = [result()]
    altered = build(tmp_path, ctx=altered_context, results=altered_results)
    assert baseline.evaluation_context_sha256 != altered.evaluation_context_sha256


@pytest.mark.parametrize("status", ["PASS", "FAIL", "ERROR"])
def test_deterministic_result_conversion_preserves_raw_status(tmp_path, status):
    failure = "evaluation failed" if status in {"FAIL", "ERROR"} else None
    evidence = build(
        tmp_path,
        results=[result(status=status, details={"status": status}, failure_reason=failure)],
    )
    item = evidence.results[0]
    assert item.raw_status == status
    assert item.effective_status == status
    assert item.exception_id is None
    assert item.artifacts == ()


@pytest.mark.parametrize("method", ["ai", "human"])
def test_nondeterministic_pending_result_conversion(tmp_path, method):
    registry = rules_registry({"PROC-001": rule(method=method)})
    evidence = build(
        tmp_path,
        registry=registry,
        results=[
            result(
                rule_id="PROC-001",
                method=method,
                status="PENDING",
                details={"reason": f"{method}_verification_required"},
            )
        ],
    )
    item = evidence.results[0]
    assert item.checker is None
    assert item.checker_version is None
    assert item.raw_status == item.effective_status == "PENDING"
    assert item.exception_id is None
    assert item.artifacts == ()


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"project_id": ""}, "project_id"),
        ({"git_head": "abc123"}, "git_head"),
        ({"git_head": "A" * 40}, "git_head"),
        ({"base_commit": "not-a-commit"}, "base_commit"),
        ({"cli_version": ""}, "cli_version"),
    ],
)
def test_run_identity_rejects_malformed_values(tmp_path, overrides, message):
    with pytest.raises(RuleEvidenceError, match=message):
        build(tmp_path, **overrides)


def test_run_identity_accepts_none_or_full_lowercase_base_commit(tmp_path):
    without_base = build(tmp_path, base_commit=None)
    with_base = build(tmp_path, base_commit=BASE_COMMIT)
    assert without_base.base_commit is None
    assert with_base.base_commit == BASE_COMMIT


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ({"verification_method": "human"}, "verification_method"),
        ({"severity": "WARNING"}, "severity"),
        ({"checkpoint": "sync_verify"}, "checkpoint"),
        ({"checker": "repository.forbidden_path"}, "checker"),
        ({"checker_version": None}, "checker_version"),
        ({"resolved_scope": ("src/**",)}, "resolved_scope"),
        ({"raw_status": "WAIVED"}, "raw_status"),
    ],
)
def test_result_consistency_mismatches_are_rejected(tmp_path, mutation, message):
    bad = replace(result(), **mutation)
    with pytest.raises(RuleEvidenceError, match=message):
        build(tmp_path, results=[bad])


def test_ai_human_result_cannot_claim_checker_identity(tmp_path):
    registry = rules_registry({"PROC-001": rule(method="human")})
    bad = replace(
        result(rule_id="PROC-001", method="human", status="PENDING"),
        checker="repository.required_path",
        checker_version="1",
    )
    with pytest.raises(RuleEvidenceError, match="checker"):
        build(tmp_path, registry=registry, results=[bad])


def test_duplicate_result_rule_id_is_rejected(tmp_path):
    with pytest.raises(RuleEvidenceError, match="duplicate"):
        build(tmp_path, results=[result(), result(status="FAIL")])


def test_evidence_serialization_and_fingerprint_are_deterministic(tmp_path):
    first = build(tmp_path)
    second = build(tmp_path)
    assert rule_evidence_to_dict(first) == rule_evidence_to_dict(second)
    assert rule_evidence_json(first) == rule_evidence_json(second)
    assert first.evidence_fingerprint == second.evidence_fingerprint


@pytest.mark.parametrize("changed", ["git", "cli", "rule", "result", "context"])
def test_fingerprint_changes_when_bound_input_changes(tmp_path, changed):
    baseline = build(tmp_path)
    kwargs = {}
    if changed == "git":
        kwargs["git_head"] = "3" * 40
    elif changed == "cli":
        kwargs["cli_version"] = "0.11.1"
    elif changed == "rule":
        kwargs["registry"] = rules_registry(
            {"REPO-001": rule(description="Changed rule.")}
        )
    elif changed == "result":
        kwargs["results"] = [
            result(
                status="FAIL",
                details={"exists": False},
                failure_reason="required path is missing",
            )
        ]
    else:
        kwargs["ctx"] = context(tmp_path, objects={"OBJ": {"data": {"value": 1}}})
    altered = build(tmp_path, **kwargs)
    assert baseline.evidence_fingerprint != altered.evidence_fingerprint


def test_results_are_sorted_and_caller_order_does_not_affect_evidence(tmp_path):
    registry = rules_registry(
        {
            "REPO-002": rule(checker="repository.forbidden_path"),
            "REPO-001": rule(),
        }
    )
    first_result = result(rule_id="REPO-001")
    second_result = result(
        rule_id="REPO-002",
        checker="repository.forbidden_path",
    )
    first = build(tmp_path, registry=registry, results=[second_result, first_result])
    second = build(tmp_path, registry=registry, results=[first_result, second_result])
    assert [item.rule_id for item in first.results] == ["REPO-001", "REPO-002"]
    assert rule_evidence_to_dict(first) == rule_evidence_to_dict(second)


def test_json_form_contains_only_native_empty_exception_and_artifact_values(tmp_path):
    payload = rule_evidence_to_dict(build(tmp_path))
    assert payload["applied_exception_ids"] == []
    assert payload["results"][0]["artifacts"] == []
    assert payload["results"][0]["exception_id"] is None
    assert rule_evidence_json(build(tmp_path)).startswith('{"applied_exception_ids":[]')


@pytest.mark.parametrize("rule_status", ["draft", "deprecated"])
def test_evidence_rejects_result_for_non_active_rule(tmp_path, rule_status):
    selected = rule()
    selected["status"] = rule_status

    with pytest.raises(RuleEvidenceError, match="active"):
        build(
            tmp_path,
            registry=rules_registry({"REPO-001": selected}),
            results=[result()],
        )


@pytest.mark.parametrize(
    ("method", "status"),
    [
        ("deterministic", "PENDING"),
        ("ai", "PASS"),
        ("human", "ERROR"),
    ],
)
def test_evidence_rejects_status_impossible_for_verification_method(
    tmp_path,
    method,
    status,
):
    selected = rule(method=method)
    selected_result = result(
        rule_id="REPO-001",
        method=method,
        status=status,
    )

    with pytest.raises(RuleEvidenceError, match="raw_status"):
        build(
            tmp_path,
            registry=rules_registry({"REPO-001": selected}),
            results=[selected_result],
        )


def test_evidence_rejects_non_na_status_when_checkpoint_does_not_apply(tmp_path):
    selected = rule()
    selected["enforcement"]["checkpoints"] = ["sync_verify"]

    with pytest.raises(RuleEvidenceError, match="raw_status"):
        build(
            tmp_path,
            registry=rules_registry({"REPO-001": selected}),
            ctx=context(tmp_path, checkpoint="project_validate"),
            results=[result(status="PASS")],
        )


def test_serialization_rejects_stale_fingerprint_after_details_mutation(tmp_path):
    evidence = build(
        tmp_path,
        results=[
            result(
                details={"nested": {"value": 1}},
            )
        ],
    )

    evidence.results[0].details["nested"]["value"] = 2

    with pytest.raises(RuleEvidenceError, match="fingerprint"):
        rule_evidence_to_dict(evidence)