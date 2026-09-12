"""Windows Task Scheduler adapter for Project System automatic SYNC."""
from datetime import datetime, timedelta
import csv
from hashlib import sha256
import io
import ntpath
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET


TASK_XML_NS = 'http://schemas.microsoft.com/windows/2004/02/mit/task'
TASK_DESCRIPTION_PREFIX = 'Project System automatic SYNC registration '
SID_RE = re.compile(r'^S-1-[0-9]+(?:-[0-9]+)+$')
DURATION_RE = re.compile(
    r'^P(?:(?P<days>[0-9]+)D)?(?:T(?:(?P<hours>[0-9]+)H)?'
    r'(?:(?P<minutes>[0-9]+)M)?(?:(?P<seconds>[0-9]+)S)?)?$'
)


class WindowsSchedulerError(RuntimeError):
    pass


def _decode_output(raw):
    if raw.startswith((b'\xff\xfe', b'\xfe\xff')):
        return raw.decode('utf-16')
    for encoding in ('utf-8-sig', 'mbcs'):
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            pass
    raise WindowsSchedulerError('Task Scheduler returned undecodable output')


def _duration_seconds(value):
    match = DURATION_RE.fullmatch(value or '')
    if not match or not any(match.group(name) for name in (
        'days', 'hours', 'minutes', 'seconds',
    )):
        return None
    return (
        int(match.group('days') or 0) * 86400
        + int(match.group('hours') or 0) * 3600
        + int(match.group('minutes') or 0) * 60
        + int(match.group('seconds') or 0)
    )


def _boolean(value, default=None):
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {'true', '1'}:
        return True
    if normalized in {'false', '0'}:
        return False
    return None


def _enum(value):
    return value.strip().casefold() if isinstance(value, str) else None


def _windows_path(value):
    if not isinstance(value, str):
        return None
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        value = value[1:-1]
    if not value or '%' in value or not ntpath.isabs(value):
        return None
    if value.startswith('\\\\?\\'):
        value = value[4:]
    return ntpath.normcase(ntpath.normpath(value))


def _paths_equivalent(expected, actual):
    expected_identity = _windows_path(expected)
    actual_identity = _windows_path(actual)
    if expected_identity is None or actual_identity is None:
        return False
    if os.name == 'nt':
        try:
            if Path(expected).is_file() and Path(actual.strip('"')).is_file():
                return os.path.samefile(expected, actual.strip('"'))
        except OSError:
            pass
    return expected_identity == actual_identity


def _argument_tokens(value):
    """Parse the conservative subset needed by the fixed auto-run arguments."""
    if not isinstance(value, str):
        return None
    tokens = []
    position = 0
    while position < len(value):
        while position < len(value) and value[position].isspace():
            position += 1
        if position == len(value):
            break
        if value[position] == '"':
            end = value.find('"', position + 1)
            if end < 0 or (end + 1 < len(value) and not value[end + 1].isspace()):
                return None
            token = value[position + 1:end]
            if '"' in token or '\\' in token:
                return None
            position = end + 1
        else:
            end = position
            while end < len(value) and not value[end].isspace():
                if value[end] == '"':
                    return None
                end += 1
            token = value[position:end]
            position = end
        if not token:
            return None
        tokens.append(token)
    return tokens


def _same_user(actual, expected_sid, expected_name=None, resolver=None):
    if not isinstance(actual, str) or not actual.strip():
        return False
    actual = actual.strip()
    if actual.casefold() == expected_sid.casefold():
        return True
    if expected_name and actual.casefold() == expected_name.casefold():
        return True
    if resolver is not None and not SID_RE.fullmatch(actual):
        resolved = resolver(actual)
        return bool(resolved and resolved.casefold() == expected_sid.casefold())
    return False


def _safe_value(value, *, sensitive=False):
    if value is None:
        return '<missing>'
    if sensitive:
        raw = str(value).encode('utf-8', errors='replace')
        return f'<different sha256={sha256(raw).hexdigest()[:12]} length={len(raw)}>'
    rendered = str(value).replace('\r', r'\r').replace('\n', r'\n')
    return rendered[:160] + ('...' if len(rendered) > 160 else '')


def _lookup_account_sid(account):
    """Resolve a Windows account representation to its SID without shelling out."""
    if SID_RE.fullmatch(account or ''):
        return account
    if os.name != 'nt' or not isinstance(account, str) or not account:
        return None
    import ctypes
    from ctypes import wintypes

    lookup = ctypes.WinDLL('advapi32', use_last_error=True).LookupAccountNameW
    lookup.argtypes = [
        wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPVOID,
        ctypes.POINTER(wintypes.DWORD), wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD),
    ]
    lookup.restype = wintypes.BOOL
    sid_size = wintypes.DWORD(0)
    domain_size = wintypes.DWORD(0)
    sid_use = wintypes.DWORD(0)
    lookup(
        None, account, None, ctypes.byref(sid_size), None,
        ctypes.byref(domain_size), ctypes.byref(sid_use),
    )
    if ctypes.get_last_error() != 122 or not sid_size.value:
        return None
    sid = ctypes.create_string_buffer(sid_size.value)
    domain = ctypes.create_unicode_buffer(max(domain_size.value, 1))
    if not lookup(
        None, account, sid, ctypes.byref(sid_size), domain,
        ctypes.byref(domain_size), ctypes.byref(sid_use),
    ):
        return None
    convert = ctypes.WinDLL('advapi32', use_last_error=True).ConvertSidToStringSidW
    convert.argtypes = [wintypes.LPVOID, ctypes.POINTER(wintypes.LPWSTR)]
    convert.restype = wintypes.BOOL
    string_sid = wintypes.LPWSTR()
    if not convert(sid, ctypes.byref(string_sid)):
        return None
    try:
        return string_sid.value
    finally:
        local_free = ctypes.WinDLL('kernel32', use_last_error=True).LocalFree
        local_free.argtypes = [wintypes.HLOCAL]
        local_free.restype = wintypes.HLOCAL
        local_free(string_sid)


def _child(parent, name, text=None, **attributes):
    value = ET.SubElement(parent, f'{{{TASK_XML_NS}}}{name}', attributes)
    if text is not None:
        value.text = str(text)
    return value


def _task_arguments(registration):
    return subprocess.list2cmdline(registration['runner']['arguments'])


def task_xml(registration, user_sid, start_at=None):
    if not SID_RE.fullmatch(user_sid):
        raise WindowsSchedulerError('cannot determine a safe current-user SID')
    start_at = start_at or datetime.now().astimezone() + timedelta(minutes=1)
    ET.register_namespace('', TASK_XML_NS)
    task = ET.Element(f'{{{TASK_XML_NS}}}Task', {'version': '1.4'})
    info = _child(task, 'RegistrationInfo')
    _child(info, 'Description', TASK_DESCRIPTION_PREFIX + registration['registration_id'])
    triggers = _child(task, 'Triggers')
    trigger = _child(triggers, 'CalendarTrigger')
    _child(trigger, 'StartBoundary', start_at.replace(microsecond=0).isoformat())
    _child(trigger, 'Enabled', 'true')
    repetition = _child(trigger, 'Repetition')
    _child(repetition, 'Interval', f"PT{registration['interval_seconds']}S")
    _child(repetition, 'Duration', 'P1D')
    _child(repetition, 'StopAtDurationEnd', 'false')
    daily = _child(trigger, 'ScheduleByDay')
    _child(daily, 'DaysInterval', '1')
    principals = _child(task, 'Principals')
    principal = _child(principals, 'Principal', id='Author')
    _child(principal, 'UserId', user_sid)
    _child(principal, 'LogonType', 'InteractiveToken')
    _child(principal, 'RunLevel', 'LeastPrivilege')
    settings = _child(task, 'Settings')
    _child(settings, 'MultipleInstancesPolicy', 'IgnoreNew')
    _child(settings, 'DisallowStartIfOnBatteries', 'false')
    _child(settings, 'StopIfGoingOnBatteries', 'false')
    _child(settings, 'AllowHardTerminate', 'true')
    _child(settings, 'StartWhenAvailable', 'true')
    _child(settings, 'RunOnlyIfNetworkAvailable', 'false')
    _child(settings, 'Enabled', 'true')
    _child(settings, 'Hidden', 'false')
    _child(settings, 'RunOnlyIfIdle', 'false')
    _child(settings, 'WakeToRun', 'false')
    _child(settings, 'ExecutionTimeLimit', 'PT10M')
    _child(settings, 'Priority', '7')
    actions = _child(task, 'Actions', Context='Author')
    execute = _child(actions, 'Exec')
    _child(execute, 'Command', registration['runner']['executable'])
    _child(execute, 'Arguments', _task_arguments(registration))
    return ET.tostring(task, encoding='utf-16', xml_declaration=True)


def inspect_task_xml(
    raw_xml, registration, expected_user_sid=None, expected_user_name=None,
    account_sid_resolver=None,
):
    try:
        root = ET.fromstring(raw_xml)
    except ET.ParseError as exc:
        raise WindowsSchedulerError('Task Scheduler returned malformed XML') from exc

    def text(path):
        node = root.find(path)
        return node.text if node is not None else None

    mismatches = []

    def check(field, expected, actual, equivalent, *, ownership=False, sensitive=False):
        if equivalent:
            return
        mismatches.append({
            'field': field,
            'expected': _safe_value(expected),
            'actual': _safe_value(actual, sensitive=sensitive),
            'ownership': ownership,
        })

    namespace = root.tag[1:].split('}', 1)[0] if root.tag.startswith('{') else None
    check('xml_namespace', TASK_XML_NS, namespace, namespace == TASK_XML_NS,
          ownership=True)

    description = text('.//{*}RegistrationInfo/{*}Description')
    marker = TASK_DESCRIPTION_PREFIX + registration['registration_id']
    check('description', marker, description, description == marker,
          ownership=True, sensitive=description != marker)

    principals = root.findall('.//{*}Principals/{*}Principal')
    check('principal_count', 1, len(principals), len(principals) == 1,
          ownership=True)
    user_id = text('.//{*}Principals/{*}Principal/{*}UserId')
    if expected_user_sid is not None:
        check(
            'user_id', expected_user_sid, user_id,
            _same_user(
                user_id, expected_user_sid, expected_user_name,
                account_sid_resolver,
            ),
            ownership=True,
        )

    action_container = root.find('.//{*}Actions')
    actions = list(action_container) if action_container is not None else []
    exec_actions = [item for item in actions if item.tag.rsplit('}', 1)[-1] == 'Exec']
    check('action_count', 1, len(actions), len(actions) == 1, ownership=True)
    check('exec_action_count', 1, len(exec_actions), len(exec_actions) == 1,
          ownership=True)
    command = text('.//{*}Actions/{*}Exec/{*}Command')
    arguments = text('.//{*}Actions/{*}Exec/{*}Arguments')
    working_directory = text('.//{*}Actions/{*}Exec/{*}WorkingDirectory')
    expected_executable = registration['runner']['executable']
    check(
        'executable', _windows_path(expected_executable), _windows_path(command),
        _paths_equivalent(expected_executable, command), ownership=True,
    )
    expected_arguments = registration['runner']['arguments']
    parsed_arguments = _argument_tokens(arguments)
    check(
        'arguments', ' '.join(expected_arguments), arguments,
        parsed_arguments == expected_arguments, ownership=True,
        sensitive=parsed_arguments != expected_arguments,
    )
    check(
        'working_directory', '<unset>', working_directory,
        working_directory is None or not working_directory.strip(),
        ownership=True, sensitive=bool(working_directory and working_directory.strip()),
    )

    triggers_container = root.find('.//{*}Triggers')
    triggers = list(triggers_container) if triggers_container is not None else []
    calendar_triggers = [
        item for item in triggers if item.tag.rsplit('}', 1)[-1] == 'CalendarTrigger'
    ]
    check('trigger_count', 1, len(triggers), len(triggers) == 1)
    check('calendar_trigger_count', 1, len(calendar_triggers),
          len(calendar_triggers) == 1)
    interval = _duration_seconds(text('.//{*}Triggers/*/{*}Repetition/{*}Interval'))
    check('interval_seconds', registration['interval_seconds'], interval,
          interval == registration['interval_seconds'])
    repetition_duration = _duration_seconds(
        text('.//{*}Triggers/*/{*}Repetition/{*}Duration')
    )
    check('repetition_duration_seconds', 86400, repetition_duration,
          repetition_duration == 86400)
    trigger_enabled = _boolean(text('.//{*}Triggers/*/{*}Enabled'), default=True)
    check('trigger_enabled', True, trigger_enabled, trigger_enabled is True)
    stop_at_end = _boolean(
        text('.//{*}Triggers/*/{*}Repetition/{*}StopAtDurationEnd'),
        default=False,
    )
    check('stop_at_duration_end', False, stop_at_end, stop_at_end is False)
    days_interval_raw = text('.//{*}Triggers/*/{*}ScheduleByDay/{*}DaysInterval')
    try:
        days_interval = int(days_interval_raw)
    except (TypeError, ValueError):
        days_interval = None
    check('days_interval', 1, days_interval, days_interval == 1)

    multiple = text('.//{*}Settings/{*}MultipleInstancesPolicy')
    check('multiple_instances_policy', 'IgnoreNew', multiple,
          _enum(multiple) == _enum('IgnoreNew'))
    start_available = _boolean(text('.//{*}Settings/{*}StartWhenAvailable'), default=False)
    check('start_when_available', True, start_available, start_available is True)
    logon_type = text('.//{*}Principals/{*}Principal/{*}LogonType')
    check('logon_type', 'InteractiveToken', logon_type,
          _enum(logon_type) == _enum('InteractiveToken'))
    run_level_node = root.find('.//{*}Principals/{*}Principal/{*}RunLevel')
    run_level = run_level_node.text if run_level_node is not None else None
    effective_run_level = (
        'LeastPrivilege' if run_level_node is None else run_level
    )
    check('run_level', 'LeastPrivilege', run_level,
          _enum(effective_run_level) == _enum('LeastPrivilege'))
    execution_limit = _duration_seconds(text('.//{*}Settings/{*}ExecutionTimeLimit'))
    check('execution_time_limit_seconds', 600, execution_limit,
          execution_limit == 600)

    ownership_mismatches = [item for item in mismatches if item['ownership']]
    configuration_mismatches = [item for item in mismatches if not item['ownership']]
    return {
        'exists': True,
        'owned': not ownership_mismatches,
        'matches': not mismatches,
        'mismatches': mismatches,
        'ownership_mismatches': ownership_mismatches,
        'configuration_mismatches': configuration_mismatches,
        'interval_seconds': interval,
        'task_state': 'registered',
        'next_scheduled_run': None,
        'raw_xml': raw_xml,
    }


class WindowsTaskScheduler:
    """Small, mockable schtasks XML boundary; never parses localized status text."""

    def __init__(self, executable=None, task_store=None):
        if os.name != 'nt':
            raise WindowsSchedulerError('automatic scheduler installation is Windows-only')
        system32 = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32'
        self.executable = executable
        if not self.executable:
            candidate = system32 / 'schtasks.exe'
            if candidate.is_file():
                self.executable = str(candidate)
            else:
                self.executable = shutil.which('schtasks.exe')
        if not self.executable:
            raise WindowsSchedulerError('schtasks.exe is unavailable')
        self.task_store = Path(task_store) if task_store is not None else (
            Path(self.executable).resolve().parent / 'Tasks'
        )
        if not self.task_store.is_absolute():
            raise WindowsSchedulerError('Task Scheduler store path must be absolute')

    def _run(self, arguments, timeout=30):
        try:
            return subprocess.run(
                [self.executable, *arguments], capture_output=True, check=False,
                timeout=timeout, shell=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise WindowsSchedulerError('Task Scheduler command failed or timed out') from exc

    def _current_user_identity(self):
        system_candidate = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32/whoami.exe'
        executable = str(system_candidate) if system_candidate.is_file() else (
            shutil.which('whoami.exe') or shutil.which('whoami')
        )
        if not executable:
            raise WindowsSchedulerError('whoami.exe is unavailable')
        try:
            result = subprocess.run(
                [executable, '/user', '/fo', 'csv', '/nh'], capture_output=True,
                check=False, timeout=15, shell=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise WindowsSchedulerError('cannot resolve current-user SID') from exc
        if result.returncode:
            raise WindowsSchedulerError('cannot resolve current-user SID')
        rows = list(csv.reader(io.StringIO(_decode_output(result.stdout))))
        if (len(rows) != 1 or len(rows[0]) < 2 or not rows[0][0]
                or not SID_RE.fullmatch(rows[0][1])):
            raise WindowsSchedulerError('whoami returned an invalid current-user SID')
        return {'name': rows[0][0], 'sid': rows[0][1]}

    def _current_user_sid(self):
        return self._current_user_identity()['sid']

    def inspect(self, registration):
        result = self._run(['/Query', '/TN', registration['task_name'], '/XML', 'ONE'])
        if result.returncode:
            task_file = self.task_store / registration['task_name']
            try:
                task_file.stat()
            except FileNotFoundError:
                pass
            except OSError as exc:
                raise WindowsSchedulerError(
                    'cannot distinguish a missing task from a Task Scheduler access failure'
                ) from exc
            else:
                raise WindowsSchedulerError(
                    'Task Scheduler query failed for an existing task'
                )
            return {
                'exists': False, 'owned': False, 'matches': False,
                'interval_seconds': None, 'task_state': 'missing',
                'next_scheduled_run': None, 'raw_xml': None,
            }
        identity = self._current_user_identity()
        return inspect_task_xml(
            _decode_output(result.stdout), registration,
            expected_user_sid=identity['sid'],
            expected_user_name=identity['name'],
            account_sid_resolver=_lookup_account_sid,
        )

    def create(self, registration, *, replace=False):
        raw = task_xml(registration, self._current_user_sid())
        descriptor, temporary = tempfile.mkstemp(prefix='project-system-task-', suffix='.xml')
        try:
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            arguments = ['/Create', '/TN', registration['task_name'], '/XML', temporary]
            if replace:
                arguments.append('/F')
            result = self._run(arguments)
            if result.returncode:
                raise WindowsSchedulerError('Task Scheduler rejected task creation')
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def restore(self, task_name, raw_xml):
        descriptor, temporary = tempfile.mkstemp(prefix='project-system-task-restore-', suffix='.xml')
        try:
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(raw_xml.encode('utf-16') if isinstance(raw_xml, str) else raw_xml)
                stream.flush()
                os.fsync(stream.fileno())
            result = self._run(['/Create', '/TN', task_name, '/XML', temporary, '/F'])
            if result.returncode:
                raise WindowsSchedulerError('Task Scheduler rollback failed')
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def delete(self, registration):
        result = self._run(['/Delete', '/TN', registration['task_name'], '/F'])
        if result.returncode:
            raise WindowsSchedulerError('Task Scheduler rejected task deletion')
