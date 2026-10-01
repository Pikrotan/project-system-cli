import hashlib
import json
from pathlib import Path
import subprocess

import pytest

from project_system import dart_test_adapter
from project_system import verification_adapters as adapters


def _event(event_type, time=0, **values):
    return {"type": event_type, "time": time, **values}


def _start(*, time=0, runner_version="1.31.0"):
    return _event(
        "start",
        time,
        protocolVersion="0.1.1",
        runnerVersion=runner_version,
        pid=1234,
    )


def _suite(path, *, suite_id=0, time=1):
    return _event(
        "suite",
        time,
        suite={"id": suite_id, "platform": "vm", "path": path},
    )


def _test_start(
    name,
    *,
    test_id=1,
    suite_id=0,
    line=1,
    column=1,
    time=2,
):
    return _event(
        "testStart",
        time,
        test={
            "id": test_id,
            "name": name,
            "suiteID": suite_id,
            "groupIDs": [],
            "line": line,
            "column": column,
            "url": "package:example/example_test.dart",
            "metadata": {"skip": False, "skipReason": None},
        },
    )


def _test_done(
    result="success",
    *,
    test_id=1,
    skipped=False,
    hidden=False,
    time=3,
):
    return _event(
        "testDone",
        time,
        testID=test_id,
        result=result,
        skipped=skipped,
        hidden=hidden,
    )


def _json_stream(*events):
    return "".join(
        json.dumps(event, separators=(",", ":")) + "\n"
        for event in events
    )


def _passing_output(path="test/example_test.dart", *, time_offset=0):
    return _json_stream(
        _start(time=time_offset),
        _event("allSuites", time_offset + 1, count=1),
        _suite(path, time=time_offset + 2),
        _test_start("passes", time=time_offset + 3),
        _test_done(time=time_offset + 4),
        _event("done", time_offset + 5, success=True),
    )


def _completed(*, returncode, stdout, stderr=""):
    return type(
        "Completed",
        (),
        {
            "returncode": returncode,
            "stdout": stdout,
            "stderr": stderr,
        },
    )()


def _write_test(root, relative="test/example_test.dart"):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("void main() {}\n", encoding="utf-8")
    return path


def test_dart_test_adapter_is_allowlisted_as_project_code_execution():
    spec = adapters.VERIFICATION_ADAPTER_REGISTRY["dart.test"]

    assert spec.adapter_id == "dart.test"
    assert spec.version == "1"
    assert spec.implementation is dart_test_adapter.run_dart_test
    assert spec.executes_project_code is True
    assert spec.global_input_patterns == ("**",)


def test_dart_test_applicability_is_fail_safe_without_test_selection_heuristics():
    assert adapters.resolve_verification_adapter_evaluation(
        "dart.test",
        ("README.md",),
    ) == (None, "project_wide_invalidation")

    assert adapters.resolve_verification_adapter_evaluation(
        "dart.test",
        ("lib/main.dart",),
    ) == (None, "project_wide_invalidation")

    assert adapters.resolve_verification_adapter_evaluation(
        "dart.test",
        ("config/team_test_options.yaml",),
    ) == (None, "project_wide_invalidation")

    assert adapters.resolve_verification_adapter_evaluation(
        "dart.test",
        ("pubspec.lock",),
    ) == (None, "project_wide_invalidation")


def test_empty_bounded_change_is_not_applicable_and_does_not_execute(
    tmp_path,
    monkeypatch,
):
    calls = []

    def fail_if_called(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("bounded NOT_APPLICABLE must not execute Dart")

    monkeypatch.setattr(dart_test_adapter, "run_process", fail_if_called)

    result = adapters.evaluate_verification_adapter(
        "dart.test",
        tmp_path,
        evaluation_paths=(),
    )

    assert calls == []
    assert result.adapter_id == "dart.test"
    assert result.adapter_version == "1"
    assert result.tool_name == "dart test"
    assert result.tool_version == "not_executed"
    assert result.evaluation_mode == "bounded"
    assert result.verification_status == "NOT_APPLICABLE"
    assert result.inspected_paths == ()
    assert result.findings == ()
    assert result.exit_code == 0


@pytest.mark.parametrize(
    ("evaluation_paths", "expected_mode"),
    [
        (None, "project_wide"),
        (("lib/main.dart",), "project_wide_invalidation"),
        (("README.md",), "project_wide_invalidation"),
        (("test/fixtures/data.json",), "project_wide_invalidation"),
        (("assets/nested/golden.png",), "project_wide_invalidation"),
    ],
)
def test_dart_test_uses_fixed_safe_project_wide_invocation(
    tmp_path,
    monkeypatch,
    evaluation_paths,
    expected_mode,
):
    _write_test(tmp_path)
    stdout = _passing_output()
    calls = []

    def fake_run_process(arguments, **kwargs):
        calls.append((tuple(arguments), dict(kwargs)))
        return _completed(returncode=0, stdout=stdout)

    monkeypatch.setattr(dart_test_adapter, "run_process", fake_run_process)

    result = adapters.evaluate_verification_adapter(
        "dart.test",
        tmp_path,
        evaluation_paths=evaluation_paths,
    )

    assert calls == [
        (
            ("dart", "test", "--reporter", "json"),
            {
                "cwd": Path(tmp_path).resolve(),
                "capture_output": True,
                "text": True,
                "encoding": "utf-8",
                "check": False,
                "shell": False,
                "timeout": 300,
                "max_capture_bytes": 16 * 1024 * 1024,
            },
        )
    ]
    assert result.adapter_id == "dart.test"
    assert result.adapter_version == "1"
    assert result.tool_name == "dart test"
    assert result.tool_version == "1.31.0"
    assert result.evaluation_mode == expected_mode
    assert result.verification_status == "PASS"
    assert result.inspected_paths == ()
    assert result.findings == ()
    assert result.exit_code == 0
    assert result.stdout_sha256 == hashlib.sha256(
        stdout.encode("utf-8")
    ).hexdigest()
    assert result.stderr_sha256 == hashlib.sha256(b"").hexdigest()
    assert result.result_sha256 == adapters.verification_result_sha256(result)


def test_genuine_test_failures_are_normalized_sorted_and_deduplicated(
    tmp_path,
    monkeypatch,
):
    _write_test(tmp_path, "test/z_test.dart")
    _write_test(tmp_path, "test/a_test.dart")

    stdout = _json_stream(
        _start(),
        _event("allSuites", 1, count=2),
        _suite("test/z_test.dart", suite_id=10, time=2),
        _test_start(
            "z fails",
            test_id=11,
            suite_id=10,
            line=9,
            column=4,
            time=3,
        ),
        _event(
            "error",
            4,
            testID=11,
            error="SECRET failure detail",
            stackTrace="SECRET stack trace",
            isFailure=True,
        ),
        _event(
            "error",
            5,
            testID=11,
            error="another failure",
            stackTrace="another trace",
            isFailure=True,
        ),
        _test_done("failure", test_id=11, time=6),
        _suite("test/a_test.dart", suite_id=20, time=7),
        _test_start(
            "a errors",
            test_id=21,
            suite_id=20,
            line=3,
            column=2,
            time=8,
        ),
        _event(
            "error",
            9,
            testID=21,
            error="SECRET runtime error",
            stackTrace="SECRET runtime trace",
            isFailure=False,
        ),
        _test_done("error", test_id=21, time=10),
        _event("done", 11, success=False),
    )

    monkeypatch.setattr(
        dart_test_adapter,
        "run_process",
        lambda *args, **kwargs: _completed(returncode=1, stdout=stdout),
    )

    result = adapters.evaluate_verification_adapter(
        "dart.test",
        tmp_path,
    )

    assert result.verification_status == "FAIL"
    assert result.exit_code == 1
    assert result.findings == (
        adapters.VerificationFinding(
            path="test/a_test.dart",
            line=3,
            column=2,
            severity="ERROR",
            code="dart_test.error",
            message="Dart test error: a errors",
        ),
        adapters.VerificationFinding(
            path="test/z_test.dart",
            line=9,
            column=4,
            severity="ERROR",
            code="dart_test.failure",
            message="Dart test failure: z fails",
        ),
    )
    assert "SECRET" not in repr(result.findings)


def test_large_raw_error_detail_is_hashed_but_not_copied_into_evidence(
    tmp_path,
    monkeypatch,
):
    _write_test(tmp_path)
    large_detail = "sensitive detail " * 1000
    stdout = _json_stream(
        _start(),
        _event("allSuites", 1, count=1),
        _suite("test/example_test.dart"),
        _test_start("fails"),
        _event(
            "error",
            3,
            testID=1,
            error=large_detail,
            stackTrace=large_detail,
            isFailure=True,
        ),
        _test_done("failure", time=4),
        _event("done", 5, success=False),
    )
    monkeypatch.setattr(
        dart_test_adapter,
        "run_process",
        lambda *args, **kwargs: _completed(returncode=1, stdout=stdout),
    )

    result = adapters.evaluate_verification_adapter("dart.test", tmp_path)

    assert result.verification_status == "FAIL"
    assert large_detail not in repr(result.findings)
    assert result.stdout_sha256 == hashlib.sha256(
        stdout.encode("utf-8")
    ).hexdigest()


def test_failure_uses_root_source_location_when_reporter_provides_it(
    tmp_path,
    monkeypatch,
):
    _write_test(tmp_path)
    start = _test_start("generated failure", line=90, column=8)
    start["test"].update(
        {
            "root_line": 12,
            "root_column": 5,
            "root_url": (tmp_path / "test/example_test.dart").as_uri(),
        }
    )
    stdout = _json_stream(
        _start(),
        _event("allSuites", 1, count=1),
        _suite("test/example_test.dart"),
        start,
        _event("error", testID=1, error="failure", stackTrace="trace", isFailure=True),
        _test_done("failure"),
        _event("done", 4, success=False),
    )
    monkeypatch.setattr(
        dart_test_adapter,
        "run_process",
        lambda *args, **kwargs: _completed(returncode=1, stdout=stdout),
    )

    result = adapters.evaluate_verification_adapter("dart.test", tmp_path)

    assert result.findings[0].line == 12
    assert result.findings[0].column == 5


def test_project_wide_run_with_no_visible_tests_is_not_applicable(
    tmp_path,
    monkeypatch,
):
    stdout = _json_stream(
        _start(),
        _event("allSuites", 1, count=0),
        _event("done", 2, success=True),
    )
    monkeypatch.setattr(
        dart_test_adapter,
        "run_process",
        lambda *args, **kwargs: _completed(returncode=0, stdout=stdout),
    )

    result = adapters.evaluate_verification_adapter("dart.test", tmp_path)

    assert result.evaluation_mode == "project_wide"
    assert result.verification_status == "NOT_APPLICABLE"
    assert result.findings == ()


def test_suite_path_outside_project_fails_closed(tmp_path, monkeypatch):
    outside = tmp_path.parent / "outside_test.dart"
    outside.write_text("void main() {}\n", encoding="utf-8")
    stdout = _json_stream(
        _start(),
        _event("allSuites", 1, count=1),
        _suite(str(outside.resolve())),
        _test_start("outside"),
        _test_done(),
        _event("done", 4, success=True),
    )
    monkeypatch.setattr(
        dart_test_adapter,
        "run_process",
        lambda *args, **kwargs: _completed(returncode=0, stdout=stdout),
    )

    with pytest.raises(adapters.VerificationAdapterError):
        adapters.evaluate_verification_adapter("dart.test", tmp_path)


def test_same_normalized_run_has_deterministic_result_hash(tmp_path, monkeypatch):
    _write_test(tmp_path)
    stdout = _passing_output()
    monkeypatch.setattr(
        dart_test_adapter,
        "run_process",
        lambda *args, **kwargs: _completed(returncode=0, stdout=stdout),
    )

    first = adapters.evaluate_verification_adapter("dart.test", tmp_path)
    second = adapters.evaluate_verification_adapter("dart.test", tmp_path)

    assert first == second
    assert first.result_sha256 == adapters.verification_result_sha256(first)


@pytest.mark.parametrize("mutation", ["name", "count", "skipped", "hidden", "suite", "platform"])
def test_semantic_hash_binds_inventory_and_outcomes(tmp_path, monkeypatch, mutation):
    _write_test(tmp_path)
    _write_test(tmp_path, "test/other_test.dart")
    baseline = [json.loads(line) for line in _passing_output().splitlines()]
    changed = json.loads(json.dumps(baseline))
    if mutation == "name":
        changed[3]["test"]["name"] = "different test"
    elif mutation == "count":
        changed.insert(-1, _test_start("passes", test_id=2))
        changed.insert(-1, _test_done(test_id=2))
    elif mutation in {"skipped", "hidden"}:
        changed[4][mutation] = True
    elif mutation == "suite":
        changed[2]["suite"]["path"] = "test/other_test.dart"
    else:
        changed[2]["suite"]["platform"] = "chrome"
    streams = iter([_json_stream(*baseline), _json_stream(*changed)])
    monkeypatch.setattr(
        dart_test_adapter, "run_process",
        lambda *args, **kwargs: _completed(returncode=0, stdout=next(streams)),
    )
    first = adapters.evaluate_verification_adapter("dart.test", tmp_path)
    second = adapters.evaluate_verification_adapter("dart.test", tmp_path)
    assert first.semantic_sha256 != second.semantic_sha256
    assert first.result_sha256 != second.result_sha256


def test_reporter_ids_are_not_semantic_test_identities(tmp_path, monkeypatch):
    _write_test(tmp_path)
    baseline = [json.loads(line) for line in _passing_output().splitlines()]
    changed = json.loads(json.dumps(baseline))
    changed[2]["suite"]["id"] = 40
    changed[3]["test"]["suiteID"] = 40
    changed[3]["test"]["id"] = 41
    changed[4]["testID"] = 41
    streams = iter([_json_stream(*baseline), _json_stream(*changed)])
    monkeypatch.setattr(
        dart_test_adapter, "run_process",
        lambda *args, **kwargs: _completed(returncode=0, stdout=next(streams)),
    )
    first = adapters.evaluate_verification_adapter("dart.test", tmp_path)
    second = adapters.evaluate_verification_adapter("dart.test", tmp_path)
    assert first.stdout_sha256 != second.stdout_sha256
    assert first.semantic_sha256 == second.semantic_sha256
    assert first.result_sha256 == second.result_sha256


def test_stderr_is_execution_provenance_and_does_not_change_semantic_binding(
    tmp_path, monkeypatch,
):
    _write_test(tmp_path)
    stderr = iter(["first runner warning", "same run with different diagnostics"])
    monkeypatch.setattr(
        dart_test_adapter, "run_process",
        lambda *args, **kwargs: _completed(
            returncode=0, stdout=_passing_output(), stderr=next(stderr),
        ),
    )
    first = adapters.evaluate_verification_adapter("dart.test", tmp_path)
    second = adapters.evaluate_verification_adapter("dart.test", tmp_path)
    assert first.stderr_sha256 != second.stderr_sha256
    assert first.result_sha256 == second.result_sha256


@pytest.mark.parametrize("semantic_hash", [None, "bad", [], 3])
def test_semantic_contract_cannot_be_downgraded_or_malformed(
    tmp_path, monkeypatch, semantic_hash,
):
    from dataclasses import replace

    _write_test(tmp_path)
    monkeypatch.setattr(
        dart_test_adapter, "run_process",
        lambda *args, **kwargs: _completed(returncode=0, stdout=_passing_output()),
    )
    actual = adapters.evaluate_verification_adapter("dart.test", tmp_path)
    tampered = replace(actual, semantic_sha256=semantic_hash)
    with pytest.raises(adapters.VerificationAdapterError):
        adapters.validate_verification_adapter_result(tampered, expected_adapter_id="dart.test")


def test_semantic_evidence_rejects_tampered_digest_or_provenance_field_injection(
    tmp_path, monkeypatch,
):
    from project_system import rule_checkers
    from project_system.rule_engine import RuleEvaluationContext

    _write_test(tmp_path)
    monkeypatch.setattr(
        dart_test_adapter, "run_process",
        lambda *args, **kwargs: _completed(returncode=0, stdout=_passing_output()),
    )
    outcome = rule_checkers.evaluate_checker(
        "code.verification", RuleEvaluationContext(tmp_path, "sync_verify", {}, True),
        {"adapter": "dart.test"},
    )
    details = outcome.details
    assert "semantic_sha256" in details
    assert "stdout_sha256" not in details and "stderr_sha256" not in details
    for tampered in [
        {**details, "semantic_sha256": "a" * 64},
        {**details, "stdout_sha256": "b" * 64},
        {key: value for key, value in details.items() if key != "semantic_sha256"},
    ]:
        assert rule_checkers.checker_outcome_contract_messages(
            "code.verification", "PASS", tampered, None, {"adapter": "dart.test"},
        )


def _interleaved_pass_output(*, alternate=False):
    first_start = _test_start("first", test_id=11, suite_id=10)
    second_start = _test_start("second", test_id=21, suite_id=20)
    first_done = _test_done(test_id=11)
    second_done = _test_done(test_id=21)
    if alternate:
        events = [
            _start(),
            _suite("test/second_test.dart", suite_id=20),
            second_start,
            _suite("test/first_test.dart", suite_id=10),
            first_start,
            first_done,
            _event("allSuites", count=2),
            second_done,
            _event("done", success=True),
        ]
        events[0]["pid"] = 9876
    else:
        events = [
            _start(),
            _event("allSuites", count=2),
            _suite("test/first_test.dart", suite_id=10),
            first_start,
            _suite("test/second_test.dart", suite_id=20),
            second_start,
            second_done,
            first_done,
            _event("done", success=True),
        ]
    for index, event in enumerate(events):
        event["time"] = index * (100 if alternate else 5)
    return _json_stream(*events)


def test_reporter_volatility_does_not_change_semantic_result_or_rule_evidence(
    tmp_path, monkeypatch,
):
    from project_system.rule_engine import RuleEvaluationContext, evaluate_rules
    from project_system.rule_evidence import build_rule_evidence

    _write_test(tmp_path, "test/first_test.dart")
    _write_test(tmp_path, "test/second_test.dart")
    streams = [_interleaved_pass_output(), _interleaved_pass_output(alternate=True)]
    outcomes = iter(streams)
    monkeypatch.setattr(
        dart_test_adapter, "run_process",
        lambda *args, **kwargs: _completed(returncode=0, stdout=next(outcomes)),
    )
    results = [adapters.evaluate_verification_adapter("dart.test", tmp_path)
               for _ in range(2)]
    assert results[0].stdout_sha256 != results[1].stdout_sha256
    assert results[0].result_sha256 == results[1].result_sha256

    registry = {
        "schema_version": 1, "profile": "project-system-rules-v1",
        "rules": {"TEST-001": {
            "title": "Test suite", "status": "active", "category": "testing",
            "description": "Run the governed test suite.",
            "verification": {"method": "deterministic", "checker": "code.verification",
                             "parameters": {"adapter": "dart.test"}},
            "enforcement": {"severity": "ERROR", "checkpoints": ["sync_verify"]},
            "exception_policy": "forbidden",
        }},
    }
    context = RuleEvaluationContext(tmp_path, "sync_verify", {}, True)
    outcomes = iter(streams)
    evidence = [build_rule_evidence(
        project_id="demo", git_head="1" * 40, base_commit="1" * 40,
        cli_version="0.12.1", rules_registry=registry,
        exception_registry={"schema_version": 1,
                            "profile": "project-system-rule-exceptions-v1", "exceptions": {}},
        context=context, results=evaluate_rules(registry, context),
    ) for _ in range(2)]
    assert evidence[0].evidence_fingerprint == evidence[1].evidence_fingerprint


@pytest.mark.parametrize(
    ("completions", "expected"),
    [([{"skipped": True}], "NOT_APPLICABLE"),
     ([{"hidden": True}], "NOT_APPLICABLE"),
     ([{"skipped": True}, {}], "PASS")],
)
def test_skipped_and_hidden_tests_do_not_establish_executed_pass(
    tmp_path, monkeypatch, completions, expected,
):
    _write_test(tmp_path)
    events = [_start(), _event("allSuites", count=1), _suite("test/example_test.dart")]
    for index, completion in enumerate(completions, start=1):
        events.extend([_test_start(f"test {index}", test_id=index),
                       _test_done(test_id=index, **completion)])
    events.append(_event("done", success=True))
    monkeypatch.setattr(
        dart_test_adapter, "run_process",
        lambda *args, **kwargs: _completed(returncode=0, stdout=_json_stream(*events)),
    )
    assert adapters.evaluate_verification_adapter(
        "dart.test", tmp_path,
    ).verification_status == expected


@pytest.mark.parametrize(
    ("result", "error_flags", "late", "expected"),
    [("failure", [], False, "ERROR"),
     ("error", [], False, "ERROR"),
     ("failure", [False], False, "ERROR"),
     ("success", [True], False, "ERROR"),
     ("success", [True], True, "FAIL"),
     ("failure", [True, True], False, "FAIL"),
     ("error", [True, False], False, "FAIL")],
)
def test_failure_lifecycle_requires_consistent_ordered_error_evidence(
    tmp_path, monkeypatch, result, error_flags, late, expected,
):
    from project_system.rule_checkers import evaluate_checker
    from project_system.rule_engine import RuleEvaluationContext

    _write_test(tmp_path)
    errors = [_event("error", testID=1, error="detail", stackTrace="trace", isFailure=flag)
              for flag in error_flags]
    completion = _test_done(result)
    events = [_start(), _event("allSuites", count=1),
              _suite("test/example_test.dart"), _test_start("case")]
    events.extend([completion, *errors] if late else [*errors, completion])
    events.append(_event("done", success=False))
    monkeypatch.setattr(
        dart_test_adapter, "run_process",
        lambda *args, **kwargs: _completed(returncode=1, stdout=_json_stream(*events)),
    )
    outcome = evaluate_checker(
        "code.verification", RuleEvaluationContext(tmp_path, "sync_verify", {}, True),
        {"adapter": "dart.test"},
    )
    assert outcome.raw_status == expected


@pytest.mark.parametrize(
    "stdout",
    [
        "not json\n",
        _json_stream(_start(), _event("done", 1, success=None)),
        _json_stream(_start(), _event("done", 1, success=True))
        + "{\"type\":\"print\",\"time\":2}\n",
        (
            '{"type":"start","time":0,"protocolVersion":"0.1.1",'
            '"protocolVersion":"0.1.1","runnerVersion":"1.31.0"}\n'
        ),
        _json_stream(
            {
                **_start(),
                "protocolVersion": "0.2.0",
            }
        ),
    ],
)
def test_malformed_structured_output_fails_closed(tmp_path, monkeypatch, stdout):
    monkeypatch.setattr(
        dart_test_adapter,
        "run_process",
        lambda *args, **kwargs: _completed(returncode=0, stdout=stdout),
    )

    with pytest.raises(adapters.VerificationAdapterError):
        adapters.evaluate_verification_adapter("dart.test", tmp_path)


@pytest.mark.parametrize("returncode", [2, 64, 70, 79, 254, 255])
def test_unsupported_exit_code_is_infrastructure_error(
    tmp_path,
    monkeypatch,
    returncode,
):
    _write_test(tmp_path)
    stdout = _passing_output()
    monkeypatch.setattr(
        dart_test_adapter,
        "run_process",
        lambda *args, **kwargs: _completed(
            returncode=returncode,
            stdout=stdout,
            stderr="SECRET infrastructure detail",
        ),
    )

    with pytest.raises(adapters.VerificationAdapterError) as captured:
        adapters.evaluate_verification_adapter("dart.test", tmp_path)

    assert "SECRET" not in str(captured.value)


@pytest.mark.parametrize(
    "failure",
    [
        FileNotFoundError(r"C:\secret\dart.exe was not found"),
        subprocess.TimeoutExpired(
            cmd=r"C:\secret\dart.exe test",
            timeout=300,
            output="SECRET STDOUT",
            stderr="SECRET STDERR",
        ),
    ],
)
def test_execution_failure_is_sanitized(tmp_path, monkeypatch, failure):
    def fail(*args, **kwargs):
        raise failure

    monkeypatch.setattr(dart_test_adapter, "run_process", fail)

    with pytest.raises(adapters.VerificationAdapterError) as captured:
        adapters.evaluate_verification_adapter("dart.test", tmp_path)

    message = str(captured.value)
    assert "dart.test" in message
    assert "SECRET" not in message
    assert "C:\\secret" not in message


def test_output_overflow_is_sanitized(tmp_path, monkeypatch):
    from project_system.process_runner import ProcessOutputLimitExceeded

    def fail(*args, **kwargs):
        raise ProcessOutputLimitExceeded("stdout", 16 * 1024 * 1024)

    monkeypatch.setattr(dart_test_adapter, "run_process", fail)

    with pytest.raises(adapters.VerificationAdapterError) as captured:
        adapters.evaluate_verification_adapter("dart.test", tmp_path)

    message = str(captured.value)
    assert "stdout" not in message
    assert "max_capture_bytes" not in message


@pytest.mark.parametrize(
    ("returncode", "success", "test_result"),
    [
        (0, False, "failure"),
        (1, True, "success"),
        (1, False, "success"),
        (0, True, "failure"),
    ],
)
def test_exit_code_must_agree_with_structured_result(
    tmp_path,
    monkeypatch,
    returncode,
    success,
    test_result,
):
    _write_test(tmp_path)
    stdout = _json_stream(
        _start(),
        _event("allSuites", 1, count=1),
        _suite("test/example_test.dart"),
        _test_start("case"),
        _test_done(test_result),
        _event("done", 4, success=success),
    )
    monkeypatch.setattr(
        dart_test_adapter,
        "run_process",
        lambda *args, **kwargs: _completed(
            returncode=returncode,
            stdout=stdout,
        ),
    )

    with pytest.raises(adapters.VerificationAdapterError):
        adapters.evaluate_verification_adapter("dart.test", tmp_path)
