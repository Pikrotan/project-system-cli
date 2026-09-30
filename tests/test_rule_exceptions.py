from datetime import datetime, timezone
from pathlib import Path

import pytest

from project_system.rule_engine import RuleEvaluationContext, RuleEvaluationResult
from project_system.rule_exceptions import (
    RuleExceptionApplication,
    RuleExceptionResolutionError,
    resolve_rule_exceptions,
    scope_pattern_matches,
)


NOW = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)
DECISION_ID = "DEC-20260927-deadbeef"


def rule(*, checker="repository.required_path", policy="decision_required"):
    return {
        "title": "Exception test rule",
        "status": "active",
        "category": "repository",
        "description": "Exercise governed exception resolution.",
        "verification": {
            "method": "deterministic",
            "checker": checker,
            "parameters": {"path": "docs/required.md"},
        },
        "enforcement": {
            "severity": "ERROR",
            "checkpoints": ["project_validate"],
        },
        "exception_policy": policy,
    }


def registries(*, rules=None, exceptions=None):
    return (
        {
            "schema_version": 1,
            "profile": "project-system-rules-v1",
            "rules": rules or {"REPO-001": rule()},
        },
        {
            "schema_version": 1,
            "profile": "project-system-rule-exceptions-v1",
            "exceptions": exceptions or {},
        },
    )


def exception(
    *,
    rule_id="REPO-001",
    paths=("docs/required.md",),
    state="active",
    mode="permanent",
    decision_id=DECISION_ID,
    expires_at=None,
):
    value = {
        "rule_id": rule_id,
        "state": state,
        "mode": mode,
        "reason": "Approved governed exception.",
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
                "revocation_reason": "No longer approved.",
            }
        )
    return value


def context(root, *, objects=None):
    decision = {
        "path": Path(root) / f"knowledge/decisions/{DECISION_ID}.md",
        "data": {"id": DECISION_ID, "type": "decision"},
        "body": "Approved decision.",
    }
    values = {DECISION_ID: decision}
    values.update(objects or {})
    return RuleEvaluationContext(
        project_root=Path(root),
        checkpoint="project_validate",
        objects=values,
        object_layer_complete=True,
    )


def result(
    *,
    rule_id="REPO-001",
    checker="repository.required_path",
    status="FAIL",
    details=None,
):
    return RuleEvaluationResult(
        rule_id=rule_id,
        verification_method="deterministic",
        checker=checker,
        checker_version="1",
        severity="ERROR",
        checkpoint="project_validate",
        resolved_scope=(),
        raw_status=status,
        details={"path": "docs/required.md", "exists": False}
        if details is None
        else details,
        failure_reason="rule failed" if status == "FAIL" else None,
    )


def resolve(root, *, rules=None, exceptions=None, results=None, ctx=None, as_of=NOW):
    rules_registry, exception_registry = registries(
        rules=rules,
        exceptions=exceptions,
    )
    return resolve_rule_exceptions(
        rules_registry,
        exception_registry,
        ctx or context(root),
        [result()] if results is None else results,
        as_of=as_of,
    )


def test_valid_permanent_exception_applies_without_mutating_raw_result(tmp_path):
    raw = result()
    resolved = resolve(
        tmp_path,
        exceptions={"EXC-20260927-aaaa0001": exception()},
        results=[raw],
    )

    assert resolved.applications == (
        RuleExceptionApplication("REPO-001", "EXC-20260927-aaaa0001"),
    )
    assert raw.raw_status == "FAIL"


def architecture_details(*, malformed=False):
    violation = {
        "source_path": "lib/domain/legacy.dart",
        "directive": "import",
        "target_path": "lib/presentation/widget.dart",
        "target_uri": "../presentation/widget.dart",
    }
    if malformed:
        violation.pop("target_path")
    return {
        "provider_id": "dart.imports",
        "provider_version": "1",
        "fact_set_sha256": "0" * 64,
        "inspected_source_paths": ["lib/domain/legacy.dart"],
        "provider_evaluation_mode": "project_wide",
        "source_paths": ["lib/domain/**"],
        "forbidden_target_paths": ["lib/presentation/**"],
        "violations": [violation],
    }


def test_architecture_exception_scope_uses_violating_source_path(tmp_path):
    raw = result(
        rule_id="ARCH-001",
        checker="architecture.dependency_boundary",
        details=architecture_details(),
    )
    resolved = resolve(
        tmp_path,
        rules={
            "ARCH-001": rule(checker="architecture.dependency_boundary")
        },
        exceptions={
            "EXC-20260927-aaaa0001": exception(
                rule_id="ARCH-001",
                paths=("lib/domain/legacy.dart",),
            )
        },
        results=[raw],
    )

    assert resolved.applications == (
        RuleExceptionApplication("ARCH-001", "EXC-20260927-aaaa0001"),
    )


def test_malformed_architecture_violation_fails_exception_resolution(tmp_path):
    raw = result(
        rule_id="ARCH-001",
        checker="architecture.dependency_boundary",
        details=architecture_details(malformed=True),
    )
    with pytest.raises(RuleExceptionResolutionError, match="violations"):
        resolve(
            tmp_path,
            rules={
                "ARCH-001": rule(checker="architecture.dependency_boundary")
            },
            exceptions={
                "EXC-20260927-aaaa0001": exception(
                    rule_id="ARCH-001",
                    paths=("lib/domain/legacy.dart",),
                )
            },
            results=[raw],
        )


@pytest.mark.parametrize("policy", ["forbidden"])
def test_forbidden_exception_policy_never_waives(tmp_path, policy):
    assert resolve(
        tmp_path,
        rules={"REPO-001": rule(policy=policy)},
        exceptions={"EXC-20260927-aaaa0001": exception()},
    ).applications == ()


def test_revoked_exception_does_not_apply(tmp_path):
    assert resolve(
        tmp_path,
        exceptions={
            "EXC-20260927-aaaa0001": exception(state="revoked")
        },
    ).applications == ()


@pytest.mark.parametrize(
    ("as_of", "applies"),
    [
        (datetime(2026, 9, 27, 11, 59, 59, tzinfo=timezone.utc), True),
        (datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc), False),
        (datetime(2026, 9, 27, 12, 0, 1, tzinfo=timezone.utc), False),
    ],
)
def test_temporary_exception_expiry_boundary_is_explicit(tmp_path, as_of, applies):
    resolved = resolve(
        tmp_path,
        exceptions={
            "EXC-20260927-aaaa0001": exception(
                mode="temporary",
                expires_at="2026-09-27T12:00:00Z",
            )
        },
        as_of=as_of,
    )
    assert bool(resolved.applications) is applies


@pytest.mark.parametrize("bad_as_of", [None, "2026-09-27T12:00:00Z", datetime(2026, 9, 27)])
def test_as_of_must_be_an_aware_datetime(tmp_path, bad_as_of):
    with pytest.raises(RuleExceptionResolutionError, match="as_of"):
        resolve(tmp_path, as_of=bad_as_of)


def test_scope_matching_is_anchored_and_segment_aware():
    assert scope_pattern_matches("docs/*.md", "docs/file.md")
    assert not scope_pattern_matches("docs/*.md", "docs/nested/file.md")
    assert not scope_pattern_matches("*.md", "docs/file.md")
    assert scope_pattern_matches("docs/**/*.md", "docs/file.md")
    assert scope_pattern_matches("docs/**/*.md", "docs/nested/file.md")
    assert not scope_pattern_matches("docs/**/*.md", "src/file.md")
    assert scope_pattern_matches("docs/file?.[mt][dx]", "docs/file1.md")


def test_scope_mismatch_does_not_apply(tmp_path):
    assert resolve(
        tmp_path,
        exceptions={
            "EXC-20260927-aaaa0001": exception(paths=("src/**",))
        },
    ).applications == ()


@pytest.mark.parametrize(
    "checker",
    ["repository.required_path", "repository.forbidden_path"],
)
def test_repository_checker_violation_path_is_extracted(tmp_path, checker):
    details = {
        "path": "docs/required.md",
        "exists": checker == "repository.forbidden_path",
    }
    resolved = resolve(
        tmp_path,
        rules={"REPO-001": rule(checker=checker)},
        exceptions={"EXC-20260927-aaaa0001": exception()},
        results=[result(checker=checker, details=details)],
    )
    assert resolved.applications[0].rule_id == "REPO-001"


def test_knowledge_scope_requires_every_violating_object_path(tmp_path):
    objects = {
        "FEAT-001": {
            "path": Path(tmp_path) / "knowledge/features/FEAT-001-one.md",
            "data": {"id": "FEAT-001", "type": "feature"},
        },
        "FEAT-002": {
            "path": Path(tmp_path) / "knowledge/features/FEAT-002-two.md",
            "data": {"id": "FEAT-002", "type": "feature"},
        },
    }
    selected_result = result(
        checker="knowledge.required_field",
        details={"violating_object_ids": ["FEAT-002", "FEAT-001"]},
    )
    selected_rule = rule(checker="knowledge.required_field")
    full = resolve(
        tmp_path,
        rules={"REQ-001": selected_rule},
        exceptions={
            "EXC-20260927-aaaa0001": exception(
                rule_id="REQ-001",
                paths=("knowledge/features/**",),
            )
        },
        results=[replace_rule_id(selected_result, "REQ-001")],
        ctx=context(tmp_path, objects=objects),
    )
    partial = resolve(
        tmp_path,
        rules={"REQ-001": selected_rule},
        exceptions={
            "EXC-20260927-aaaa0001": exception(
                rule_id="REQ-001",
                paths=("knowledge/features/FEAT-001-one.md",),
            )
        },
        results=[replace_rule_id(selected_result, "REQ-001")],
        ctx=context(tmp_path, objects=objects),
    )
    assert full.applications
    assert partial.applications == ()


def replace_rule_id(value, rule_id):
    return RuleEvaluationResult(
        rule_id=rule_id,
        verification_method=value.verification_method,
        checker=value.checker,
        checker_version=value.checker_version,
        severity=value.severity,
        checkpoint=value.checkpoint,
        resolved_scope=value.resolved_scope,
        raw_status=value.raw_status,
        details=value.details,
        failure_reason=value.failure_reason,
    )


@pytest.mark.parametrize(
    "objects",
    [
        {},
        {"FEAT-001": {"data": {"id": "FEAT-001", "type": "feature"}}},
        {
            "FEAT-001": {
                "path": "../outside.md",
                "data": {"id": "FEAT-001", "type": "feature"},
            }
        },
    ],
)
def test_inconsistent_knowledge_violation_context_fails_closed(tmp_path, objects):
    selected = replace_rule_id(
        result(
            checker="knowledge.required_field",
            details={"violating_object_ids": ["FEAT-001"]},
        ),
        "REQ-001",
    )
    with pytest.raises(RuleExceptionResolutionError):
        resolve(
            tmp_path,
            rules={"REQ-001": rule(checker="knowledge.required_field")},
            exceptions={
                "EXC-20260927-aaaa0001": exception(
                    rule_id="REQ-001",
                    paths=("knowledge/**",),
                )
            },
            results=[selected],
            ctx=context(tmp_path, objects=objects),
        )


def test_absolute_object_paths_are_normalized_relative_to_checkout(tmp_path):
    def one_resolution(root):
        object_id = "FEAT-001"
        selected = replace_rule_id(
            result(
                checker="knowledge.required_field",
                details={"violating_object_ids": [object_id]},
            ),
            "REQ-001",
        )
        objects = {
            object_id: {
                "path": Path(root) / "knowledge/features/FEAT-001.md",
                "data": {"id": object_id, "type": "feature"},
            }
        }
        return resolve(
            root,
            rules={"REQ-001": rule(checker="knowledge.required_field")},
            exceptions={
                "EXC-20260927-aaaa0001": exception(
                    rule_id="REQ-001",
                    paths=("knowledge/features/*.md",),
                )
            },
            results=[selected],
            ctx=context(root, objects=objects),
        )

    assert one_resolution(tmp_path / "a") == one_resolution(tmp_path / "b")


@pytest.mark.parametrize("status", ["PASS", "ERROR", "PENDING", "NOT_APPLICABLE"])
def test_only_raw_fail_is_exception_eligible(tmp_path, status):
    assert resolve(
        tmp_path,
        exceptions={"EXC-20260927-aaaa0001": exception()},
        results=[result(status=status)],
    ).applications == ()


@pytest.mark.parametrize(
    ("objects", "message"),
    [
        ({}, "decision"),
        (
            {
                DECISION_ID: {
                    "path": "knowledge/features/not-a-decision.md",
                    "data": {"id": DECISION_ID, "type": "feature"},
                }
            },
            "decision",
        ),
    ],
)
def test_missing_or_wrong_type_decision_fails_closed(tmp_path, objects, message):
    selected_context = RuleEvaluationContext(
        project_root=tmp_path,
        checkpoint="project_validate",
        objects=objects,
        object_layer_complete=True,
    )
    with pytest.raises(RuleExceptionResolutionError, match=message):
        resolve(
            tmp_path,
            exceptions={"EXC-20260927-aaaa0001": exception()},
            ctx=selected_context,
        )


def test_two_applicable_exceptions_are_ambiguous_and_fail_closed(tmp_path):
    with pytest.raises(RuleExceptionResolutionError, match="multiple"):
        resolve(
            tmp_path,
            exceptions={
                "EXC-20260927-aaaa0001": exception(),
                "EXC-20260927-aaaa0002": exception(),
            },
        )


@pytest.mark.parametrize(
    "expires_at",
    ["not-a-date", "2026-09-27T12:00:00"],
)
def test_malformed_expiry_fails_closed(tmp_path, expires_at):
    with pytest.raises(RuleExceptionResolutionError, match="expires_at"):
        resolve(
            tmp_path,
            exceptions={
                "EXC-20260927-aaaa0001": exception(
                    mode="temporary",
                    expires_at=expires_at,
                )
            },
        )


def test_non_string_exception_identity_fails_closed(tmp_path):
    with pytest.raises(RuleExceptionResolutionError, match="exception_id"):
        resolve(tmp_path, exceptions={1: exception()})


def test_application_order_is_deterministic(tmp_path):
    rules = {
        "REPO-002": rule(checker="repository.forbidden_path"),
        "REPO-001": rule(),
    }
    exceptions = {
        "EXC-20260927-bbbb0002": exception(rule_id="REPO-002"),
        "EXC-20260927-aaaa0001": exception(rule_id="REPO-001"),
    }
    results = [
        result(
            rule_id="REPO-002",
            checker="repository.forbidden_path",
            details={"path": "docs/required.md", "exists": True},
        ),
        result(rule_id="REPO-001"),
    ]
    resolved = resolve(
        tmp_path,
        rules=rules,
        exceptions=exceptions,
        results=results,
    )
    assert [item.rule_id for item in resolved.applications] == [
        "REPO-001",
        "REPO-002",
    ]
    reversed_resolution = resolve(
        tmp_path,
        rules=dict(reversed(list(rules.items()))),
        exceptions=dict(reversed(list(exceptions.items()))),
        results=list(reversed(results)),
    )
    assert reversed_resolution == resolved


def test_non_rfc3339_expiry_fails_closed_even_if_fromisoformat_accepts_it(tmp_path):
    with pytest.raises(RuleExceptionResolutionError, match="expires_at"):
        resolve(
            tmp_path,
            exceptions={
                "EXC-20260927-aaaa0001": exception(
                    mode="temporary",
                    expires_at="2026-09-27T12:00:00+0000",
                )
            },
        )


def test_absolute_object_path_outside_project_root_fails_closed(tmp_path):
    outside = tmp_path.parent / "outside" / "FEAT-001.md"
    selected = replace_rule_id(
        result(
            checker="knowledge.required_field",
            details={"violating_object_ids": ["FEAT-001"]},
        ),
        "REQ-001",
    )
    objects = {
        "FEAT-001": {
            "path": outside,
            "data": {"id": "FEAT-001", "type": "feature"},
        }
    }

    with pytest.raises(RuleExceptionResolutionError, match="outside project_root"):
        resolve(
            tmp_path,
            rules={"REQ-001": rule(checker="knowledge.required_field")},
            exceptions={
                "EXC-20260927-aaaa0001": exception(
                    rule_id="REQ-001",
                    paths=("knowledge/**",),
                )
            },
            results=[selected],
            ctx=context(tmp_path, objects=objects),
        )



def test_code_verification_finding_paths_drive_exception_scope(tmp_path):
    raw = result(
        checker="code.verification",
        details={
            "findings": [
                {"path": "lib/a.dart"},
                {"path": "lib/a.dart"},
                {"path": "lib/nested/b.dart"},
            ],
        },
    )

    resolved = resolve(
        tmp_path,
        rules={
            "REPO-001": rule(
                checker="code.verification",
                policy="decision_required",
            ),
        },
        exceptions={
            "EXC-20260930-code0001": exception(
                paths=("lib/**",),
            ),
        },
        results=[raw],
    )

    assert resolved.applications == (
        RuleExceptionApplication(
            "REPO-001",
            "EXC-20260930-code0001",
        ),
    )


def test_code_verification_exception_scope_requires_concrete_finding_paths(
    tmp_path,
):
    raw = result(
        checker="code.verification",
        details={
            "findings": [
                {"path": None},
            ],
        },
    )

    with pytest.raises(
        RuleExceptionResolutionError,
        match="finding.*path|concrete",
    ):
        resolve(
            tmp_path,
            rules={
                "REPO-001": rule(
                    checker="code.verification",
                    policy="decision_required",
                ),
            },
            exceptions={
                "EXC-20260930-code0001": exception(
                    paths=("lib/**",),
                ),
            },
            results=[raw],
        )
