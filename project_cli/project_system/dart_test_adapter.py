"""Packaged Dart test Verification Adapter.

Every non-empty bounded change runs the complete suite. Empty bounded
evaluations are deterministically not applicable. The adapter never guesses
a test subset from changed paths. Raw transcript hashes are provenance;
semantic_sha256 binds a sorted test inventory and validated outcomes.
"""

from dataclasses import dataclass, replace
import hashlib
import json
from pathlib import Path
import re

from .process_runner import run_process


_DART_TEST_MAX_CAPTURE_BYTES = 16 * 1024 * 1024
_DART_TEST_TIMEOUT_SECONDS = 300
_JSON_PROTOCOL_RE = re.compile(r"^0\.1\.\d+$")
_MAX_NORMALIZED_TEXT_LENGTH = 4096


@dataclass(frozen=True)
class _ParsedDartTestRun:
    runner_version: str
    verification_status: str
    findings: tuple
    semantic_sha256: str


def _sha256_text(value):
    if not isinstance(value, str):
        raise RuntimeError("Dart process output must be text")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_json(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    )


def _semantic_sha256(*, protocol_version, runner_version, suites, tests):
    # Sort lists without deduplicating: repeated test identities still bind
    # their multiplicity. Reporter IDs, PID, timing, event interleaving and
    # raw print/error/stack text are execution provenance, not identity.
    payload = {
        "schema_version": 1,
        "protocol_version": protocol_version,
        "runner_version": runner_version,
        "suites": sorted(suites, key=_canonical_json),
        "tests": sorted(tests, key=_canonical_json),
    }
    return _sha256_text(_canonical_json(payload))


def _mapping(value, label):
    if not isinstance(value, dict):
        raise RuntimeError(f"{label} is malformed")
    return value


def _integer(value, label, *, minimum=0):
    if type(value) is not int or value < minimum:
        raise RuntimeError(f"{label} is malformed")
    return value


def _string(
    value,
    label,
    *,
    nullable=False,
    nonempty=False,
    normalized=False,
):
    if nullable and value is None:
        return None
    if not isinstance(value, str):
        raise RuntimeError(f"{label} is malformed")
    if nonempty and (not value or value != value.strip()):
        raise RuntimeError(f"{label} is malformed")
    if normalized and len(value) > _MAX_NORMALIZED_TEXT_LENGTH:
        raise RuntimeError(f"{label} exceeds the normalized text limit")
    return value


def _boolean(value, label, *, nullable=False):
    if nullable and value is None:
        return None
    if type(value) is not bool:
        raise RuntimeError(f"{label} is malformed")
    return value


def _metadata(value, label):
    value = _mapping(value, label)
    _boolean(value.get("skip"), f"{label}.skip")
    _string(
        value.get("skipReason"),
        f"{label}.skipReason",
        nullable=True,
    )


def _location(value, label):
    line = value.get("line")
    column = value.get("column")
    url = value.get("url")

    if line is None and column is None and url is None:
        return None, None

    _integer(line, f"{label}.line", minimum=1)
    _integer(column, f"{label}.column", minimum=1)
    _string(url, f"{label}.url", nonempty=True)
    return line, column


def _root_location(value, label, fallback):
    names = ("root_line", "root_column", "root_url")
    present = tuple(name in value for name in names)
    if not any(present):
        return fallback
    if not all(present):
        raise RuntimeError(f"{label} root location is incomplete")

    line = _integer(value["root_line"], f"{label}.root_line", minimum=1)
    column = _integer(
        value["root_column"],
        f"{label}.root_column",
        minimum=1,
    )
    _string(value["root_url"], f"{label}.root_url", nonempty=True)
    return line, column


def _canonical_suite_path(project_root, raw_path):
    raw_path = _string(
        raw_path,
        "suite.path",
        nonempty=True,
        normalized=True,
    )
    source = Path(raw_path)
    if not source.is_absolute():
        source = Path(project_root) / source

    try:
        root = Path(project_root).resolve(strict=True)
        source = source.resolve(strict=True)
        relative = source.relative_to(root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise RuntimeError(
            "Dart test suite path cannot be contained in the project root"
        ) from exc

    if not source.is_file() or not relative.parts:
        raise RuntimeError("Dart test suite path is not a project file")

    return relative.as_posix()


def _strict_json_object(raw_line, output_line):
    def object_pairs(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate JSON object key")
            value[key] = item
        return value

    def reject_constant(_value):
        raise ValueError("non-standard JSON numeric constant")

    try:
        value = json.loads(
            raw_line,
            object_pairs_hook=object_pairs,
            parse_constant=reject_constant,
        )
    except (json.JSONDecodeError, ValueError, RecursionError) as exc:
        raise RuntimeError(
            f"Malformed Dart JSON reporter event at output line {output_line}"
        ) from exc

    return _mapping(
        value,
        f"Dart JSON reporter event at output line {output_line}",
    )


def _validate_group_entry(value, label, suites):
    value = _mapping(value, label)
    entry_id = _integer(value.get("id"), f"{label}.id")
    suite_id = _integer(value.get("suiteID"), f"{label}.suiteID")
    if suite_id not in suites:
        raise RuntimeError(f"{label}.suiteID references an unknown suite")
    _metadata(value.get("metadata"), f"{label}.metadata")
    line, column = _location(value, label)
    return value, entry_id, suite_id, line, column


def _failure_finding(test, suite_path, failure_code):
    from .verification_adapters import VerificationFinding

    name = test["name"].strip()
    kind = "failure" if failure_code == "dart_test.failure" else "error"
    message = f"Dart test {kind}"
    if name:
        message += f": {name}"

    return VerificationFinding(
        path=suite_path,
        line=test["line"],
        column=test["column"],
        severity="ERROR",
        code=failure_code,
        message=message,
    )


def _parse_json_reporter_output(project_root, stdout):
    events = []
    for output_line, raw_line in enumerate(stdout.splitlines(), start=1):
        if not raw_line:
            continue
        events.append(_strict_json_object(raw_line, output_line))

    if not events:
        raise RuntimeError("Dart JSON reporter output is empty")

    suites = {}
    suite_platforms = {}
    groups = {}
    tests = {}
    completed = {}
    errors = {}
    runner_version = None
    all_suites_count = None
    done_success = "missing"
    protocol_version = None

    for index, event in enumerate(events):
        label = f"events[{index}]"
        event_type = _string(
            event.get("type"),
            f"{label}.type",
            nonempty=True,
            normalized=True,
        )
        _integer(event.get("time"), f"{label}.time")

        if event_type == "start":
            if index != 0 or runner_version is not None:
                raise RuntimeError("Dart JSON start event must be unique and first")
            protocol_version = _string(
                event.get("protocolVersion"),
                f"{label}.protocolVersion",
                nonempty=True,
                normalized=True,
            )
            if _JSON_PROTOCOL_RE.fullmatch(protocol_version) is None:
                raise RuntimeError("Unsupported Dart JSON reporter protocol")
            runner_version = _string(
                event.get("runnerVersion"),
                f"{label}.runnerVersion",
                nonempty=True,
                normalized=True,
            )
            if "pid" in event:
                _integer(event["pid"], f"{label}.pid")
            continue

        if runner_version is None:
            raise RuntimeError("Dart JSON start event must be first")

        if event_type == "allSuites":
            if all_suites_count is not None:
                raise RuntimeError("Dart JSON allSuites event is duplicated")
            all_suites_count = _integer(
                event.get("count"),
                f"{label}.count",
            )
            continue

        if event_type == "suite":
            suite = _mapping(event.get("suite"), f"{label}.suite")
            suite_id = _integer(suite.get("id"), f"{label}.suite.id")
            if suite_id in suites:
                raise RuntimeError("Dart JSON suite ID is duplicated")
            suite_platforms[suite_id] = _string(
                suite.get("platform"),
                f"{label}.suite.platform",
                nullable=True,
            )
            suites[suite_id] = _canonical_suite_path(
                project_root,
                suite.get("path"),
            )
            continue

        if event_type == "group":
            group, group_id, _suite_id, _line, _column = (
                _validate_group_entry(
                    event.get("group"),
                    f"{label}.group",
                    suites,
                )
            )
            if group_id in groups:
                raise RuntimeError("Dart JSON group ID is duplicated")
            parent_id = group.get("parentID")
            if parent_id is not None:
                _integer(parent_id, f"{label}.group.parentID")
                if parent_id not in groups:
                    raise RuntimeError(
                        "Dart JSON group parentID references an unknown group"
                    )
                if groups[parent_id] != _suite_id:
                    raise RuntimeError(
                        "Dart JSON group parent belongs to another suite"
                    )
            _string(
                group.get("name"),
                f"{label}.group.name",
                nullable=True,
            )
            _integer(group.get("testCount"), f"{label}.group.testCount")
            groups[group_id] = _suite_id
            continue

        if event_type == "testStart":
            test, test_id, suite_id, line, column = _validate_group_entry(
                event.get("test"),
                f"{label}.test",
                suites,
            )
            if test_id in tests:
                raise RuntimeError("Dart JSON test ID is duplicated")
            name = _string(
                test.get("name"),
                f"{label}.test.name",
                normalized=True,
            )
            line, column = _root_location(
                test,
                f"{label}.test",
                (line, column),
            )
            group_ids = test.get("groupIDs")
            if not isinstance(group_ids, list):
                raise RuntimeError(f"{label}.test.groupIDs is malformed")
            normalized_group_ids = []
            for group_index, group_id in enumerate(group_ids):
                group_id = _integer(
                    group_id,
                    f"{label}.test.groupIDs[{group_index}]",
                )
                if group_id not in groups:
                    raise RuntimeError(
                        "Dart JSON test references an unknown group"
                    )
                if groups[group_id] != suite_id:
                    raise RuntimeError(
                        "Dart JSON test group belongs to another suite"
                    )
                normalized_group_ids.append(group_id)
            if len(set(normalized_group_ids)) != len(normalized_group_ids):
                raise RuntimeError("Dart JSON test groupIDs are duplicated")
            tests[test_id] = {
                "name": name,
                "suite_id": suite_id,
                "line": line,
                "column": column,
            }
            continue

        if event_type == "print":
            test_id = _integer(event.get("testID"), f"{label}.testID")
            if test_id not in tests:
                raise RuntimeError("Dart JSON print references an unknown test")
            _string(event.get("message"), f"{label}.message")
            message_type = _string(
                event.get("messageType"),
                f"{label}.messageType",
                nonempty=True,
            )
            if message_type not in {"print", "skip"}:
                raise RuntimeError("Dart JSON messageType is unsupported")
            continue

        if event_type == "error":
            test_id = _integer(event.get("testID"), f"{label}.testID")
            if test_id not in tests:
                raise RuntimeError("Dart JSON error references an unknown test")
            _string(event.get("error"), f"{label}.error")
            _string(event.get("stackTrace"), f"{label}.stackTrace")
            is_failure = _boolean(
                event.get("isFailure"),
                f"{label}.isFailure",
            )
            errors.setdefault(test_id, []).append((index, is_failure))
            continue

        if event_type == "testDone":
            test_id = _integer(event.get("testID"), f"{label}.testID")
            if test_id not in tests:
                raise RuntimeError("Dart JSON testDone references an unknown test")
            if test_id in completed:
                raise RuntimeError("Dart JSON testDone event is duplicated")
            result = _string(
                event.get("result"),
                f"{label}.result",
                nonempty=True,
            )
            if result not in {"success", "failure", "error"}:
                raise RuntimeError("Dart JSON test result is unsupported")
            skipped = _boolean(event.get("skipped"), f"{label}.skipped")
            hidden = _boolean(event.get("hidden"), f"{label}.hidden")
            if (skipped or hidden) and result != "success":
                raise RuntimeError(
                    "Dart JSON hidden/skipped test has a failing result"
                )
            completed[test_id] = {
                "result": result,
                "skipped": skipped,
                "hidden": hidden,
                "event_index": index,
            }
            continue

        if event_type == "debug":
            suite_id = _integer(event.get("suiteID"), f"{label}.suiteID")
            if suite_id not in suites:
                raise RuntimeError("Dart JSON debug references an unknown suite")
            for field in ("observatory", "remoteDebugger"):
                if field in event:
                    _string(
                        event[field],
                        f"{label}.{field}",
                        nullable=True,
                    )
            continue

        if event_type == "done":
            if done_success != "missing":
                raise RuntimeError("Dart JSON done event is duplicated")
            done_success = _boolean(
                event.get("success"),
                f"{label}.success",
                nullable=True,
            )
            continue

        # The public JSON reporter protocol explicitly permits future event
        # types. Their base Event fields are validated above, but they do not
        # contribute authority to PASS/FAIL classification.

    if events[-1].get("type") != "done" or done_success == "missing":
        raise RuntimeError("Dart JSON done event must be unique and final")
    if done_success is None:
        raise RuntimeError("Dart test runner closed before completion")
    if all_suites_count is None:
        raise RuntimeError("Dart JSON allSuites event is missing")
    if all_suites_count != len(suites):
        raise RuntimeError("Dart JSON suite count is inconsistent")
    if set(tests) != set(completed):
        raise RuntimeError("Dart JSON contains incomplete test lifecycle data")

    findings = []
    inventory = []
    for test_id, test in tests.items():
        completion = completed[test_id]
        test_errors = errors.get(test_id, [])
        before = [is_failure for event_index, is_failure in test_errors
                  if event_index < completion["event_index"]]
        after = [is_failure for event_index, is_failure in test_errors
                 if event_index > completion["event_index"]]
        expected_completion = (
            "error" if False in before else "failure" if before else "success"
        )
        if completion["result"] != expected_completion:
            raise RuntimeError(
                "Dart JSON completion disagrees with preceding error evidence"
            )
        if completion["skipped"] and test_errors:
            raise RuntimeError("Dart JSON skipped test contains an error")

        failure_code = None
        final_result = "success"
        if False in before or False in after:
            failure_code = "dart_test.error"
            final_result = "error"
        elif before or after:
            failure_code = "dart_test.failure"
            final_result = "failure"

        inventory.append({
            "suite_path": suites[test["suite_id"]],
            "platform": suite_platforms[test["suite_id"]],
            "name": test["name"],
            "line": test["line"],
            "column": test["column"],
            "completion_result": completion["result"],
            "final_result": final_result,
            "skipped": completion["skipped"],
            "hidden": completion["hidden"] if not test_errors else False,
            "errors_before_completion": {
                "failure": before.count(True), "error": before.count(False),
            },
            "errors_after_completion": {
                "failure": after.count(True), "error": after.count(False),
            },
        })

        if failure_code is not None:
            findings.append(
                _failure_finding(
                    test,
                    suites[test["suite_id"]],
                    failure_code,
                )
            )

    findings = tuple(
        sorted(
            set(findings),
            key=lambda finding: (
                finding.path or "",
                finding.line or 0,
                finding.column or 0,
                finding.severity,
                finding.code,
                finding.message,
            ),
        )
    )

    if done_success != (not findings):
        raise RuntimeError(
            "Dart JSON done status disagrees with normalized test failures"
        )

    executed_test_count = sum(
        1 for completion in completed.values()
        if not completion["hidden"] and not completion["skipped"]
    )
    if findings:
        verification_status = "FAIL"
    elif executed_test_count == 0:
        verification_status = "NOT_APPLICABLE"
    else:
        verification_status = "PASS"

    return _ParsedDartTestRun(
        runner_version=runner_version,
        verification_status=verification_status,
        findings=findings,
        semantic_sha256=_semantic_sha256(
            protocol_version=protocol_version,
            runner_version=runner_version,
            suites=[{"path": path, "platform": suite_platforms[suite_id]}
                    for suite_id, path in suites.items()],
            tests=inventory,
        ),
    )


def _not_applicable_result():
    from .verification_adapters import (
        VerificationAdapterResult,
        verification_result_sha256,
    )

    base = VerificationAdapterResult(
        adapter_id="dart.test",
        adapter_version="1",
        tool_name="dart test",
        tool_version="not_executed",
        evaluation_mode="bounded",
        verification_status="NOT_APPLICABLE",
        inspected_paths=(),
        findings=(),
        exit_code=0,
        stdout_sha256=_sha256_text(""),
        stderr_sha256=_sha256_text(""),
        result_sha256="0" * 64,
        semantic_sha256=_semantic_sha256(
            protocol_version=None, runner_version="not_executed", suites=[], tests=[],
        ),
    )
    return replace(
        base,
        result_sha256=verification_result_sha256(base),
    )


def run_dart_test(project_root, evaluation_paths, evaluation_mode):
    """Run the packaged Dart test suite with fixed machine-readable arguments."""
    from .verification_adapters import (
        VerificationAdapterResult,
        verification_result_sha256,
    )

    if evaluation_mode == "bounded":
        if evaluation_paths is None:
            raise RuntimeError(
                "bounded dart.test evaluation requires bounded paths"
            )
        if evaluation_paths:
            raise RuntimeError(
                "non-empty dart.test paths require complete invalidation"
            )
        return _not_applicable_result()

    if evaluation_mode not in {
        "project_wide",
        "project_wide_invalidation",
    }:
        raise RuntimeError("unsupported dart.test evaluation mode")
    if evaluation_paths is not None:
        raise RuntimeError(
            "complete dart.test evaluation must not receive bounded paths"
        )

    completed = run_process(
        ["dart", "test", "--reporter", "json"],
        cwd=project_root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        shell=False,
        timeout=_DART_TEST_TIMEOUT_SECONDS,
        max_capture_bytes=_DART_TEST_MAX_CAPTURE_BYTES,
    )

    stdout = completed.stdout
    stderr = completed.stderr
    if not isinstance(stdout, str) or not isinstance(stderr, str):
        raise RuntimeError("dart test returned non-text output")
    if type(completed.returncode) is not int or completed.returncode not in {0, 1}:
        raise RuntimeError("dart test infrastructure failure")

    parsed = _parse_json_reporter_output(project_root, stdout)
    expected_exit_code = 1 if parsed.verification_status == "FAIL" else 0
    if completed.returncode != expected_exit_code:
        raise RuntimeError(
            "dart test exit code disagrees with structured test result"
        )

    base = VerificationAdapterResult(
        adapter_id="dart.test",
        adapter_version="1",
        tool_name="dart test",
        tool_version=parsed.runner_version,
        evaluation_mode=evaluation_mode,
        verification_status=parsed.verification_status,
        inspected_paths=(),
        findings=parsed.findings,
        exit_code=completed.returncode,
        stdout_sha256=_sha256_text(stdout),
        stderr_sha256=_sha256_text(stderr),
        result_sha256="0" * 64,
        semantic_sha256=parsed.semantic_sha256,
    )
    return replace(
        base,
        result_sha256=verification_result_sha256(base),
    )
