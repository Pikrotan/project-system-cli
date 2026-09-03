from collections import Counter
from dataclasses import dataclass
from pathlib import Path
import re

from .frontmatter import read_object
from .utils import ID_RE


TYPE_DIRECTORIES = {
    'decision': 'decisions',
    'requirement': 'requirements',
    'feature': 'features',
    'question': 'questions',
    'risk': 'risks',
    'experiment': 'experiments',
    'screen': 'screens',
    'flow': 'flows',
    'entity': 'entities',
    'metric': 'metrics',
    'design_change': 'design_changes',
    'debt': 'debts',
}
ATOMIC_DIRECTORIES = frozenset(TYPE_DIRECTORIES.values())
OBJECT_FILENAME_RE = re.compile(
    rf'^(?P<id>{ID_RE.pattern.removeprefix("^").removesuffix("$")})'
    r'(?:-(?P<slug>[a-z0-9_](?:[a-z0-9_-]*[a-z0-9_])?))?$'
)


@dataclass(frozen=True)
class ObjectRecord:
    path: Path
    data: dict
    body: str
    filename_id: str | None


@dataclass(frozen=True)
class ObjectLoadError:
    path: Path
    error: Exception


@dataclass
class ObjectLayer:
    paths: list[Path]
    records: list[ObjectRecord]
    objects: dict
    errors: list[ObjectLoadError]
    unsupported_paths: list[Path]
    has_content: bool

    def counts_by_type(self):
        return Counter(item['data'].get('type', 'unknown') for item in self.objects.values())


def _knowledge_files(root):
    knowledge = Path(root) / 'knowledge'
    if not knowledge.exists():
        return []
    return sorted((path for path in knowledge.rglob('*') if path.is_file()), key=lambda path: str(path))


def _is_atomic_directory_file(path, knowledge):
    relative = path.relative_to(knowledge)
    return bool(relative.parts) and relative.parts[0] in ATOMIC_DIRECTORIES


def object_id_from_filename(path):
    """Return the identity prefix from an ID.md or ID-slug.md filename."""
    match = OBJECT_FILENAME_RE.fullmatch(Path(path).stem)
    return match.group('id') if match else None


def load_object_layer(root):
    """Load the canonical atomic knowledge layer from Markdown objects.

    This is the single discovery and parsing boundary used by validation,
    generation, context/task targeting, graph operations, and ID collision
    detection. Parsing failures remain visible to callers instead of being
    silently discarded.
    """
    root = Path(root)
    knowledge = root / 'knowledge'
    files = _knowledge_files(root)
    paths = [
        path for path in files
        if path.suffix.lower() == '.md' and _is_atomic_directory_file(path, knowledge)
    ]
    unsupported_paths = [
        path for path in files
        if path.suffix.lower() in {'.yaml', '.yml'} and _is_atomic_directory_file(path, knowledge)
    ]
    has_content = any(path.name != '.gitkeep' for path in files)

    records = []
    errors = []
    objects = {}
    for path in paths:
        try:
            data, body = read_object(path)
        except Exception as exc:
            errors.append(ObjectLoadError(path, exc))
            continue
        record = ObjectRecord(path, data, body, object_id_from_filename(path))
        records.append(record)
        object_id = data.get('id')
        if object_id and object_id not in objects:
            objects[object_id] = {'data': data, 'path': path, 'body': body}

    return ObjectLayer(paths, records, objects, errors, unsupported_paths, has_content)
