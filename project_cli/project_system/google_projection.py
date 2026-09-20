"""Deterministic, one-way canonical Git knowledge projections for Google Docs."""
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import re

from .google_credentials import GoogleError
from .object_loader import load_object_layer
from .sync_planning import _git_head
from .utils import load_yaml
from .validation import validate


OVERVIEW_DOCS = (
    'docs/01_VISION.md',
    'docs/03_PRODUCT.md',
    'docs/02_SCOPE.md',
    'docs/00_CURRENT_STATE.md',
)
DESIGN_DOCS = (
    'docs/design/DESIGN_OVERVIEW.md',
    'docs/product/PRODUCT_MODEL.md',
    'docs/product/USER_FLOWS.md',
    'docs/product/UX_RULES.md',
    'docs/product/DOMAIN_RULES.md',
    'docs/03_PRODUCT.md',
)
NONCURRENT = {
    'superseded', 'deprecated', 'removed', 'rejected', 'cancelled', 'archived',
}
CURRENT = {
    'active', 'approved', 'planned', 'in_progress', 'implemented', 'shipped',
    'accepted', 'resolved', 'mitigated', 'open',
}
QUESTION_CURRENT = {'open', 'proposed', 'active', 'in_progress'}
FIGMA_RE = re.compile(r'https://(?:www\.)?figma\.com/[^\s)>\]}]+', re.IGNORECASE)


class GoogleProjectionError(GoogleError):
    exit_code = 8
    category = 'google_projection'


def _iso(value=None):
    value = value or datetime.now(timezone.utc)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError as exc:
            raise GoogleProjectionError('projection time is invalid') from exc
        value = parsed
    if value.tzinfo is None or value.utcoffset() is None:
        raise GoogleProjectionError('projection time must include a timezone')
    return value.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def normalize_document_text(text):
    return str(text).replace('\r\n', '\n').replace('\r', '\n').rstrip('\n') + '\n'


def content_sha256(text):
    return sha256(normalize_document_text(text).encode('utf-8')).hexdigest()


def _plain_markdown(text):
    lines = []
    in_code = False
    for raw in normalize_document_text(text).splitlines():
        if raw.strip().startswith('```'):
            in_code = not in_code
            continue
        if in_code:
            continue
        line = re.sub(r'^#{1,6}\s+', '', raw)
        line = re.sub(r'\[([^]]+)\]\((https?://[^)]+)\)', r'\1 — \2', line)
        line = line.replace('**', '').replace('__', '').replace('`', '')
        lines.append(line.rstrip())
    while lines and not lines[-1]:
        lines.pop()
    return '\n'.join(lines).strip()


def _embedded_document_markdown(text):
    """Flatten one canonical document after removing only its leading H1."""
    lines = normalize_document_text(text).splitlines()
    first = next((index for index, line in enumerate(lines) if line.strip()), None)
    if first is not None and re.fullmatch(r'#\s+\S.*', lines[first].strip()):
        del lines[first]
        while first < len(lines) and not lines[first].strip():
            del lines[first]
    return _plain_markdown('\n'.join(lines))


def _read_sources(root, relative_paths):
    result = []
    for relative in relative_paths:
        path = root / relative
        if not path.exists():
            continue
        if not path.is_file() or path.is_symlink() or getattr(path, 'is_junction', lambda: False)():
            raise GoogleProjectionError(f'projection source is not a safe regular file: {relative}')
        result.append((relative, path.read_bytes()))
    return result


def _source_hash(files, objects):
    digest = sha256()
    for relative, raw in files:
        digest.update(relative.encode('utf-8') + b'\0' + raw + b'\0')
    for record in sorted(objects, key=lambda item: item.path.as_posix()):
        digest.update(record.path.as_posix().encode('utf-8') + b'\0')
        digest.update(json.dumps(
            record.data, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
        ).encode('utf-8'))
        digest.update(b'\0' + record.body.encode('utf-8') + b'\0')
    return digest.hexdigest()


def _current(record):
    return str(record.data.get('status', '')).lower() in CURRENT


def _object_text(record, *, unresolved=False):
    data = record.data
    title = str(data.get('title') or data.get('id'))
    marker = '[UNRESOLVED] ' if unresolved else ''
    lines = [f'{marker}{title}', f'ID: {data.get("id")}', f'Status: {data.get("status", "unknown")}']
    body = _plain_markdown(record.body)
    if body:
        lines += ['', body]
    return '\n'.join(lines)


def _section(title, content):
    content = content.strip()
    return f'{title}\n{"=" * len(title)}\n\n{content or "No canonical information is recorded for this section."}'


def _canonical_preflight(root):
    fatal = [item for item in validate(root) if item[0] in {'BLOCKING', 'ERROR'}]
    if fatal:
        raise GoogleProjectionError(
            f'canonical project validation blocks projection ({len(fatal)} issue(s))'
        )
    layer = load_object_layer(root)
    return layer.records


def build_projection(root, role, *, projected_at=None, source_commit=None):
    root = Path(root).resolve()
    config = load_yaml(root / 'project.yaml')
    project = config.get('project') or {}
    name = project.get('name') or project.get('id') or 'Project'
    records = _canonical_preflight(root)
    source_commit = source_commit or _git_head(root)
    projected_at = _iso(projected_at)

    if role == 'project_overview':
        files = _read_sources(root, OVERVIEW_DOCS)
        by_path = {relative: raw for relative, raw in files}
        current_decisions = [
            record for record in records
            if record.data.get('type') == 'decision'
            and str(record.data.get('status', '')).lower() in {'active', 'approved', 'accepted'}
        ]
        sections = [
            _section('What this project is', _embedded_document_markdown(by_path.get(OVERVIEW_DOCS[0], b'').decode('utf-8'))),
            _section('Product, people and value', _embedded_document_markdown(by_path.get(OVERVIEW_DOCS[1], b'').decode('utf-8'))),
            _section('Current scope', _embedded_document_markdown(by_path.get(OVERVIEW_DOCS[2], b'').decode('utf-8'))),
            _section('Current state', _embedded_document_markdown(by_path.get(OVERVIEW_DOCS[3], b'').decode('utf-8'))),
            _section('Major approved decisions', '\n\n'.join(_object_text(item) for item in current_decisions)),
        ]
        title = f'{name} — Project Overview'
        objects = current_decisions
    elif role == 'design_knowledge':
        files = _read_sources(root, DESIGN_DOCS)
        current = [
            record for record in records
            if record.data.get('type') != 'question' and _current(record)
        ]
        screens = [item for item in current if item.data.get('type') == 'screen']
        flows = [item for item in current if item.data.get('type') == 'flow']
        entities = [item for item in current if item.data.get('type') == 'entity']
        rules = [item for item in current if item.data.get('type') in {'decision', 'requirement', 'feature'}]
        changes = [item for item in current if item.data.get('type') == 'design_change'][-20:]
        questions = [
            item for item in records
            if item.data.get('type') == 'question'
            and str(item.data.get('status', '')).lower() in QUESTION_CURRENT
        ]
        canonical_docs = '\n\n'.join(
            _section(Path(relative).stem.replace('_', ' ').title(), _embedded_document_markdown(raw.decode('utf-8')))
            for relative, raw in files
        )
        all_text = canonical_docs + '\n' + '\n'.join(
            json.dumps(item.data, ensure_ascii=False, sort_keys=True) + '\n' + item.body
            for item in current + questions
        )
        figma = sorted(set(FIGMA_RE.findall(all_text)))
        sections = [
            _section('Approved design and product guidance', canonical_docs),
            _section('Screens and states', '\n\n'.join(_object_text(item) for item in screens)),
            _section('Flows and navigation', '\n\n'.join(_object_text(item) for item in flows)),
            _section('Users, roles and related entities', '\n\n'.join(_object_text(item) for item in entities)),
            _section('Product logic, constraints and invariants', '\n\n'.join(_object_text(item) for item in rules)),
            _section('Recent approved design-relevant changes', '\n\n'.join(_object_text(item) for item in changes)),
            _section('Open design questions — unresolved', '\n\n'.join(_object_text(item, unresolved=True) for item in questions)),
            _section('Canonical Figma references', '\n'.join(f'- {url}' for url in figma)),
        ]
        title = f'{name} — Design Knowledge'
        objects = current + questions
    else:
        raise GoogleProjectionError(f'unsupported projection role: {role}')

    preamble = (
        f'{title}\n{"=" * len(title)}\n\n'
        'This document is a read-only projection of canonical Git knowledge.\n'
        'Manual edits are not product decisions and may be replaced.\n\n'
        f'Canonical source commit: {source_commit}\n'
        f'Last projection update: {projected_at}\n'
    )
    text = normalize_document_text(preamble + '\n\n' + '\n\n'.join(sections))
    return {
        'role': role,
        'source_commit': source_commit,
        'source_sha256': _source_hash(files, objects),
        'content_sha256': content_sha256(text),
        'projected_at': projected_at,
        'text': text,
    }
