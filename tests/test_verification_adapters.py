from dataclasses import fields, replace
from pathlib import Path

import pytest

from project_system import verification_adapters as adapters


SHA_A = "a" * 64
SHA_B = "b" * 64


def finding(
    *,
    path="lib/a.dart",
    line=1,
    column=1,
    severity="WARNING",
    code="unused_import",
    message="Unused import.",
):
    return adapters.VerificationFinding(
        path=path,
        line=line,
        column=column,
        severity=severity,
        code=code,
        message=message,
    )


def result(
    *,
    adapter_id="fake.verify",
    adapter_version="1",
    tool_name="fake",
    tool_version="1.2.3",
    evaluation_mode="bounded",
    verification_status="PASS",
    inspected_paths=("lib/a.dart",),
    findings=None,
    exit_code=0,
    stdout_sha256=SHA_A,
    stderr_sha256=SHA_B,
):
    base = adapters.VerificationAdapterResult(
        adapter_id=adapter_id,
        adapter_version=adapter_version,
        tool_name=tool_name,
        tool_version=tool_version,
        evaluation_mode=evaluation_mode,
        verification_status=verification_status,
        inspected_paths=inspected_paths,
        findings=() if findings is None else findings,
        exit_code=exit_code,
        stdout_sha256=stdout_sha256,
        stderr_sha256=stderr_sha256,
        result_sha256="0" * 64,
    )
    return replace(
        base,
        result_sha256=adapters.verification_result_sha256(base),
    )


def test_evaluation_resolution_distinguishes_project_wide_bounded_and_invalidation(
    monkeypatch,
):
    spec = adapters.VerificationAdapterSpec(
        adapter_id="fake.verify",
        version="1",
        implementation=lambda root, paths, mode: None,
        global_input_patterns=("config.yaml",),
        executes_project_code=False,
    )
    monkeypatch.setattr(
        adapters,
        "VERIFICATION_ADAPTER_REGISTRY",
        {"fake.verify": spec},
    )

    assert adapters.resolve_verification_adapter_evaluation(
        "fake.verify", None
    ) == (None, "project_wide")

    assert adapters.resolve_verification_adapter_evaluation(
        "fake.verify", ()
    ) == ((), "bounded")

    assert adapters.resolve_verification_adapter_evaluation(
        "fake.verify", ("lib/a.dart",)
    ) == (("lib/a.dart",), "bounded")

    assert adapters.resolve_verification_adapter_evaluation(
        "fake.verify", ("config.yaml",)
    ) == (None, "project_wide_invalidation")


def test_adapter_metadata_fails_closed_when_identity_is_inconsistent(monkeypatch):
    spec = adapters.VerificationAdapterSpec(
        adapter_id="different.id",
        version="1",
        implementation=lambda root, paths, mode: None,
        executes_project_code=False,
    )
    monkeypatch.setattr(
        adapters,
        "VERIFICATION_ADAPTER_REGISTRY",
        {"fake.verify": spec},
    )

    with pytest.raises(adapters.VerificationAdapterError, match="identity"):
        adapters.resolve_verification_adapter_evaluation(
            "fake.verify",
            ("lib/a.dart",),
        )


@pytest.mark.parametrize(
    "mutation",
    [
        {"adapter_id": "other.verify"},
        {"adapter_version": "2"},
        {"evaluation_mode": "wrong"},
        {"verification_status": "UNKNOWN"},
        {"inspected_paths": ("lib/b.dart", "lib/a.dart")},
        {"findings": "__OUT_OF_ORDER_FINDINGS__"},
        {"exit_code": -1},
        {"stdout_sha256": "bad"},
        {"stderr_sha256": "bad"},
        {"result_sha256": "bad"},
    ],
)
def test_result_validation_rejects_malformed_or_inconsistent_results(
    monkeypatch,
    mutation,
):
    spec = adapters.VerificationAdapterSpec(
        adapter_id="fake.verify",
        version="1",
        implementation=lambda root, paths, mode: None,
        executes_project_code=False,
    )
    monkeypatch.setattr(
        adapters,
        "VERIFICATION_ADAPTER_REGISTRY",
        {"fake.verify": spec},
    )

    mutation = dict(mutation)
    if mutation.get("findings") == "__OUT_OF_ORDER_FINDINGS__":
        mutation["findings"] = (
            finding(path="lib/b.dart"),
            finding(path="lib/a.dart"),
        )

    bad = replace(result(), **mutation)

    with pytest.raises(adapters.VerificationAdapterError):
        adapters.validate_verification_adapter_result(
            bad,
            expected_adapter_id="fake.verify",
            bounded_evaluation_paths=("lib/a.dart",),
        )


def test_result_rejects_inspected_paths_outside_bounded_context(monkeypatch):
    spec = adapters.VerificationAdapterSpec(
        adapter_id="fake.verify",
        version="1",
        implementation=lambda root, paths, mode: None,
        executes_project_code=False,
    )
    monkeypatch.setattr(
        adapters,
        "VERIFICATION_ADAPTER_REGISTRY",
        {"fake.verify": spec},
    )

    bad = result(inspected_paths=("lib/a.dart", "lib/b.dart"))

    with pytest.raises(
        adapters.VerificationAdapterError,
        match="outside bounded evaluation_paths",
    ):
        adapters.validate_verification_adapter_result(
            bad,
            expected_adapter_id="fake.verify",
            bounded_evaluation_paths=("lib/a.dart",),
        )


def test_finding_source_must_be_reported_as_inspected(monkeypatch):
    spec = adapters.VerificationAdapterSpec(
        adapter_id="fake.verify",
        version="1",
        implementation=lambda root, paths, mode: None,
        executes_project_code=False,
    )
    monkeypatch.setattr(
        adapters,
        "VERIFICATION_ADAPTER_REGISTRY",
        {"fake.verify": spec},
    )

    bad = result(
        inspected_paths=("lib/a.dart",),
        findings=(finding(path="lib/b.dart"),),
    )

    with pytest.raises(
        adapters.VerificationAdapterError,
        match="not reported as inspected",
    ):
        adapters.validate_verification_adapter_result(
            bad,
            expected_adapter_id="fake.verify",
            bounded_evaluation_paths=("lib/a.dart",),
        )


def test_result_hash_is_deterministic_and_semantic(monkeypatch):
    spec = adapters.VerificationAdapterSpec(
        adapter_id="fake.verify",
        version="1",
        implementation=lambda root, paths, mode: None,
        executes_project_code=False,
    )
    monkeypatch.setattr(
        adapters,
        "VERIFICATION_ADAPTER_REGISTRY",
        {"fake.verify": spec},
    )

    first = result()
    second = result()

    adapters.validate_verification_adapter_result(
        first,
        expected_adapter_id="fake.verify",
        bounded_evaluation_paths=("lib/a.dart",),
    )
    adapters.validate_verification_adapter_result(
        second,
        expected_adapter_id="fake.verify",
        bounded_evaluation_paths=("lib/a.dart",),
    )

    assert first.result_sha256 == second.result_sha256
    assert first.result_sha256 != adapters.verification_result_sha256(
        replace(first, exit_code=3)
    )
    assert first.result_sha256 != adapters.verification_result_sha256(
        replace(first, verification_status="FAIL")
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("severity", "CRITICAL"),
        ("code", ""),
        ("message", ""),
        ("line", 0),
        ("column", 0),
    ],
)
def test_finding_contract_fails_closed(monkeypatch, field, value):
    spec = adapters.VerificationAdapterSpec(
        adapter_id="fake.verify",
        version="1",
        implementation=lambda root, paths, mode: None,
        executes_project_code=False,
    )
    monkeypatch.setattr(
        adapters,
        "VERIFICATION_ADAPTER_REGISTRY",
        {"fake.verify": spec},
    )

    bad_finding = replace(finding(), **{field: value})
    bad = replace(
        result(),
        findings=(bad_finding,),
    )

    with pytest.raises(adapters.VerificationAdapterError):
        adapters.validate_verification_adapter_result(
            bad,
            expected_adapter_id="fake.verify",
            bounded_evaluation_paths=("lib/a.dart",),
        )


def test_evaluate_adapter_passes_resolved_bounded_context_and_returns_validated_result(
    tmp_path,
    monkeypatch,
):
    calls = []

    def implementation(root, paths, mode):
        calls.append((root, paths, mode))
        return result(
            evaluation_mode=mode,
            inspected_paths=paths,
        )

    spec = adapters.VerificationAdapterSpec(
        adapter_id="fake.verify",
        version="1",
        implementation=implementation,
        executes_project_code=False,
    )
    monkeypatch.setattr(
        adapters,
        "VERIFICATION_ADAPTER_REGISTRY",
        {"fake.verify": spec},
    )

    actual = adapters.evaluate_verification_adapter(
        "fake.verify",
        tmp_path,
        evaluation_paths=("lib/a.dart",),
    )

    assert actual.adapter_id == "fake.verify"
    assert actual.evaluation_mode == "bounded"
    assert calls == [
        (
            Path(tmp_path).resolve(),
            ("lib/a.dart",),
            "bounded",
        )
    ]


def test_evaluate_adapter_passes_project_wide_invalidation_to_implementation(
    tmp_path,
    monkeypatch,
):
    calls = []

    def implementation(root, paths, mode):
        calls.append((root, paths, mode))
        return result(
            evaluation_mode=mode,
            inspected_paths=(),
        )

    spec = adapters.VerificationAdapterSpec(
        adapter_id="fake.verify",
        version="1",
        implementation=implementation,
        global_input_patterns=("config.yaml",),
        executes_project_code=False,
    )
    monkeypatch.setattr(
        adapters,
        "VERIFICATION_ADAPTER_REGISTRY",
        {"fake.verify": spec},
    )

    actual = adapters.evaluate_verification_adapter(
        "fake.verify",
        tmp_path,
        evaluation_paths=("config.yaml",),
    )

    assert actual.evaluation_mode == "project_wide_invalidation"
    assert calls == [
        (
            Path(tmp_path).resolve(),
            None,
            "project_wide_invalidation",
        )
    ]


def test_evaluate_adapter_rejects_result_mode_that_disagrees_with_resolved_mode(
    tmp_path,
    monkeypatch,
):
    def implementation(root, paths, mode):
        assert mode == "project_wide_invalidation"
        return result(
            evaluation_mode="bounded",
            inspected_paths=("lib/a.dart",),
        )

    spec = adapters.VerificationAdapterSpec(
        adapter_id="fake.verify",
        version="1",
        implementation=implementation,
        global_input_patterns=("config.yaml",),
        executes_project_code=False,
    )
    monkeypatch.setattr(
        adapters,
        "VERIFICATION_ADAPTER_REGISTRY",
        {"fake.verify": spec},
    )

    with pytest.raises(
        adapters.VerificationAdapterError,
        match="returned an invalid result",
    ) as captured:
        adapters.evaluate_verification_adapter(
            "fake.verify",
            tmp_path,
            evaluation_paths=("config.yaml",),
        )

    assert "evaluation_mode" not in str(captured.value)


def test_evaluate_adapter_revalidates_malformed_implementation_output(
    tmp_path,
    monkeypatch,
):
    spec = adapters.VerificationAdapterSpec(
        adapter_id="fake.verify",
        version="1",
        implementation=lambda root, paths, mode: object(),
        executes_project_code=False,
    )
    monkeypatch.setattr(
        adapters,
        "VERIFICATION_ADAPTER_REGISTRY",
        {"fake.verify": spec},
    )

    with pytest.raises(
        adapters.VerificationAdapterError,
        match="returned an invalid result",
    ) as captured:
        adapters.evaluate_verification_adapter(
            "fake.verify",
            tmp_path,
            evaluation_paths=("lib/a.dart",),
        )

    assert "malformed result" not in str(captured.value)


def test_evaluate_adapter_wraps_implementation_failure_without_leaking_child_detail(
    tmp_path,
    monkeypatch,
):
    def implementation(root, paths, mode):
        raise RuntimeError("SECRET_TOOL_DETAIL")

    spec = adapters.VerificationAdapterSpec(
        adapter_id="fake.verify",
        version="1",
        implementation=implementation,
        executes_project_code=False,
    )
    monkeypatch.setattr(
        adapters,
        "VERIFICATION_ADAPTER_REGISTRY",
        {"fake.verify": spec},
    )

    with pytest.raises(adapters.VerificationAdapterError) as captured:
        adapters.evaluate_verification_adapter(
            "fake.verify",
            tmp_path,
            evaluation_paths=("lib/a.dart",),
        )

    assert "SECRET_TOOL_DETAIL" not in str(captured.value)
    assert "fake.verify" in str(captured.value)


def test_result_validation_rejects_mode_that_disagrees_with_authoritative_mode(
    monkeypatch,
):
    spec = adapters.VerificationAdapterSpec(
        adapter_id="fake.verify",
        version="1",
        implementation=lambda root, paths, mode: None,
        executes_project_code=False,
    )
    monkeypatch.setattr(
        adapters,
        "VERIFICATION_ADAPTER_REGISTRY",
        {"fake.verify": spec},
    )

    bad = result(evaluation_mode="project_wide")

    with pytest.raises(
        adapters.VerificationAdapterError,
        match="evaluation_mode",
    ):
        adapters.validate_verification_adapter_result(
            bad,
            expected_adapter_id="fake.verify",
            bounded_evaluation_paths=("lib/a.dart",),
            expected_evaluation_mode="bounded",
        )


def test_verification_result_hash_fails_closed_for_malformed_findings():
    malformed = adapters.VerificationAdapterResult(
        adapter_id="fake.verify",
        adapter_version="1",
        tool_name="fake",
        tool_version="1.2.3",
        evaluation_mode="bounded",
        verification_status="PASS",
        inspected_paths=("lib/a.dart",),
        findings=(object(),),
        exit_code=0,
        stdout_sha256=SHA_A,
        stderr_sha256=SHA_B,
        result_sha256="0" * 64,
    )

    with pytest.raises(
        adapters.VerificationAdapterError,
        match="findings",
    ):
        adapters.verification_result_sha256(malformed)


def test_project_wide_result_does_not_require_finding_paths_in_inspected_paths(
    monkeypatch,
):
    spec = adapters.VerificationAdapterSpec(
        adapter_id="fake.verify",
        version="1",
        implementation=lambda root, paths, mode: None,
        executes_project_code=False,
    )
    monkeypatch.setattr(
        adapters,
        "VERIFICATION_ADAPTER_REGISTRY",
        {"fake.verify": spec},
    )

    complete = result(
        evaluation_mode="project_wide",
        inspected_paths=(),
        findings=(finding(path="lib/a.dart"),),
    )

    adapters.validate_verification_adapter_result(
        complete,
        expected_adapter_id="fake.verify",
        expected_evaluation_mode="project_wide",
    )


def test_bounded_result_still_requires_finding_paths_in_inspected_paths(
    monkeypatch,
):
    spec = adapters.VerificationAdapterSpec(
        adapter_id="fake.verify",
        version="1",
        implementation=lambda root, paths, mode: None,
        executes_project_code=False,
    )
    monkeypatch.setattr(
        adapters,
        "VERIFICATION_ADAPTER_REGISTRY",
        {"fake.verify": spec},
    )

    bad = result(
        evaluation_mode="bounded",
        inspected_paths=(),
        findings=(finding(path="lib/a.dart"),),
    )

    with pytest.raises(
        adapters.VerificationAdapterError,
        match="not reported as inspected",
    ):
        adapters.validate_verification_adapter_result(
            bad,
            expected_adapter_id="fake.verify",
            bounded_evaluation_paths=("lib/a.dart",),
            expected_evaluation_mode="bounded",
        )


def test_evaluate_adapter_sanitizes_result_validation_details(
    tmp_path,
    monkeypatch,
):
    def implementation(root, paths, mode):
        return result(
            evaluation_mode=mode,
            inspected_paths=("secret/private.dart",),
        )

    spec = adapters.VerificationAdapterSpec(
        adapter_id="fake.verify",
        version="1",
        implementation=implementation,
        executes_project_code=False,
    )
    monkeypatch.setattr(
        adapters,
        "VERIFICATION_ADAPTER_REGISTRY",
        {"fake.verify": spec},
    )

    with pytest.raises(adapters.VerificationAdapterError) as captured:
        adapters.evaluate_verification_adapter(
            "fake.verify",
            tmp_path,
            evaluation_paths=("lib/a.dart",),
        )

    message = str(captured.value)

    assert "fake.verify" in message
    assert "secret/private.dart" not in message
    assert "outside bounded" not in message


@pytest.mark.parametrize(
    "mode",
    ["project_wide", "project_wide_invalidation"],
)
def test_complete_evaluation_rejects_concrete_inspected_paths(
    monkeypatch,
    mode,
):
    spec = adapters.VerificationAdapterSpec(
        adapter_id="fake.verify",
        version="1",
        implementation=lambda root, paths, resolved_mode: None,
        executes_project_code=False,
    )
    monkeypatch.setattr(
        adapters,
        "VERIFICATION_ADAPTER_REGISTRY",
        {"fake.verify": spec},
    )

    bad = result(
        evaluation_mode=mode,
        inspected_paths=("lib/a.dart",),
    )

    with pytest.raises(
        adapters.VerificationAdapterError,
        match="inspected_paths",
    ):
        adapters.validate_verification_adapter_result(
            bad,
            expected_adapter_id="fake.verify",
            expected_evaluation_mode=mode,
        )


def test_verification_adapter_result_exposes_semantic_status():
    field_names = {
        field.name
        for field in fields(adapters.VerificationAdapterResult)
    }

    assert "verification_status" in field_names


def test_legacy_adapter_cannot_switch_to_semantic_hash_contract():
    legacy = result(adapter_id="dart.analyze", evaluation_mode="project_wide",
                    inspected_paths=())
    changed = replace(legacy, semantic_sha256="c" * 64)
    changed = replace(changed, result_sha256=adapters.verification_result_sha256(changed))
    with pytest.raises(adapters.VerificationAdapterError, match="semantic hash contract"):
        adapters.validate_verification_adapter_result(changed, expected_adapter_id="dart.analyze")


@pytest.mark.parametrize("field", ["stdout_sha256", "stderr_sha256"])
def test_semantic_adapter_still_validates_raw_execution_provenance_hashes(field):
    semantic = replace(
        result(adapter_id="dart.test", evaluation_mode="project_wide", inspected_paths=()),
        semantic_sha256="c" * 64,
    )
    semantic = replace(semantic, result_sha256=adapters.verification_result_sha256(semantic))
    malformed = replace(semantic, **{field: "bad"})
    with pytest.raises(adapters.VerificationAdapterError, match=field):
        adapters.validate_verification_adapter_result(malformed, expected_adapter_id="dart.test")



def test_evaluator_rejects_missing_project_root(tmp_path):
    from project_system.verification_adapters import (
        VerificationAdapterError,
        evaluate_verification_adapter,
    )

    missing_root = tmp_path / "missing-project"

    with pytest.raises(
        VerificationAdapterError,
        match="project_root",
    ):
        evaluate_verification_adapter(
            "dart.analyze",
            missing_root,
            evaluation_paths=("README.md",),
        )


def test_result_rejects_coordinates_without_finding_path():
    from dataclasses import replace

    from project_system.verification_adapters import (
        VerificationAdapterError,
        VerificationAdapterResult,
        VerificationFinding,
        validate_verification_adapter_result,
        verification_result_sha256,
    )

    malformed_finding = VerificationFinding(
        path=None,
        line=7,
        column=3,
        severity="INFO",
        code="example",
        message="pathless coordinate",
    )

    base = VerificationAdapterResult(
        adapter_id="dart.analyze",
        adapter_version="1",
        tool_name="dart",
        tool_version="test",
        evaluation_mode="project_wide",
        verification_status="PASS",
        inspected_paths=(),
        findings=(malformed_finding,),
        exit_code=0,
        stdout_sha256="a" * 64,
        stderr_sha256="b" * 64,
        result_sha256="0" * 64,
    )

    with pytest.raises(
        VerificationAdapterError,
        match="path.*line|line.*path|coordinates",
    ):
        verification_result_sha256(base)

    with pytest.raises(
        VerificationAdapterError,
        match="path.*line|line.*path|coordinates",
    ):
        validate_verification_adapter_result(
            base,
            expected_adapter_id="dart.analyze",
            expected_evaluation_mode="project_wide",
        )
