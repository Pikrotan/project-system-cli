"""Deterministic Design Changes intake without semantic interpretation."""
from datetime import datetime, timezone
from hashlib import sha256
import json
import re
import secrets
from urllib.parse import urlsplit

from .google_credentials import GoogleError
from .google_bindings import (
    CHANGE_ID_RE, GoogleBindingError, load_design_import, write_design_import,
)
from .sync_intake import intake_bindings
from .utils import ID_RE


DESIGNER_COLUMNS = (
    'Date', 'Author', 'Project Area', 'Screen / Flow', 'Change Type',
    'What Changed', 'Why', 'Figma URL', 'Logic Changed?',
)
SYSTEM_COLUMNS = (
    'Change ID', 'Import Status', 'Decision Needed?', 'Canonical Objects',
    'Applied In', 'Processing Error', 'Notes',
)
DESIGN_CHANGE_HEADERS = DESIGNER_COLUMNS + SYSTEM_COLUMNS
PAYLOAD_FIELDS = (
    'date', 'author', 'project_area', 'screen_flow', 'change_type',
    'what_changed', 'why', 'figma_url', 'logic_changed',
)


class GoogleDesignError(GoogleError):
    exit_code = 8
    category = 'google_design_change'


def _iso(value=None):
    value = value or datetime.now(timezone.utc)
    if value.tzinfo is None or value.utcoffset() is None:
        raise GoogleDesignError('Design Change import time must include a timezone')
    return value.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def _cells(values):
    return [str(value).strip() if value is not None else '' for value in values] + [''] * 16


def designer_payload(values):
    cells = _cells(values)
    return dict(zip(PAYLOAD_FIELDS, cells[:9]))


def designer_payload_sha256(payload):
    raw = json.dumps(
        payload, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
    ).encode('utf-8')
    return sha256(raw).hexdigest()


def row_designer_hash(values):
    return designer_payload_sha256(designer_payload(values))


def _figma_url(value):
    if not value:
        return True
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return parsed.scheme == 'https' and (parsed.hostname or '').lower() in {'figma.com', 'www.figma.com'}


def validate_designer_payload(payload):
    errors = []
    if not payload['what_changed']:
        errors.append('What Changed is required; a Figma URL alone is insufficient')
    if payload['logic_changed'] not in {'Yes', 'No', 'Unsure'}:
        errors.append('Logic Changed? must be Yes, No, or Unsure')
    if not _figma_url(payload['figma_url']):
        errors.append('Figma URL must be an https://figma.com URL')
    return errors


def _change_id(now):
    return f'GDES-{now:%Y%m%d}-{secrets.token_hex(4)}'


def _status_fields(status, *, decision='', objects='', applied='', error='', notes=''):
    return {
        'status': status,
        'decision_needed': decision,
        'canonical_objects': objects,
        'applied_in': applied,
        'processing_error': error,
        'notes': notes,
    }


def _canonical_ids(pack):
    result = set()
    for value in pack.get('expected_targets', []):
        if ID_RE.fullmatch(str(value)):
            result.add(value)
    for change in pack.get('changes', []):
        for value in (change.get('target_id'), (change.get('object') or {}).get('id')):
            if value and ID_RE.fullmatch(str(value)):
                result.add(value)
    return sorted(result)


def lifecycle_feedback(root):
    """Map human-controlled SYNC lifecycle evidence by Google Change ID."""
    matches = {}
    for binding in intake_bindings(root):
        source = binding.pack.get('source') or {}
        change_id = source.get('ref') if source.get('type') == 'google_design_change' else None
        if not change_id or not CHANGE_ID_RE.fullmatch(str(change_id)):
            continue
        matches.setdefault(change_id, []).append(binding)
    result = {}
    for change_id, bindings in matches.items():
        if len(bindings) != 1:
            result[change_id] = _status_fields(
                'CONFLICT', error='multiple SYNC packs reference this Change ID',
            )
            continue
        binding = bindings[0]
        objects = ', '.join(_canonical_ids(binding.pack))
        if binding.state != 'completed':
            result[change_id] = _status_fields(
                'IN REVIEW', decision='YES', objects=objects,
                applied=f'pack:{binding.pack["pack_id"]}',
            )
            continue
        terminal = binding.terminal['terminal']
        outcome = terminal['outcome']
        if outcome == 'pushed':
            result[change_id] = _status_fields(
                'APPLIED', decision='NO', objects=objects,
                applied=f'commit:{terminal["commit_sha"]}; pack:{binding.pack["pack_id"]}',
            )
        elif outcome == 'reviewed-no-change':
            result[change_id] = _status_fields(
                'REVIEWED — NO CANONICAL CHANGE', decision='NO',
                applied=f'pack:{binding.pack["pack_id"]}', notes=terminal.get('reason') or '',
            )
        else:
            result[change_id] = _status_fields(
                outcome.upper(), decision='NO', applied=f'pack:{binding.pack["pack_id"]}',
                notes=terminal.get('reason') or '',
            )
    return result


def process_design_changes(root, binding, gateway, *, clock=None):
    """Import each valid row once; never create a pack or canonical edit."""
    clock = clock or (lambda: datetime.now(timezone.utc))
    resource = binding['resources'].get('design_changes')
    if resource is None:
        return {'status': 'disabled', 'imported': 0, 'reused': 0, 'invalid': 0, 'conflicts': 0}
    spreadsheet_id = resource['id']
    rows = gateway.read_design_rows(spreadsheet_id)
    nonempty = []
    seen = {}
    for row in rows:
        cells = _cells(row.get('values', []))
        if not any(cells[:16]):
            continue
        number = row.get('row_number')
        if type(number) is not int or number < 2:
            raise GoogleDesignError('Design Changes returned an invalid row number')
        change_id = cells[9]
        if change_id:
            seen.setdefault(change_id, []).append(number)
        nonempty.append((number, cells))
    duplicates = {key: value for key, value in seen.items() if len(value) > 1}
    if duplicates:
        raise GoogleDesignError('duplicate Change ID in Design Changes sheet')

    feedback = lifecycle_feedback(root)
    counts = {'imported': 0, 'reused': 0, 'invalid': 0, 'conflicts': 0}
    for number, cells in nonempty:
        payload = designer_payload(cells)
        payload_hash = designer_payload_sha256(payload)
        change_id = cells[9]
        if not change_id:
            for _ in range(32):
                change_id = _change_id(clock())
                if change_id not in seen and load_design_import(root, change_id) is None:
                    break
            else:
                raise GoogleDesignError('cannot allocate a collision-free Design Change ID')
            seen[change_id] = [number]
            claimed = gateway.claim_change_id(
                spreadsheet_id, number, payload_hash, change_id, row_designer_hash,
            )
            claimed_number = claimed.get('row_number') if isinstance(claimed, dict) else None
            if type(claimed_number) is not int or claimed_number < 2:
                raise GoogleDesignError('Design Changes identity claim returned an invalid row number')
            number = claimed_number
        if not CHANGE_ID_RE.fullmatch(change_id):
            gateway.update_design_status(
                spreadsheet_id, change_id,
                _status_fields('ERROR', error='Change ID is malformed'),
            )
            counts['invalid'] += 1
            continue

        existing = load_design_import(root, change_id)
        if existing is not None:
            if existing['designer_payload_sha256'] != payload_hash:
                gateway.update_design_status(
                    spreadsheet_id, change_id,
                    _status_fields(
                        'CONFLICT', error='row edited after import; immutable original preserved',
                    ),
                )
                counts['conflicts'] += 1
                continue
            gateway.update_design_status(
                spreadsheet_id, change_id,
                feedback.get(change_id) or _status_fields(
                    'IMPORTED — OWNER REVIEW',
                    decision='PENDING',
                ),
            )
            counts['reused'] += 1
            continue

        errors = validate_designer_payload(payload)
        if errors:
            gateway.update_design_status(
                spreadsheet_id, change_id,
                _status_fields(
                    'ERROR', decision='PENDING', error='; '.join(errors),
                ),
            )
            counts['invalid'] += 1
            continue
        confirmed = gateway.read_design_row(spreadsheet_id, number)
        confirmed_cells = _cells(confirmed.get('values', []))
        if confirmed_cells[9] != change_id or row_designer_hash(confirmed_cells) != payload_hash:
            gateway.update_design_status(
                spreadsheet_id, change_id,
                _status_fields('CONFLICT', error='row edited during import; retry after review'),
            )
            counts['conflicts'] += 1
            continue
        document = {
            'schema_version': 1,
            'record_type': 'google_design_change_import',
            'change_id': change_id,
            'project_id': binding['project_id'],
            'repository': binding['repository'],
            'spreadsheet_id': spreadsheet_id,
            'initial_row_number': number,
            'imported_at': _iso(clock()),
            'designer_payload': payload,
            'designer_payload_sha256': payload_hash,
        }
        try:
            _, created = write_design_import(root, document)
        except GoogleBindingError:
            raise
        gateway.update_design_status(
            spreadsheet_id, change_id,
            feedback.get(change_id) or _status_fields(
                'IMPORTED — OWNER REVIEW',
                decision='PENDING',
            ),
        )
        counts['imported' if created else 'reused'] += 1
    return {
        'status': 'processed' if any(counts.values()) else 'no_changes',
        **counts,
    }
