"""No-console entry point for one bounded scheduled automatic SYNC cycle."""
import sys

from .process_runner import background_process_context
from .sync_auto import (
    REGISTRATION_ID_RE, SyncAutoError, record_background_failure, run_auto,
)


def main(argv=None, *, run=None, recorder=None):
    """Run without terminal I/O and return a Task Scheduler process exit code."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    if (len(arguments) != 2 or arguments[0] != '--registration'
            or not REGISTRATION_ID_RE.fullmatch(arguments[1])):
        return 2
    registration_id = arguments[1]
    run = run or run_auto
    recorder = recorder or record_background_failure
    try:
        with background_process_context():
            run(registration_id)
    except SyncAutoError as exc:
        try:
            recorder(registration_id, 'scheduled_run_error')
        except BaseException:
            pass
        return exc.exit_code
    except BaseException:
        try:
            recorder(registration_id, 'background_runner_exception')
        except BaseException:
            pass
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
