"""Safe top-level namespace ownership for durable Stage 13 intake artifacts."""

from dataclasses import dataclass
import os
from pathlib import Path, PurePosixPath
import stat

from .source_layer import SourceError, checked_path


KNOWN_INTAKE_DIRECTORIES = (
    'representations',
    'extraction-contracts',
    'extraction-runs',
    'proposals',
    'extraction-submissions',
)


class IntakeError(SourceError):
    """The durable intake namespace cannot be trusted."""


def intake_path(root, relative):
    pure = PurePosixPath(relative)
    if (pure.is_absolute() or pure.as_posix() != relative or not pure.parts
            or pure.parts[0] != 'intake' or any(part in {'.', '..'} for part in pure.parts)
            or any(char in relative for char in '\\:\x00')):
        raise IntakeError('unsafe intake storage path')
    try:
        return checked_path(Path(root).absolute().joinpath(*pure.parts))
    except SourceError as exc:
        raise IntakeError(str(exc)) from exc


@dataclass(frozen=True)
class IntakeLayer:
    issues: tuple


def inspect_intake_layer(root):
    """Validate only the known top-level intake namespace, never artifact semantics."""
    root = Path(root).absolute()
    try:
        info = os.lstat(root / 'intake')
    except FileNotFoundError:
        return IntakeLayer(())
    except OSError:
        return IntakeLayer((('ERROR', 'intake', 'intake layer cannot be inspected'),))
    try:
        directory = intake_path(root, 'intake')
        if not stat.S_ISDIR(info.st_mode) or not directory.is_dir():
            raise IntakeError('intake storage is not a directory')
        entries = []
        for child in directory.iterdir():
            relative = 'intake/' + child.name
            try:
                safe = intake_path(root, relative)
                entries.append((relative, os.lstat(safe)))
            except (IntakeError, OSError, ValueError):
                entries.append((relative, None))
    except (IntakeError, OSError, ValueError):
        return IntakeLayer((('ERROR', 'intake', 'intake layer cannot be inspected safely'),))

    issues = []
    allowed = {'intake/' + name for name in KNOWN_INTAKE_DIRECTORIES}
    for relative, child_info in sorted(entries):
        if child_info is None:
            issues.append(('ERROR', relative, 'unsafe intake layer entry'))
        elif relative not in allowed:
            issues.append(('ERROR', relative, 'unexpected intake layer entry'))
        elif not stat.S_ISDIR(child_info.st_mode):
            issues.append(('ERROR', relative, 'intake layer section must be a directory'))
    return IntakeLayer(tuple(sorted(set(issues))))
