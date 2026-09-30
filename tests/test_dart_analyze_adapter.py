from pathlib import Path
import subprocess

import pytest

from project_system import verification_adapters as adapters
from project_system import dart_analyze_adapter


def test_dart_analyze_adapter_is_allowlisted():
    spec = adapters.VERIFICATION_ADAPTER_REGISTRY["dart.analyze"]

    assert spec.adapter_id == "dart.analyze"
    assert spec.version == "1"
    assert spec.implementation is dart_analyze_adapter.run_dart_analyze
    assert spec.executes_project_code is False


def test_dart_analyze_adapter_uses_complete_invalidation_for_semantic_inputs():
    spec = adapters.VERIFICATION_ADAPTER_REGISTRY["dart.analyze"]

    assert spec.global_input_patterns == (
        "**/*.dart",
        "**/*.yaml",
        "**/pubspec.lock",
    )


def test_dart_change_forces_project_wide_invalidation():
    paths, mode = adapters.resolve_verification_adapter_evaluation(
        "dart.analyze",
        ("lib/main.dart",),
    )

    assert paths is None
    assert mode == "project_wide_invalidation"


def test_unrelated_change_remains_bounded():
    paths, mode = adapters.resolve_verification_adapter_evaluation(
        "dart.analyze",
        ("README.md",),
    )

    assert paths == ("README.md",)
    assert mode == "bounded"


def test_dart_analyze_uses_fixed_safe_process_invocation(tmp_path, monkeypatch):
    calls = []

    def fake_run_process(arguments, **kwargs):
        calls.append((tuple(arguments), dict(kwargs)))

        if tuple(arguments) == ("dart", "--version"):
            return type(
                "Completed",
                (),
                {
                    "returncode": 0,
                    "stdout": "",
                    "stderr": (
                        "Dart SDK version: 3.13.1 (stable) "
                        "(Tue Aug 18 12:00:00 2026 +0000) on \"windows_x64\""
                    ),
                },
            )()

        if tuple(arguments) == (
            "dart",
            "analyze",
            "--format=machine",
            "--no-plugins",
            ".",
        ):
            return type(
                "Completed",
                (),
                {
                    "returncode": 0,
                    "stdout": "",
                    "stderr": "",
                },
            )()

        raise AssertionError(f"unexpected command: {arguments!r}")

    monkeypatch.setattr(
        dart_analyze_adapter,
        "run_process",
        fake_run_process,
        raising=False,
    )

    actual = adapters.evaluate_verification_adapter(
        "dart.analyze",
        tmp_path,
        evaluation_paths=("lib/main.dart",),
    )

    root = Path(tmp_path).resolve()

    assert calls == [
        (
            ("dart", "--version"),
            {
                "cwd": root,
                "capture_output": True,
                "text": True,
                "encoding": "utf-8",
                "check": False,
                "timeout": 10,
                "max_capture_bytes": 64 * 1024,
            },
        ),
        (
            (
                "dart",
                "analyze",
                "--format=machine",
                "--no-plugins",
                ".",
            ),
            {
                "cwd": root,
                "capture_output": True,
                "text": True,
                "encoding": "utf-8",
                "check": False,
                "timeout": 120,
                "max_capture_bytes": 16 * 1024 * 1024,
            },
        ),
    ]

    assert actual.adapter_id == "dart.analyze"
    assert actual.adapter_version == "1"
    assert actual.tool_name == "dart"
    assert actual.tool_version == "3.13.1"
    assert actual.verification_status == "PASS"
    assert actual.evaluation_mode == "project_wide_invalidation"
    assert actual.inspected_paths == ()
    assert actual.findings == ()
    assert actual.exit_code == 0

    adapters.validate_verification_adapter_result(
        actual,
        expected_adapter_id="dart.analyze",
        expected_evaluation_mode="project_wide_invalidation",
    )


def test_dart_analyze_missing_executable_fails_closed_without_os_detail(
    tmp_path,
    monkeypatch,
):
    calls = []

    def fake_run_process(arguments, **kwargs):
        calls.append((tuple(arguments), dict(kwargs)))
        raise FileNotFoundError(
            "C:\\secret\\tooling\\dart.exe was not found"
        )

    monkeypatch.setattr(
        dart_analyze_adapter,
        "run_process",
        fake_run_process,
        raising=False,
    )

    with pytest.raises(adapters.VerificationAdapterError) as captured:
        adapters.evaluate_verification_adapter(
            "dart.analyze",
            tmp_path,
            evaluation_paths=("lib/main.dart",),
        )

    message = str(captured.value)

    assert calls
    assert calls[0][0] == ("dart", "--version")
    assert "dart.analyze" in message
    assert "C:\\secret" not in message
    assert "dart.exe was not found" not in message


def test_unrelated_bounded_change_does_not_execute_dart(tmp_path, monkeypatch):
    calls = []

    def fake_run_process(arguments, **kwargs):
        calls.append((tuple(arguments), dict(kwargs)))
        raise AssertionError("Dart must not run for unrelated bounded changes")

    monkeypatch.setattr(
        dart_analyze_adapter,
        "run_process",
        fake_run_process,
    )

    actual = adapters.evaluate_verification_adapter(
        "dart.analyze",
        tmp_path,
        evaluation_paths=("README.md",),
    )

    assert calls == []
    assert actual.adapter_id == "dart.analyze"
    assert actual.adapter_version == "1"
    assert actual.tool_name == "dart"
    assert actual.tool_version == "not_executed"
    assert actual.verification_status == "NOT_APPLICABLE"
    assert actual.evaluation_mode == "bounded"
    assert actual.inspected_paths == ()
    assert actual.findings == ()
    assert actual.exit_code == 0

    adapters.validate_verification_adapter_result(
        actual,
        expected_adapter_id="dart.analyze",
        bounded_evaluation_paths=("README.md",),
        expected_evaluation_mode="bounded",
    )


def test_dart_analyze_parses_machine_error_into_canonical_finding(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "lib" / "main.dart"
    source.parent.mkdir(parents=True)
    source.write_text("void main() {}\n", encoding="utf-8")

    machine_path = str(source.resolve()).replace("\\", "/")

    def fake_run_process(arguments, **kwargs):
        if tuple(arguments) == ("dart", "--version"):
            return type(
                "Completed",
                (),
                {
                    "returncode": 0,
                    "stdout": "",
                    "stderr": (
                        "Dart SDK version: 3.13.1 (stable) "
                        '(Tue Aug 18 12:00:00 2026 +0000) on "windows_x64"'
                    ),
                },
            )()

        if tuple(arguments) == (
            "dart",
            "analyze",
            "--format=machine",
            "--no-plugins",
            ".",
        ):
            return type(
                "Completed",
                (),
                {
                    "returncode": 3,
                    "stdout": (
                        "ERROR|COMPILE_TIME_ERROR|UNDEFINED_IDENTIFIER|"
                        f"{machine_path}|5|7|3|Undefined name 'foo'.\n"
                    ),
                    "stderr": "",
                },
            )()

        raise AssertionError(f"unexpected command: {arguments!r}")

    monkeypatch.setattr(
        dart_analyze_adapter,
        "run_process",
        fake_run_process,
    )

    actual = adapters.evaluate_verification_adapter(
        "dart.analyze",
        tmp_path,
        evaluation_paths=("lib/main.dart",),
    )

    assert actual.exit_code == 3
    assert actual.verification_status == "FAIL"
    assert actual.inspected_paths == ()
    assert actual.findings == (
        adapters.VerificationFinding(
            path="lib/main.dart",
            line=5,
            column=7,
            severity="ERROR",
            code="UNDEFINED_IDENTIFIER",
            message="Undefined name 'foo'.",
        ),
    )

    adapters.validate_verification_adapter_result(
        actual,
        expected_adapter_id="dart.analyze",
        expected_evaluation_mode="project_wide_invalidation",
    )


def test_machine_parser_unescapes_delimiter_backslash_newline_and_carriage_return(
    tmp_path,
):
    source = tmp_path / "lib" / "main.dart"
    source.parent.mkdir(parents=True)
    source.write_text("void main() {}\n", encoding="utf-8")

    # Dart machine format escapes backslash itself as \\.
    machine_path = str(source.resolve()).replace("\\", "\\\\")

    escaped_message = r"Use a\|b\\c\nnext\rend"
    stdout = (
        "WARNING|STATIC_WARNING|FAKE_CODE|"
        f"{machine_path}|2|4|1|{escaped_message}\n"
    )

    findings = dart_analyze_adapter._parse_machine_output(
        tmp_path,
        stdout,
    )

    assert findings == (
        adapters.VerificationFinding(
            path="lib/main.dart",
            line=2,
            column=4,
            severity="WARNING",
            code="FAKE_CODE",
            message="Use a|b\\c\nnext\rend",
        ),
    )


def test_machine_parser_rejects_unknown_escape_sequence(tmp_path):
    source = tmp_path / "lib" / "main.dart"
    source.parent.mkdir(parents=True)
    source.write_text("void main() {}\n", encoding="utf-8")

    machine_path = str(source.resolve()).replace("\\", "\\\\")

    stdout = (
        "INFO|HINT|FAKE_CODE|"
        f"{machine_path}|1|1|1|Unknown \\q escape\n"
    )

    with pytest.raises(RuntimeError, match="escape"):
        dart_analyze_adapter._parse_machine_output(
            tmp_path,
            stdout,
        )


@pytest.mark.parametrize(
    "stdout",
    [
        "ERROR|TYPE|CODE|C:/project/lib/a.dart|1|1|1\n",
        "ERROR|TYPE|CODE|C:/project/lib/a.dart|1|1|1|message|extra\n",
    ],
)
def test_machine_parser_rejects_wrong_field_count(tmp_path, stdout):
    with pytest.raises(RuntimeError, match="Malformed"):
        dart_analyze_adapter._parse_machine_output(
            tmp_path,
            stdout,
        )


def test_machine_parser_rejects_diagnostic_path_outside_project(tmp_path):
    outside = tmp_path.parent / "outside.dart"
    outside.write_text("void outside() {}\n", encoding="utf-8")

    machine_path = str(outside.resolve()).replace("\\", "/")
    stdout = (
        "ERROR|TYPE|CODE|"
        f"{machine_path}|1|1|1|Outside project.\n"
    )

    with pytest.raises(RuntimeError, match="outside the project root"):
        dart_analyze_adapter._parse_machine_output(
            tmp_path,
            stdout,
        )


def test_dart_analyze_exit_four_is_infrastructure_failure(
    tmp_path,
    monkeypatch,
):
    calls = []

    def fake_run_process(arguments, **kwargs):
        calls.append(tuple(arguments))

        if tuple(arguments) == ("dart", "--version"):
            return type(
                "Completed",
                (),
                {
                    "returncode": 0,
                    "stdout": "",
                    "stderr": "Dart SDK version: 3.13.1 (stable)",
                },
            )()

        if tuple(arguments) == (
            "dart",
            "analyze",
            "--format=machine",
            "--no-plugins",
            ".",
        ):
            return type(
                "Completed",
                (),
                {
                    "returncode": 4,
                    "stdout": "",
                    "stderr": "SECRET ANALYSIS SERVER CRASH DETAIL",
                },
            )()

        raise AssertionError(f"unexpected command: {arguments!r}")

    monkeypatch.setattr(
        dart_analyze_adapter,
        "run_process",
        fake_run_process,
    )

    with pytest.raises(adapters.VerificationAdapterError) as captured:
        adapters.evaluate_verification_adapter(
            "dart.analyze",
            tmp_path,
            evaluation_paths=("lib/main.dart",),
        )

    assert calls == [
        ("dart", "--version"),
        (
            "dart",
            "analyze",
            "--format=machine",
            "--no-plugins",
            ".",
        ),
    ]
    assert "SECRET ANALYSIS SERVER CRASH DETAIL" not in str(captured.value)


def _evaluate_dart_machine_result(
    tmp_path,
    monkeypatch,
    *,
    exit_code,
    stdout,
):
    def fake_run_process(arguments, **kwargs):
        if tuple(arguments) == ("dart", "--version"):
            return type(
                "Completed",
                (),
                {
                    "returncode": 0,
                    "stdout": "",
                    "stderr": "Dart SDK version: 3.13.1 (stable)",
                },
            )()

        if tuple(arguments) == (
            "dart",
            "analyze",
            "--format=machine",
            "--no-plugins",
            ".",
        ):
            return type(
                "Completed",
                (),
                {
                    "returncode": exit_code,
                    "stdout": stdout,
                    "stderr": "",
                },
            )()

        raise AssertionError(f"unexpected command: {arguments!r}")

    monkeypatch.setattr(
        dart_analyze_adapter,
        "run_process",
        fake_run_process,
    )

    return adapters.evaluate_verification_adapter(
        "dart.analyze",
        tmp_path,
        evaluation_paths=("lib/main.dart",),
    )


def test_dart_analyze_rejects_error_finding_with_success_exit(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "lib" / "main.dart"
    source.parent.mkdir(parents=True)
    source.write_text("void main() {}\n", encoding="utf-8")
    machine_path = str(source.resolve()).replace("\\", "/")

    stdout = (
        "ERROR|COMPILE_TIME_ERROR|FAKE_ERROR|"
        f"{machine_path}|1|1|1|Error diagnostic.\n"
    )

    with pytest.raises(adapters.VerificationAdapterError):
        _evaluate_dart_machine_result(
            tmp_path,
            monkeypatch,
            exit_code=0,
            stdout=stdout,
        )


def test_dart_analyze_rejects_warning_only_output_with_error_exit(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "lib" / "main.dart"
    source.parent.mkdir(parents=True)
    source.write_text("void main() {}\n", encoding="utf-8")
    machine_path = str(source.resolve()).replace("\\", "/")

    stdout = (
        "WARNING|STATIC_WARNING|FAKE_WARNING|"
        f"{machine_path}|1|1|1|Warning diagnostic.\n"
    )

    with pytest.raises(adapters.VerificationAdapterError):
        _evaluate_dart_machine_result(
            tmp_path,
            monkeypatch,
            exit_code=3,
            stdout=stdout,
        )


def test_dart_analyze_rejects_nonzero_exit_without_matching_findings(
    tmp_path,
    monkeypatch,
):
    with pytest.raises(adapters.VerificationAdapterError):
        _evaluate_dart_machine_result(
            tmp_path,
            monkeypatch,
            exit_code=2,
            stdout="",
        )


def test_dart_analyze_rejects_info_exit_one_without_fatal_infos(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "lib" / "main.dart"
    source.parent.mkdir(parents=True)
    source.write_text("void main() {}\n", encoding="utf-8")
    machine_path = str(source.resolve()).replace("\\", "/")

    stdout = (
        "INFO|HINT|FAKE_INFO|"
        f"{machine_path}|1|1|1|Info diagnostic.\n"
    )

    with pytest.raises(adapters.VerificationAdapterError):
        _evaluate_dart_machine_result(
            tmp_path,
            monkeypatch,
            exit_code=1,
            stdout=stdout,
        )


def test_dart_version_timeout_fails_closed_without_leaking_detail(
    tmp_path,
    monkeypatch,
):
    def fake_run_process(arguments, **kwargs):
        raise subprocess.TimeoutExpired(
            cmd="C:\\secret\\dart.exe --version",
            timeout=10,
            output="SECRET_STDOUT",
            stderr="SECRET_STDERR",
        )

    monkeypatch.setattr(
        dart_analyze_adapter,
        "run_process",
        fake_run_process,
    )

    with pytest.raises(adapters.VerificationAdapterError) as captured:
        adapters.evaluate_verification_adapter(
            "dart.analyze",
            tmp_path,
            evaluation_paths=("lib/main.dart",),
        )

    message = str(captured.value)
    assert "dart.analyze" in message
    assert "SECRET_STDOUT" not in message
    assert "SECRET_STDERR" not in message
    assert "C:\\secret" not in message


def test_dart_version_nonzero_exit_fails_closed(tmp_path, monkeypatch):
    def fake_run_process(arguments, **kwargs):
        assert tuple(arguments) == ("dart", "--version")
        return type(
            "Completed",
            (),
            {
                "returncode": 1,
                "stdout": "SECRET_STDOUT",
                "stderr": "SECRET_STDERR",
            },
        )()

    monkeypatch.setattr(
        dart_analyze_adapter,
        "run_process",
        fake_run_process,
    )

    with pytest.raises(adapters.VerificationAdapterError) as captured:
        adapters.evaluate_verification_adapter(
            "dart.analyze",
            tmp_path,
            evaluation_paths=("lib/main.dart",),
        )

    message = str(captured.value)
    assert "SECRET_STDOUT" not in message
    assert "SECRET_STDERR" not in message


def test_dart_version_malformed_output_fails_closed(tmp_path, monkeypatch):
    def fake_run_process(arguments, **kwargs):
        assert tuple(arguments) == ("dart", "--version")
        return type(
            "Completed",
            (),
            {
                "returncode": 0,
                "stdout": "not a Dart SDK version",
                "stderr": "",
            },
        )()

    monkeypatch.setattr(
        dart_analyze_adapter,
        "run_process",
        fake_run_process,
    )

    with pytest.raises(adapters.VerificationAdapterError):
        adapters.evaluate_verification_adapter(
            "dart.analyze",
            tmp_path,
            evaluation_paths=("lib/main.dart",),
        )


def test_dart_version_non_text_output_fails_closed(tmp_path, monkeypatch):
    def fake_run_process(arguments, **kwargs):
        assert tuple(arguments) == ("dart", "--version")
        return type(
            "Completed",
            (),
            {
                "returncode": 0,
                "stdout": b"Dart SDK version: 3.13.1",
                "stderr": b"",
            },
        )()

    monkeypatch.setattr(
        dart_analyze_adapter,
        "run_process",
        fake_run_process,
    )

    with pytest.raises(adapters.VerificationAdapterError):
        adapters.evaluate_verification_adapter(
            "dart.analyze",
            tmp_path,
            evaluation_paths=("lib/main.dart",),
        )


def test_dart_analyze_timeout_fails_closed_without_leaking_detail(
    tmp_path,
    monkeypatch,
):
    calls = []

    def fake_run_process(arguments, **kwargs):
        calls.append(tuple(arguments))

        if tuple(arguments) == ("dart", "--version"):
            return type(
                "Completed",
                (),
                {
                    "returncode": 0,
                    "stdout": "",
                    "stderr": "Dart SDK version: 3.13.1 (stable)",
                },
            )()

        raise subprocess.TimeoutExpired(
            cmd="C:\\secret\\dart.exe analyze",
            timeout=120,
            output="SECRET_ANALYZER_STDOUT",
            stderr="SECRET_ANALYZER_STDERR",
        )

    monkeypatch.setattr(
        dart_analyze_adapter,
        "run_process",
        fake_run_process,
    )

    with pytest.raises(adapters.VerificationAdapterError) as captured:
        adapters.evaluate_verification_adapter(
            "dart.analyze",
            tmp_path,
            evaluation_paths=("lib/main.dart",),
        )

    assert calls == [
        ("dart", "--version"),
        (
            "dart",
            "analyze",
            "--format=machine",
            "--no-plugins",
            ".",
        ),
    ]

    message = str(captured.value)
    assert "SECRET_ANALYZER_STDOUT" not in message
    assert "SECRET_ANALYZER_STDERR" not in message
    assert "C:\\secret" not in message


def test_dart_analyze_non_text_output_fails_closed(
    tmp_path,
    monkeypatch,
):
    def fake_run_process(arguments, **kwargs):
        if tuple(arguments) == ("dart", "--version"):
            return type(
                "Completed",
                (),
                {
                    "returncode": 0,
                    "stdout": "",
                    "stderr": "Dart SDK version: 3.13.1 (stable)",
                },
            )()

        return type(
            "Completed",
            (),
            {
                "returncode": 0,
                "stdout": b"",
                "stderr": b"",
            },
        )()

    monkeypatch.setattr(
        dart_analyze_adapter,
        "run_process",
        fake_run_process,
    )

    with pytest.raises(adapters.VerificationAdapterError):
        adapters.evaluate_verification_adapter(
            "dart.analyze",
            tmp_path,
            evaluation_paths=("lib/main.dart",),
        )


def test_dart_version_output_limit_failure_is_sanitized(
    tmp_path,
    monkeypatch,
):
    from project_system import process_runner

    calls = []

    def fake_run_process(arguments, **kwargs):
        calls.append((tuple(arguments), dict(kwargs)))
        raise process_runner.ProcessOutputLimitExceeded(
            "stderr",
            64 * 1024,
        )

    monkeypatch.setattr(
        dart_analyze_adapter,
        "run_process",
        fake_run_process,
    )

    with pytest.raises(adapters.VerificationAdapterError) as captured:
        adapters.evaluate_verification_adapter(
            "dart.analyze",
            tmp_path,
            evaluation_paths=("lib/main.dart",),
        )

    assert calls
    assert calls[0][0] == ("dart", "--version")

    message = str(captured.value)
    assert "dart.analyze" in message
    assert "stderr" not in message
    assert "max_capture_bytes" not in message


def test_dart_analyze_output_limit_failure_is_sanitized(
    tmp_path,
    monkeypatch,
):
    from project_system import process_runner

    calls = []

    def fake_run_process(arguments, **kwargs):
        calls.append((tuple(arguments), dict(kwargs)))

        if tuple(arguments) == ("dart", "--version"):
            return type(
                "Completed",
                (),
                {
                    "returncode": 0,
                    "stdout": "",
                    "stderr": "Dart SDK version: 3.13.1 (stable)",
                },
            )()

        raise process_runner.ProcessOutputLimitExceeded(
            "stdout",
            16 * 1024 * 1024,
        )

    monkeypatch.setattr(
        dart_analyze_adapter,
        "run_process",
        fake_run_process,
    )

    with pytest.raises(adapters.VerificationAdapterError) as captured:
        adapters.evaluate_verification_adapter(
            "dart.analyze",
            tmp_path,
            evaluation_paths=("lib/main.dart",),
        )

    assert [call[0] for call in calls] == [
        ("dart", "--version"),
        (
            "dart",
            "analyze",
            "--format=machine",
            "--no-plugins",
            ".",
        ),
    ]

    message = str(captured.value)
    assert "dart.analyze" in message
    assert "stdout" not in message
    assert "max_capture_bytes" not in message


def test_included_analysis_options_change_cannot_be_treated_as_bounded():
    paths, mode = adapters.resolve_verification_adapter_evaluation(
        "dart.analyze",
        ("config/team_options.yaml",),
    )

    assert paths is None
    assert mode == "project_wide_invalidation"
