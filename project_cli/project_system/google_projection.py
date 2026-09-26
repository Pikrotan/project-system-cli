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


PROJECTION_TEXT = {
    'en': {
        'empty': 'No canonical information is recorded for this section.',
        'unresolved': '[UNRESOLVED] ',
        'id': 'ID',
        'status': 'Status',
        'unknown': 'unknown',
        'project_overview': 'Project Overview',
        'design_knowledge': 'Design Knowledge',
        'what_project_is': 'What this project is',
        'product_people_value': 'Product, people and value',
        'current_scope': 'Current scope',
        'current_state': 'Current state',
        'major_decisions': 'Major approved decisions',
        'approved_design_guidance': 'Approved design and product guidance',
        'screens_states': 'Screens and states',
        'flows_navigation': 'Flows and navigation',
        'users_roles_entities': 'Users, roles and related entities',
        'product_logic': 'Product logic, constraints and invariants',
        'recent_design_changes': 'Recent approved design-relevant changes',
        'open_design_questions': 'Open design questions \u2014 unresolved',
        'figma_references': 'Canonical Figma references',
        'readonly': 'This document is a read-only projection of canonical Git knowledge.',
        'manual_edits': 'Manual edits are not product decisions and may be replaced.',
        'source_commit': 'Canonical source commit',
        'projection_update': 'Last projection update',
    },
    'ru': {
        'empty': '\u0414\u043b\u044f \u044d\u0442\u043e\u0433\u043e \u0440\u0430\u0437\u0434\u0435\u043b\u0430 \u043d\u0435\u0442 \u0437\u0430\u0444\u0438\u043a\u0441\u0438\u0440\u043e\u0432\u0430\u043d\u043d\u043e\u0439 \u043a\u0430\u043d\u043e\u043d\u0438\u0447\u0435\u0441\u043a\u043e\u0439 \u0438\u043d\u0444\u043e\u0440\u043c\u0430\u0446\u0438\u0438.',
        'unresolved': '[\u041d\u0415 \u0420\u0415\u0428\u0415\u041d\u041e] ',
        'id': 'ID',
        'status': '\u0421\u0442\u0430\u0442\u0443\u0441',
        'unknown': '\u043d\u0435\u0438\u0437\u0432\u0435\u0441\u0442\u043d\u043e',
        'project_overview': '\u041e\u0431\u0437\u043e\u0440 \u043f\u0440\u043e\u0435\u043a\u0442\u0430',
        'design_knowledge': '\u0411\u0430\u0437\u0430 \u0437\u043d\u0430\u043d\u0438\u0439 \u043f\u043e \u0434\u0438\u0437\u0430\u0439\u043d\u0443',
        'what_project_is': '\u0427\u0442\u043e \u044d\u0442\u043e \u0437\u0430 \u043f\u0440\u043e\u0435\u043a\u0442',
        'product_people_value': '\u041f\u0440\u043e\u0434\u0443\u043a\u0442, \u043f\u043e\u043b\u044c\u0437\u043e\u0432\u0430\u0442\u0435\u043b\u0438 \u0438 \u0446\u0435\u043d\u043d\u043e\u0441\u0442\u044c',
        'current_scope': '\u0422\u0435\u043a\u0443\u0449\u0438\u0439 \u043e\u0431\u044a\u0451\u043c \u043f\u0440\u043e\u0435\u043a\u0442\u0430',
        'current_state': '\u0422\u0435\u043a\u0443\u0449\u0435\u0435 \u0441\u043e\u0441\u0442\u043e\u044f\u043d\u0438\u0435',
        'major_decisions': '\u041a\u043b\u044e\u0447\u0435\u0432\u044b\u0435 \u0443\u0442\u0432\u0435\u0440\u0436\u0434\u0451\u043d\u043d\u044b\u0435 \u0440\u0435\u0448\u0435\u043d\u0438\u044f',
        'approved_design_guidance': '\u0423\u0442\u0432\u0435\u0440\u0436\u0434\u0451\u043d\u043d\u044b\u0435 \u043f\u0440\u0430\u0432\u0438\u043b\u0430 \u0434\u0438\u0437\u0430\u0439\u043d\u0430 \u0438 \u043f\u0440\u043e\u0434\u0443\u043a\u0442\u0430',
        'screens_states': '\u042d\u043a\u0440\u0430\u043d\u044b \u0438 \u0441\u043e\u0441\u0442\u043e\u044f\u043d\u0438\u044f',
        'flows_navigation': '\u0421\u0446\u0435\u043d\u0430\u0440\u0438\u0438 \u0438 \u043d\u0430\u0432\u0438\u0433\u0430\u0446\u0438\u044f',
        'users_roles_entities': '\u041f\u043e\u043b\u044c\u0437\u043e\u0432\u0430\u0442\u0435\u043b\u0438, \u0440\u043e\u043b\u0438 \u0438 \u0441\u0432\u044f\u0437\u0430\u043d\u043d\u044b\u0435 \u0441\u0443\u0449\u043d\u043e\u0441\u0442\u0438',
        'product_logic': '\u041b\u043e\u0433\u0438\u043a\u0430 \u043f\u0440\u043e\u0434\u0443\u043a\u0442\u0430, \u043e\u0433\u0440\u0430\u043d\u0438\u0447\u0435\u043d\u0438\u044f \u0438 \u0438\u043d\u0432\u0430\u0440\u0438\u0430\u043d\u0442\u044b',
        'recent_design_changes': '\u041d\u0435\u0434\u0430\u0432\u043d\u0438\u0435 \u0443\u0442\u0432\u0435\u0440\u0436\u0434\u0451\u043d\u043d\u044b\u0435 \u0438\u0437\u043c\u0435\u043d\u0435\u043d\u0438\u044f, \u0432\u0430\u0436\u043d\u044b\u0435 \u0434\u043b\u044f \u0434\u0438\u0437\u0430\u0439\u043d\u0430',
        'open_design_questions': '\u041e\u0442\u043a\u0440\u044b\u0442\u044b\u0435 \u0432\u043e\u043f\u0440\u043e\u0441\u044b \u043f\u043e \u0434\u0438\u0437\u0430\u0439\u043d\u0443 \u2014 \u043d\u0435 \u0440\u0435\u0448\u0435\u043d\u043e',
        'figma_references': '\u041a\u0430\u043d\u043e\u043d\u0438\u0447\u0435\u0441\u043a\u0438\u0435 \u0441\u0441\u044b\u043b\u043a\u0438 \u043d\u0430 Figma',
        'readonly': '\u042d\u0442\u043e\u0442 \u0434\u043e\u043a\u0443\u043c\u0435\u043d\u0442 \u2014 \u0434\u043e\u0441\u0442\u0443\u043f\u043d\u0430\u044f \u0442\u043e\u043b\u044c\u043a\u043e \u0434\u043b\u044f \u0447\u0442\u0435\u043d\u0438\u044f \u043f\u0440\u043e\u0435\u043a\u0446\u0438\u044f \u043a\u0430\u043d\u043e\u043d\u0438\u0447\u0435\u0441\u043a\u0438\u0445 \u0437\u043d\u0430\u043d\u0438\u0439 \u0438\u0437 Git.',
        'manual_edits': '\u0420\u0443\u0447\u043d\u044b\u0435 \u0438\u0437\u043c\u0435\u043d\u0435\u043d\u0438\u044f \u043d\u0435 \u044f\u0432\u043b\u044f\u044e\u0442\u0441\u044f \u043f\u0440\u043e\u0434\u0443\u043a\u0442\u043e\u0432\u044b\u043c\u0438 \u0440\u0435\u0448\u0435\u043d\u0438\u044f\u043c\u0438 \u0438 \u043c\u043e\u0433\u0443\u0442 \u0431\u044b\u0442\u044c \u043f\u0435\u0440\u0435\u0437\u0430\u043f\u0438\u0441\u0430\u043d\u044b.',
        'source_commit': '\u041a\u043e\u043c\u043c\u0438\u0442 \u043a\u0430\u043d\u043e\u043d\u0438\u0447\u0435\u0441\u043a\u043e\u0433\u043e \u0438\u0441\u0442\u043e\u0447\u043d\u0438\u043a\u0430',
        'projection_update': '\u041f\u043e\u0441\u043b\u0435\u0434\u043d\u0435\u0435 \u043e\u0431\u043d\u043e\u0432\u043b\u0435\u043d\u0438\u0435 \u043f\u0440\u043e\u0435\u043a\u0446\u0438\u0438',
    },
}

RU_DESIGN_DOC_TITLES = {
    'docs/design/DESIGN_OVERVIEW.md': '\u041e\u0431\u0437\u043e\u0440 \u0434\u0438\u0437\u0430\u0439\u043d\u0430',
    'docs/product/PRODUCT_MODEL.md': '\u041c\u043e\u0434\u0435\u043b\u044c \u043f\u0440\u043e\u0434\u0443\u043a\u0442\u0430',
    'docs/product/USER_FLOWS.md': '\u041f\u043e\u043b\u044c\u0437\u043e\u0432\u0430\u0442\u0435\u043b\u044c\u0441\u043a\u0438\u0435 \u0441\u0446\u0435\u043d\u0430\u0440\u0438\u0438',
    'docs/product/UX_RULES.md': '\u041f\u0440\u0430\u0432\u0438\u043b\u0430 UX',
    'docs/product/DOMAIN_RULES.md': '\u041f\u0440\u0430\u0432\u0438\u043b\u0430 \u043f\u0440\u0435\u0434\u043c\u0435\u0442\u043d\u043e\u0439 \u043e\u0431\u043b\u0430\u0441\u0442\u0438',
    'docs/03_PRODUCT.md': '\u041f\u0440\u043e\u0434\u0443\u043a\u0442',
}

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


def _source_hash(files, objects, *, language='en'):
    digest = sha256()
    if language != 'en':
        digest.update(
            b'projection_language\0' + language.encode('utf-8') + b'\0'
        )
    for relative, raw in files:
        digest.update(relative.encode('utf-8') + b'\0' + raw + b'\0')
    for record in sorted(objects, key=lambda item: item.path.as_posix()):
        digest.update(record.path.as_posix().encode('utf-8') + b'\0')
        digest.update(json.dumps(
            record.data, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
        ).encode('utf-8'))
        digest.update(b'\0' + record.body.encode('utf-8') + b'\0')
    return digest.hexdigest()


def _projection_text(language):
    try:
        return PROJECTION_TEXT[language]
    except KeyError as exc:
        raise GoogleProjectionError(
            f'unsupported projection language: {language}'
        ) from exc


def _design_doc_title(relative, language):
    if language == 'ru':
        return RU_DESIGN_DOC_TITLES.get(
            relative,
            Path(relative).stem.replace('_', ' ').title(),
        )
    return Path(relative).stem.replace('_', ' ').title()


def _current(record):
    return str(record.data.get('status', '')).lower() in CURRENT


def _object_text(record, *, unresolved=False, language='en'):
    labels = _projection_text(language)
    data = record.data
    title = str(data.get('title') or data.get('id'))
    marker = labels['unresolved'] if unresolved else ''
    lines = [
        f'{marker}{title}',
        f'{labels["id"]}: {data.get("id")}',
        f'{labels["status"]}: {data.get("status", labels["unknown"])}',
    ]
    body = _plain_markdown(record.body)
    if body:
        lines += ['', body]
    return '\n'.join(lines)


def _section(title, content, *, language='en'):
    content = content.strip()
    empty = _projection_text(language)['empty']
    return f'{title}\n{"=" * len(title)}\n\n{content or empty}'


def _canonical_preflight(root):
    fatal = [item for item in validate(root) if item[0] in {'BLOCKING', 'ERROR'}]
    if fatal:
        raise GoogleProjectionError(
            f'canonical project validation blocks projection ({len(fatal)} issue(s))'
        )
    layer = load_object_layer(root)
    return layer.records


def build_projection(
    root, role, *, projected_at=None, source_commit=None, language='en',
):
    root = Path(root).resolve()
    labels = _projection_text(language)
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
            _section(labels['what_project_is'], _embedded_document_markdown(by_path.get(OVERVIEW_DOCS[0], b'').decode('utf-8')), language=language),
            _section(labels['product_people_value'], _embedded_document_markdown(by_path.get(OVERVIEW_DOCS[1], b'').decode('utf-8')), language=language),
            _section(labels['current_scope'], _embedded_document_markdown(by_path.get(OVERVIEW_DOCS[2], b'').decode('utf-8')), language=language),
            _section(labels['current_state'], _embedded_document_markdown(by_path.get(OVERVIEW_DOCS[3], b'').decode('utf-8')), language=language),
            _section(labels['major_decisions'], '\n\n'.join(_object_text(item, language=language) for item in current_decisions), language=language),
        ]
        title = f'{name} — {labels["project_overview"]}'
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
            _section(_design_doc_title(relative, language), _embedded_document_markdown(raw.decode('utf-8')), language=language)
            for relative, raw in files
        )
        all_text = canonical_docs + '\n' + '\n'.join(
            json.dumps(item.data, ensure_ascii=False, sort_keys=True) + '\n' + item.body
            for item in current + questions
        )
        figma = sorted(set(FIGMA_RE.findall(all_text)))
        sections = [
            _section(labels['approved_design_guidance'], canonical_docs, language=language),
            _section(labels['screens_states'], '\n\n'.join(_object_text(item, language=language) for item in screens), language=language),
            _section(labels['flows_navigation'], '\n\n'.join(_object_text(item, language=language) for item in flows), language=language),
            _section(labels['users_roles_entities'], '\n\n'.join(_object_text(item, language=language) for item in entities), language=language),
            _section(labels['product_logic'], '\n\n'.join(_object_text(item, language=language) for item in rules), language=language),
            _section(labels['recent_design_changes'], '\n\n'.join(_object_text(item, language=language) for item in changes), language=language),
            _section(labels['open_design_questions'], '\n\n'.join(_object_text(item, unresolved=True, language=language) for item in questions), language=language),
            _section(labels['figma_references'], '\n'.join(f'- {url}' for url in figma), language=language),
        ]
        title = f'{name} — {labels["design_knowledge"]}'
        objects = current + questions
    else:
        raise GoogleProjectionError(f'unsupported projection role: {role}')

    preamble = (
        f'{title}\n{"=" * len(title)}\n\n'
        f'{labels["readonly"]}\n'
        f'{labels["manual_edits"]}\n\n'
        f'{labels["source_commit"]}: {source_commit}\n'
        f'{labels["projection_update"]}: {projected_at}\n'
    )
    text = normalize_document_text(preamble + '\n\n' + '\n\n'.join(sections))
    return {
        'role': role,
        'source_commit': source_commit,
        'source_sha256': _source_hash(files, objects, language=language),
        'content_sha256': content_sha256(text),
        'projected_at': projected_at,
        'text': text,
    }
