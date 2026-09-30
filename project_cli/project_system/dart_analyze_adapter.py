"""Packaged Dart static-analysis Verification Adapter."""

from dataclasses import replace
import hashlib
from pathlib import Path
import re

from .process_runner import run_process


_DART_VERSION_RE = re.compile(r"Dart SDK version:\s+(\S+)")

_DART_VERSION_MAX_CAPTURE_BYTES = 64 * 1024
_DART_ANALYZE_MAX_CAPTURE_BYTES = 16 * 1024 * 1024


def _sha256_text(value):
    if not isinstance(value, str):
        raise RuntimeError("Dart process output must be text")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _dart_version(project_root):
    completed = run_process(
        ["dart", "--version"],
        cwd=project_root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=10,
        max_capture_bytes=_DART_VERSION_MAX_CAPTURE_BYTES,
    )

    if completed.returncode != 0:
        raise RuntimeError("dart --version failed")

    stdout = completed.stdout
    stderr = completed.stderr

    if not isinstance(stdout, str) or not isinstance(stderr, str):
        raise RuntimeError("dart --version returned non-text output")

    match = _DART_VERSION_RE.search(stdout + "\n" + stderr)
    if match is None:
        raise RuntimeError("Dart SDK version could not be parsed")

    return match.group(1)


def _canonical_machine_path(project_root, raw_path):
    if not isinstance(raw_path, str) or not raw_path:
        raise RuntimeError("Dart diagnostic path is malformed")

    source = Path(raw_path)
    if not source.is_absolute():
        raise RuntimeError("Dart diagnostic path must be absolute")

    root = Path(project_root).resolve()
    source = source.resolve()

    try:
        relative = source.relative_to(root)
    except ValueError as exc:
        raise RuntimeError(
            "Dart diagnostic path is outside the project root"
        ) from exc

    return relative.as_posix()


def _decode_machine_fields(raw_line, *, output_line):
    fields = []
    current = []
    index = 0

    escape_map = {
        "\\": "\\",
        "|": "|",
        "n": "\n",
        "r": "\r",
    }

    while index < len(raw_line):
        char = raw_line[index]

        if char == "|":
            fields.append("".join(current))
            current = []
            index += 1
            continue

        if char == "\\":
            index += 1

            if index >= len(raw_line):
                raise RuntimeError(
                    f"Malformed Dart machine escape at output line {output_line}"
                )

            escaped = raw_line[index]
            if escaped not in escape_map:
                raise RuntimeError(
                    f"Unsupported Dart machine escape at output line {output_line}"
                )

            current.append(escape_map[escaped])
            index += 1
            continue

        current.append(char)
        index += 1

    fields.append("".join(current))

    if len(fields) != 8:
        raise RuntimeError(
            f"Malformed Dart machine diagnostic at output line {output_line}"
        )

    return tuple(fields)


def _parse_machine_output(project_root, stdout):
    from .verification_adapters import VerificationFinding

    findings = []

    for line_number, raw_line in enumerate(stdout.splitlines(), start=1):
        if not raw_line:
            continue

        fields = _decode_machine_fields(
            raw_line,
            output_line=line_number,
        )

        (
            severity,
            diagnostic_type,
            code,
            raw_path,
            raw_line_number,
            raw_column,
            raw_length,
            message,
        ) = fields

        if severity not in {"ERROR", "WARNING", "INFO"}:
            raise RuntimeError("Unsupported Dart diagnostic severity")

        if not diagnostic_type or not code or not message:
            raise RuntimeError("Malformed Dart machine diagnostic")

        try:
            source_line = int(raw_line_number)
            source_column = int(raw_column)
            source_length = int(raw_length)
        except ValueError as exc:
            raise RuntimeError(
                "Dart diagnostic location is malformed"
            ) from exc

        if (
            source_line <= 0
            or source_column <= 0
            or source_length < 0
        ):
            raise RuntimeError(
                "Dart diagnostic location is malformed"
            )

        findings.append(
            VerificationFinding(
                path=_canonical_machine_path(project_root, raw_path),
                line=source_line,
                column=source_column,
                severity=severity,
                code=code,
                message=message,
            )
        )

    return tuple(
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


def _expected_analyze_exit_code(findings):
    severities = {finding.severity for finding in findings}

    if "ERROR" in severities:
        return 3
    if "WARNING" in severities:
        return 2
    return 0


def run_dart_analyze(project_root, evaluation_paths, evaluation_mode):
    """Run packaged Dart analysis with fixed arguments."""
    from .verification_adapters import (
        VerificationAdapterResult,
        verification_result_sha256,
    )

    if evaluation_mode == "bounded":
        if evaluation_paths is None:
            raise RuntimeError(
                "bounded dart.analyze evaluation requires bounded paths"
            )

        base = VerificationAdapterResult(
            adapter_id="dart.analyze",
            adapter_version="1",
            tool_name="dart",
            tool_version="not_executed",
            evaluation_mode="bounded",
            verification_status="NOT_APPLICABLE",
            inspected_paths=(),
            findings=(),
            exit_code=0,
            stdout_sha256=_sha256_text(""),
            stderr_sha256=_sha256_text(""),
            result_sha256="0" * 64,
        )

        return replace(
            base,
            result_sha256=verification_result_sha256(base),
        )

    if evaluation_mode not in {
        "project_wide",
        "project_wide_invalidation",
    }:
        raise RuntimeError("unsupported dart.analyze evaluation mode")

    if evaluation_paths is not None:
        raise RuntimeError(
            "complete dart.analyze evaluation must not receive bounded paths"
        )

    tool_version = _dart_version(project_root)

    completed = run_process(
        [
            "dart",
            "analyze",
            "--format=machine",
            "--no-plugins",
            ".",
        ],
        cwd=project_root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=120,
        max_capture_bytes=_DART_ANALYZE_MAX_CAPTURE_BYTES,
    )

    stdout = completed.stdout
    stderr = completed.stderr

    if not isinstance(stdout, str) or not isinstance(stderr, str):
        raise RuntimeError("dart analyze returned non-text output")

    if (
        type(completed.returncode) is not int
        or completed.returncode not in {0, 1, 2, 3}
    ):
        raise RuntimeError("dart analyze infrastructure failure")

    findings = _parse_machine_output(project_root, stdout)

    expected_exit_code = _expected_analyze_exit_code(findings)
    if completed.returncode != expected_exit_code:
        raise RuntimeError(
            "dart analyze exit code disagrees with diagnostic severities"
        )

    verification_status = (
        "PASS" if completed.returncode == 0 else "FAIL"
    )
    inspected_paths = ()

    base = VerificationAdapterResult(
        adapter_id="dart.analyze",
        adapter_version="1",
        tool_name="dart",
        tool_version=tool_version,
        evaluation_mode=evaluation_mode,
        verification_status=verification_status,
        inspected_paths=inspected_paths,
        findings=findings,
        exit_code=completed.returncode,
        stdout_sha256=_sha256_text(stdout),
        stderr_sha256=_sha256_text(stderr),
        result_sha256="0" * 64,
    )

    return replace(
        base,
        result_sha256=verification_result_sha256(base),
    )
