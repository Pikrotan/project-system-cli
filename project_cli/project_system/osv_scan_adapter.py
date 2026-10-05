"""Read-only, resolved-lockfile-only OSV-Scanner v2 verification.

Discovery and output validation are Project System trust boundaries. OSV gets
explicit files, not a recursive directory target or project-controlled options.
"""

from dataclasses import replace
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile

from .process_runner import run_process
from .rule_scope import canonical_rule_path, RuleScopeError


SUPPORTED_LOCKFILES = frozenset({
    "pubspec.lock", "package-lock.json", "pnpm-lock.yaml", "yarn.lock", "bun.lock",
    "uv.lock", "poetry.lock", "Pipfile.lock", "pdm.lock", "pylock.toml",
})
EXCLUDED_DIRECTORIES = frozenset({
    ".git", ".generated", ".dart_tool", ".pub-cache", "build", "node_modules",
    ".venv", "venv", "__pycache__", "vendor", "dist", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", ".cache", ".tox", ".nox", "coverage",
})
OSV_TIMEOUT_SECONDS = 300
OSV_MAX_CAPTURE_BYTES = 16 * 1024 * 1024
OSV_VERSION_TIMEOUT_SECONDS = 10
OSV_VERSION_MAX_CAPTURE_BYTES = 64 * 1024
HASH_CHUNK_BYTES = 1024 * 1024
_VERSION_RE = re.compile(r"osv-scanner version: (2\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*))")
_NAME_RE = re.compile(r"[@A-Za-z0-9][@A-Za-z0-9._/+!~%=-]*")
_VERSION_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+!~:-]*")
_ECOSYSTEM_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._:/-]*")
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]*")


def _error(message):
    from .verification_adapters import VerificationAdapterError

    # Fixed messages only: never include exception text, paths or raw output.
    raise VerificationAdapterError(message) from None


def _canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def _sha256(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _semantic_digest(lockfiles, packages, vulnerabilities):
    return _sha256(_canonical_json({
        "schema_version": 1,
        "lockfiles": list(lockfiles),
        # Keep package rows as a list, not a set: multiplicity is meaningful.
        "packages": sorted(packages, key=_canonical_json),
        "vulnerabilities": sorted(vulnerabilities, key=_canonical_json),
    }))


def _lstat(path):
    try:
        info = os.lstat(path)
    except OSError:
        _error("OSV dependency path cannot be inspected reliably")
    if (stat.S_ISLNK(info.st_mode)
            or getattr(info, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)):
        _error("OSV dependency discovery encountered a symlink or reparse point")
    return info


def _root(value):
    selected = Path(value).absolute()
    # Inspect ancestors before resolve, so a reparse alias cannot be erased.
    for current in reversed((selected, *selected.parents)):
        if not stat.S_ISDIR(_lstat(current).st_mode):
            _error("OSV project root is not a regular directory")
    try:
        root = selected.resolve(strict=True)
    except (OSError, RuntimeError):
        _error("OSV project root cannot be resolved reliably")
    return root


def _safe_file(root, relative):
    if _root(root) != root:
        _error("OSV dependency root containment changed")
    try:
        if canonical_rule_path(relative, "OSV lockfile", pattern=False) != relative:
            _error("OSV lockfile path is not canonical")
    except RuleScopeError:
        _error("OSV lockfile path is unsafe")
    # OSV/urfave accepts comma-separated lockfile values and parser:path syntax.
    # These delimiters cannot be used as literal repository path components.
    if ("," in str(root / relative) or ":" in relative
            or any(ord(char) < 32 or ord(char) == 127 for char in relative)
            or len(relative) > 4096 or Path(relative).name not in SUPPORTED_LOCKFILES):
        _error("OSV lockfile path is unsupported or ambiguous")
    current = root
    parts = relative.split("/")
    for index, part in enumerate(parts):
        current = current / part
        info = _lstat(current)
        required = stat.S_ISREG if index == len(parts) - 1 else stat.S_ISDIR
        if not required(info.st_mode):
            _error("OSV lockfile path is not a regular file")
    try:
        if current.resolve(strict=True) != current:
            _error("OSV lockfile path has ambiguous containment")
        current.relative_to(root)
    except (OSError, RuntimeError, ValueError):
        _error("OSV lockfile path escapes project root")
    return current, info


def discover_lockfiles(project_root):
    """Return sorted canonical lockfile paths; never follow reparse points."""
    root = _root(project_root)
    pending = [root]
    found = []
    while pending:
        directory = pending.pop()
        # Re-check directories before enumeration, including after discovery.
        if not stat.S_ISDIR(_lstat(directory).st_mode):
            _error("OSV dependency directory is not regular")
        try:
            with os.scandir(directory) as scan:
                entries = sorted(scan, key=lambda entry: entry.name)
        except OSError:
            _error("OSV dependency directory cannot be enumerated")
        for entry in entries:
            path = Path(entry.path)
            # Excluded names are subtrees with no canonical dependency authority.
            if entry.name.casefold() in EXCLUDED_DIRECTORIES:
                continue
            info = _lstat(path)
            if entry.name in SUPPORTED_LOCKFILES:
                relative = path.relative_to(root).as_posix()
                _safe_file(root, relative)
                found.append(relative)
            elif stat.S_ISDIR(info.st_mode):
                pending.append(path)
    return tuple(sorted(found))


def _regular_stat_state(info):
    if (not stat.S_ISREG(info.st_mode)
            or getattr(info, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)):
        _error("OSV integrity input is not a regular non-reparse file")
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _windows_read_fd(path):
    # Python's os.open has no O_NOFOLLOW on Windows. Open the reparse object,
    # not its target, then reject it using handle stat before reading any bytes.
    import ctypes
    from ctypes import wintypes
    import msvcrt

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                       wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    close = kernel.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL
    # GENERIC_READ, share read/write/delete, OPEN_EXISTING, OPEN_REPARSE_POINT.
    handle = create(str(path), 0x80000000, 7, None, 3, 0x00200000, None)
    if handle == wintypes.HANDLE(-1).value:
        raise OSError(ctypes.get_last_error(), "cannot open OSV integrity input")
    try:
        return msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY | os.O_NOINHERIT)
    except BaseException:
        close(handle)
        raise


def _open_binary(path):
    if os.name == "nt":
        fd = _windows_read_fd(path)
    else:
        if not hasattr(os, "O_NOFOLLOW"):
            _error("OSV integrity requires a no-follow file open primitive")
        # NONBLOCK avoids hanging on a raced FIFO before fstat can reject it.
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        return os.fdopen(fd, "rb", buffering=0)
    except BaseException:
        os.close(fd)
        raise


def _content_state(path, path_stat):
    """Bind streamed bytes to consistent path/handle observations, not a snapshot."""
    try:
        before = _regular_stat_state(path_stat())
        digest = hashlib.sha256()
        size = 0
        with _open_binary(path) as stream:
            opened = _regular_stat_state(os.fstat(stream.fileno()))
            if opened != before:
                _error("OSV integrity input changed while being opened")
            while chunk := stream.read(HASH_CHUNK_BYTES):
                size += len(chunk)
                if size > before[2]:
                    _error("OSV integrity input changed while being read")
                digest.update(chunk)
            after = _regular_stat_state(os.fstat(stream.fileno()))
            current = _regular_stat_state(path_stat())
            if after != before or current != before or size != before[2]:
                _error("OSV integrity input changed while being read")
        return (*before, digest.hexdigest())
    except OSError:
        _error("OSV integrity input cannot be read reliably")


def _file_state(root, lockfiles):
    return tuple((relative, *_content_state(
        root / relative, lambda: _safe_file(root, relative)[1],
    )) for relative in lockfiles)


def _regular_path_stat(path):
    if not path.is_absolute() or _root(path.parent) != path.parent:
        _error("OSV integrity path containment changed")
    info = _lstat(path)
    _regular_stat_state(info)
    try:
        if path.resolve(strict=True) != path:
            _error("OSV integrity path containment changed")
    except (OSError, RuntimeError):
        _error("OSV integrity path cannot be resolved reliably")
    return info


def _external_state(path):
    return (str(path), *_content_state(path, lambda: _regular_path_stat(path)))


def _directory_state(directory):
    if _root(directory) != directory:
        _error("OSV temporary directory containment changed")
    info = _lstat(directory)
    if not stat.S_ISDIR(info.st_mode):
        _error("OSV temporary directory is not a regular directory")
    return str(directory), info.st_dev, info.st_ino


def _config_state(config, directory_state):
    if _directory_state(config.parent) != directory_state:
        _error("OSV temporary directory identity changed")
    state = _external_state(config)
    if _directory_state(config.parent) != directory_state:
        _error("OSV temporary directory identity changed")
    if state[3] != 0 or state[-1] != _sha256(""):
        _error("OSV temporary config must remain empty")
    return state


@contextmanager
def _temporary_directory(temp_root):
    # Cleanup must not adopt a different real directory substituted at this
    # pathname. Do not schedule an unconditional GC finalizer for that path.
    directory = Path(tempfile.mkdtemp(prefix="project-system-osv-", dir=temp_root))
    state = None
    try:
        state = _directory_state(directory)
        yield str(directory), state
    finally:
        if state is None or _directory_state(directory) != state:
            _error("OSV temporary directory ownership changed; cleanup refused")
        try:
            shutil.rmtree(directory)
        except OSError:
            _error("OSV temporary directory could not be cleaned safely")


def _executable(root):
    selected = shutil.which("osv-scanner")
    if not selected:
        _error("OSV-Scanner executable is unavailable")
    try:
        path = Path(selected).resolve(strict=True)
        if not path.is_file() or path.is_relative_to(root):
            _error("OSV-Scanner executable must be external to the project")
    except (OSError, RuntimeError):
        _error("OSV-Scanner executable cannot be resolved reliably")
    return str(path)


def _run(argv, directory, *, version=False):
    try:
        result = run_process(
            argv, cwd=directory, capture_output=True, text=True, encoding="utf-8",
            check=False, shell=False,
            timeout=OSV_VERSION_TIMEOUT_SECONDS if version else OSV_TIMEOUT_SECONDS,
            max_capture_bytes=OSV_VERSION_MAX_CAPTURE_BYTES if version else OSV_MAX_CAPTURE_BYTES,
        )
    except Exception:
        _error("OSV-Scanner execution failed")
    if (type(result.returncode) is not int
            or not isinstance(result.stdout, str) or not isinstance(result.stderr, str)):
        _error("OSV-Scanner returned an invalid execution result")
    return result


def _tool_version(tool, directory):
    result = _run([tool, "--version"], directory, version=True)
    if result.returncode != 0:
        _error("OSV-Scanner version check failed")
    candidates = [line for line in (result.stdout + "\n" + result.stderr).splitlines()
                  if line.startswith("osv-scanner version:")]
    if len(candidates) != 1:
        _error("OSV-Scanner version output is malformed")
    match = _VERSION_RE.fullmatch(candidates[0])
    # All-packages inventory was fixed in 2.3.0, bun.lock added in 2.3.2,
    # and pylock.toml in 2.3.3. Never downgrade the fixed scan contract.
    if not match or tuple(map(int, match.group(1).split('.'))) < (2, 3, 3):
        _error("OSV-Scanner version is unsupported or malformed")
    return match.group(1)


def _json(text):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    def constant(_value):
        raise ValueError("non-standard number")

    try:
        return json.loads(text, object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, RecursionError):
        _error("OSV-Scanner JSON is malformed")


def _object(value, *, allowed=None):
    if not isinstance(value, dict) or (allowed is not None and set(value) - allowed):
        _error("OSV-Scanner result structure is unsupported or malformed")
    return value


def _list(value, *, nonempty=False):
    if not isinstance(value, list) or (nonempty and not value):
        _error("OSV-Scanner result inventory is missing or malformed")
    return value


def _token(value, expression, maximum):
    if (not isinstance(value, str) or not value or len(value) > maximum
            or value != value.strip() or not expression.fullmatch(value)):
        _error("OSV-Scanner identity is incomplete or malformed")
    return value


def _ids(value, *, nonempty=False):
    return tuple(sorted({_token(item, _ID_RE, 128)
                         for item in _list(value, nonempty=nonempty)}))


def _source_path(root, value, lockfiles):
    if not isinstance(value, str) or not value or value != value.strip():
        _error("OSV-Scanner source path is malformed")
    path = Path(value)
    # Absolute paths in actual OSV output are accepted, but not ../ aliases.
    if ".." in path.parts or "." in value.replace("\\", "/").split("/"):
        _error("OSV-Scanner source path is unsafe")
    if path.is_absolute():
        try:
            relative = path.relative_to(root).as_posix()
        except ValueError:
            _error("OSV-Scanner source is outside the project")
    else:
        relative = value
    _safe_file(root, relative)
    if relative not in lockfiles:
        _error("OSV-Scanner source was not an explicit scan input")
    return relative


def _expected_groups(advisory_identities):
    # Alias-indexed DFS: avoid comparing every advisory pair. Every shared
    # identity token adds an undirected edge; traversal order is non-semantic.
    by_alias = {}
    for identity, aliases in advisory_identities.items():
        for alias in aliases:
            by_alias.setdefault(alias, set()).add(identity)
    remaining = set(advisory_identities)
    groups = set()
    for seed in sorted(advisory_identities):
        if seed not in remaining:
            continue
        pending = [seed]
        ids, aliases = set(), set()
        while pending:
            identity = pending.pop()
            if identity not in remaining:
                continue
            remaining.remove(identity)
            ids.add(identity)
            for alias in advisory_identities[identity]:
                if alias not in aliases:
                    aliases.add(alias)
                    pending.extend(by_alias[alias].intersection(remaining))
        groups.add((tuple(sorted(ids)), tuple(sorted(aliases))))
    return groups


def _parse(root, text, lockfiles, exit_code):
    from .verification_adapters import VerificationFinding

    envelope = _object(_json(text), allowed={
        "results", "experimental_config", "experimental_generic_findings",
        "image_metadata", "license_summary",
    })
    # These capabilities were not requested and must not create result authority.
    for key in ("experimental_generic_findings", "license_summary"):
        if key in envelope and _list(envelope[key]):
            _error("OSV-Scanner returned unsupported findings")
    if envelope.get("image_metadata") is not None:
        _error("OSV-Scanner returned unsupported image data")
    if "experimental_config" in envelope:
        config = _object(envelope["experimental_config"], allowed={"licenses"})
        if "licenses" in config:
            licenses = _object(config["licenses"], allowed={"summary", "allowlist"})
            if licenses.get("summary", False) is not False or licenses.get("allowlist") not in (None, []):
                _error("OSV-Scanner returned unsupported license policy")

    seen = set()
    inventory = []
    vulnerability_inventory = []
    findings = set()
    for result in _list(envelope.get("results"), nonempty=True):
        result = _object(result, allowed={"source", "packages", "experimental_pes"})
        if "experimental_pes" in result and _list(result["experimental_pes"]):
            _error("OSV-Scanner returned unsupported exploitability signals")
        source = _object(result.get("source"), allowed={"path", "type"})
        if source.get("type") != "lockfile":
            _error("OSV-Scanner source must be a lockfile")
        relative = _source_path(root, source.get("path"), lockfiles)
        if relative in seen:
            _error("OSV-Scanner source coverage is duplicated")
        seen.add(relative)
        for item in _list(result.get("packages"), nonempty=True):
            item = _object(item, allowed={
                "package", "vulnerabilities", "groups", "dependency_groups",
                "licenses", "license_violations",
            })
            identity = _object(item.get("package"), allowed={
                "ecosystem", "name", "version", "commit", "os_package_name",
                "image_origin_details", "deprecated",
            })
            if any(identity.get(key) not in (None, "")
                   for key in ("commit", "os_package_name")):
                _error("OSV-Scanner returned unsupported package identity")
            if (identity.get("image_origin_details") is not None
                    or identity.get("deprecated", False) is not False):
                _error("OSV-Scanner returned unsupported package identity")
            package = {
                "source": relative,
                "ecosystem": _token(identity.get("ecosystem"), _ECOSYSTEM_RE, 128),
                "name": _token(identity.get("name"), _NAME_RE, 256),
                "version": _token(identity.get("version"), _VERSION_TOKEN_RE, 256),
            }
            inventory.append(package)
            for key in ("dependency_groups", "licenses", "license_violations"):
                if key in item:
                    values = _list(item[key])
                    if any(not isinstance(value, str) for value in values):
                        _error("OSV-Scanner package metadata is malformed")
                    if key == "license_violations" and values:
                        _error("OSV-Scanner returned unsupported license findings")
            advisory_identities = {}
            for advisory in _list(item.get("vulnerabilities", [])):
                advisory = _object(advisory)
                advisory_id = _token(advisory.get("id"), _ID_RE, 128)
                alias_identity = frozenset({advisory_id, *_ids(advisory.get("aliases", []))})
                if (advisory_id in advisory_identities
                        and advisory_identities[advisory_id] != alias_identity):
                    _error("OSV-Scanner duplicate advisory identities conflict")
                advisory_identities[advisory_id] = alias_identity
            vuln_ids = set(advisory_identities)
            groups = set()
            for group in _list(item.get("groups", [])):
                group = _object(group, allowed={
                    "ids", "aliases", "max_severity", "experimental_analysis", "experimentalAnalysis",
                })
                ids = _ids(group.get("ids"), nonempty=True)
                aliases = _ids(group.get("aliases"), nonempty=True)
                # Call analysis cannot suppress vulnerabilities in this adapter.
                for key in ("experimental_analysis", "experimentalAnalysis"):
                    if key in group:
                        _object(group[key])
                groups.add((ids, aliases))
            grouped = set()
            grouped_aliases = set()
            for ids, aliases in sorted(groups):
                if grouped.intersection(ids):
                    _error("OSV-Scanner vulnerability groups overlap")
                if not set(ids).issubset(vuln_ids):
                    _error("OSV-Scanner vulnerability groups contradict advisory identities")
                expected_aliases = set().union(*(advisory_identities[identity] for identity in ids))
                if set(aliases) != expected_aliases:
                    _error("OSV-Scanner group aliases contradict advisory identities")
                if grouped_aliases.intersection(aliases):
                    _error("OSV-Scanner vulnerability group aliases overlap")
                grouped.update(ids)
                grouped_aliases.update(aliases)
            if grouped != vuln_ids:
                _error("OSV-Scanner vulnerability groups contradict advisory identities")
            if groups != _expected_groups(advisory_identities):
                _error("OSV-Scanner groups contradict advisory connected components")
            vulnerability_inventory.append({
                **package,
                "ids": sorted(vuln_ids),
                "groups": [{"ids": list(ids), "aliases": list(aliases)}
                           for ids, aliases in sorted(groups)],
            })
            for vuln_id in sorted(vuln_ids):
                findings.add(VerificationFinding(
                    path=relative, line=None, column=None, severity="ERROR", code="osv.vulnerability",
                    message=(f"OSV vulnerability: {package['ecosystem']} "
                             f"{package['name']}@{package['version']} [{vuln_id}]"),
                ))
    if seen != set(lockfiles):
        _error("OSV-Scanner source coverage is incomplete")
    if type(exit_code) is not int or exit_code != (1 if findings else 0):
        _error("OSV-Scanner exit code disagrees with vulnerability inventory")
    ordered_findings = tuple(sorted(findings, key=lambda finding: (
        finding.path, finding.line or 0, finding.column or 0,
        finding.severity, finding.code, finding.message,
    )))
    return ordered_findings, _semantic_digest(lockfiles, inventory, vulnerability_inventory)


def _result(mode, *, lockfiles=(), tool_version="not_executed", stdout="", stderr="",
            exit_code=0, findings=(), semantic=None):
    from .verification_adapters import VerificationAdapterResult, verification_result_sha256

    base = VerificationAdapterResult(
        adapter_id="osv.scan", adapter_version="1", tool_name="osv-scanner",
        tool_version=tool_version, evaluation_mode=mode,
        verification_status=("NOT_APPLICABLE" if not lockfiles else "FAIL" if findings else "PASS"),
        inspected_paths=(), findings=findings, exit_code=exit_code,
        stdout_sha256=_sha256(stdout), stderr_sha256=_sha256(stderr), result_sha256="0" * 64,
        semantic_sha256=semantic or _semantic_digest((), [], []),
    )
    return replace(base, result_sha256=verification_result_sha256(base))


def run_osv_scan(project_root, evaluation_paths, evaluation_mode):
    if evaluation_mode == "bounded":
        if evaluation_paths != ():
            _error("Non-empty OSV bounded input requires complete invalidation")
        return _result(evaluation_mode)
    if evaluation_mode not in {"project_wide", "project_wide_invalidation"} or evaluation_paths is not None:
        _error("OSV evaluation context is unsupported")
    root = _root(project_root)
    lockfiles = discover_lockfiles(root)
    if not lockfiles:
        return _result(evaluation_mode)
    before = _file_state(root, lockfiles)
    tool = _executable(root)
    executable_state = _external_state(Path(tool))  # E0: before version probe.
    # Do not even create an adapter-owned directory inside a project if TMP was
    # configured there. No permanent or .generated policy file is introduced.
    try:
        temp_root = _root(Path(tempfile.gettempdir()))
    except (OSError, RuntimeError):
        _error("OSV temporary config location cannot be resolved")
    if temp_root.is_relative_to(root):
        _error("OSV temporary config must be outside the project")
    with _temporary_directory(temp_root) as (directory, directory_state):
        config = Path(directory) / "osv-scanner.toml"
        # Exclusive creation: never overwrite even an empty pre-existing file.
        with config.open("xb"):
            pass
        config_state = _config_state(config, directory_state)  # C0.
        version = _tool_version(tool, directory)
        if _external_state(Path(tool)) != executable_state:  # E1.
            _error("OSV executable changed during verification")
        # T1 binds the complete discovery inventory, not just the old paths.
        if discover_lockfiles(root) != lockfiles or before != _file_state(root, lockfiles):
            _error("OSV dependency inputs changed during verification")
        argv = [tool, "scan", "source", "--format=json", "--all-packages", "--no-resolve",
                "--no-call-analysis=all", "--all-vulns", "--config", str(config)]
        for relative in lockfiles:
            argv.extend(["-L", str(root / relative)])
        if _config_state(config, directory_state) != config_state:  # C1.
            _error("OSV temporary config changed during verification")
        if _external_state(Path(tool)) != executable_state:  # E2.
            _error("OSV executable changed during verification")
        result = _run(argv, directory)
        if _external_state(Path(tool)) != executable_state:  # E3.
            _error("OSV executable changed during verification")
        if discover_lockfiles(root) != lockfiles or before != _file_state(root, lockfiles):  # T2.
            _error("OSV dependency inputs changed during verification")
        if _config_state(config, directory_state) != config_state:  # C2.
            _error("OSV temporary config changed during verification")
        # Only 0/1 currently have defined successful result semantics. Reserved
        # codes are not evidence of a vulnerability; 127/128/129 etc are ERROR.
        if result.returncode not in {0, 1}:
            _error("OSV-Scanner did not establish a trustworthy scan result")
        findings, semantic = _parse(root, result.stdout, lockfiles, result.returncode)
        return _result(
            evaluation_mode, lockfiles=lockfiles, tool_version=version,
            stdout=result.stdout, stderr=result.stderr, exit_code=result.returncode,
            findings=findings, semantic=semantic,
        )
