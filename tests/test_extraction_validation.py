"""Stage 13B2a durable extraction evidence validation."""

from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import shutil

import pytest
from jsonschema import Draft202012Validator

from extraction_test_helpers import contracted_project, proposal, seal, submission
from project_system.extraction_layer import (
    EXTRACTION_CONTRACT_PROFILE,
    EXTRACTION_RUN_PROFILE,
    EXTRACTION_SUBMISSION_PROFILE,
    PROPOSAL_PROFILE,
    extraction_contract_id,
    extraction_run_id,
    proposal_id,
    serialize_receipt,
)
from project_system.init_project import init_project
from project_system.source_layer import canonical_bytes
from project_system.utils import distribution_root
from project_system.validation import validate


def extraction_issues(root):
    return [item for item in validate(root) if item[1].startswith('intake')]


@pytest.fixture
def reference_project(tmp_path):
    project = contracted_project(tmp_path)
    report, run, proposals, _ = seal(
        project, tmp_path,
        submission([
            proposal('requirement', 'Requirement', project['segment_ids'][:2]),
            proposal('question', 'Question', [project['segment_ids'][2]], 'ambiguous'),
        ]))
    project.update(report=report, run=run, proposals=proposals)
    return project


@pytest.fixture
def snapshot_project(tmp_path):
    project = contracted_project(tmp_path)
    report, run, proposals, _ = seal(
        project, tmp_path,
        submission([proposal('risk', 'Risk', [project['segment_ids'][0]], 'inferred')]),
        retention='repository_snapshot')
    project.update(report=report, run=run, proposals=proposals)
    return project


def test_all_four_schemas_are_packaged_strict_and_exact():
    schemas = {
        'extraction-contract.schema.json': EXTRACTION_CONTRACT_PROFILE,
        'extraction-submission.schema.json': EXTRACTION_SUBMISSION_PROFILE,
        'extraction-run.schema.json': EXTRACTION_RUN_PROFILE,
        'proposal.schema.json': PROPOSAL_PROFILE,
    }
    for name, profile in schemas.items():
        schema = json.loads((distribution_root() / 'schemas' / name).read_bytes())
        Draft202012Validator.check_schema(schema)
        assert schema['additionalProperties'] is False
        assert set(schema['required']) == set(schema['properties'])
        assert schema['properties']['profile']['const'] == profile
    submission_schema = json.loads((distribution_root() /
        'schemas/extraction-submission.schema.json').read_bytes())
    assert submission_schema['properties']['proposals']['items']['additionalProperties'] is False
    run_schema = json.loads((distribution_root() /
        'schemas/extraction-run.schema.json').read_bytes())
    submission = run_schema['properties']['submission']
    assert set(submission['required']) == set(submission['properties']) == {
        'sha256', 'bytes', 'proposals', 'proposal_manifest_sha256',
        'retention', 'snapshot'}


def test_valid_reference_and_snapshot_projects_validate(reference_project, snapshot_project):
    assert not extraction_issues(reference_project['root'])
    assert not extraction_issues(snapshot_project['root'])


def test_generated_cache_deletion_does_not_affect_validation(reference_project):
    generated = reference_project['root'] / '.generated/source-extractions'
    shutil.rmtree(generated)
    assert not extraction_issues(reference_project['root'])


def test_reference_retention_has_no_durable_submission_snapshot(reference_project):
    root = reference_project['root']
    assert not list((root / 'intake/extraction-submissions').glob('XRUN-*'))
    assert reference_project['run']['submission']['snapshot'] is None
    assert not extraction_issues(root)


@pytest.mark.parametrize(('artifact', 'mutation'), [
    ('contract', lambda doc: doc.update(extra=True)),
    ('contract', lambda doc: doc.update(contract_id='XCON-' + 'f' * 32)),
    ('run', lambda doc: doc.update(project_id='wrong-project')),
    ('run', lambda doc: doc.update(sealer_profile='future-sealer')),
    ('proposal', lambda doc: doc.update(human_review_required=False)),
    ('proposal', lambda doc: doc.update(canonical_authority=True)),
])
def test_malformed_or_tampered_receipts_are_rejected(reference_project, artifact, mutation):
    root = reference_project['root']
    if artifact == 'contract':
        path = root / f'intake/extraction-contracts/{reference_project["contract"]["contract_id"]}.json'
    elif artifact == 'run':
        path = root / f'intake/extraction-runs/{reference_project["report"]["run_id"]}.json'
    else:
        path = root / f'intake/proposals/{reference_project["proposals"][0]["proposal_id"]}.json'
    document = json.loads(path.read_bytes())
    mutation(document)
    path.write_text(json.dumps(document), encoding='utf-8')
    assert any(location == path.relative_to(root).as_posix()
               for _, location, _ in extraction_issues(root))


def test_xcon_project_and_coverage_tamper_rejected(reference_project):
    root = reference_project['root']
    old = root / f'intake/extraction-contracts/{reference_project["contract"]["contract_id"]}.json'
    document = json.loads(old.read_bytes())
    document['coverage']['complete'] = not document['coverage']['complete']
    document['contract_id'] = extraction_contract_id(document)
    new = old.with_name(document['contract_id'] + '.json')
    old.unlink()
    new.write_bytes(serialize_receipt(document))
    assert extraction_issues(root)


def test_xcon_missing_rep_is_rejected(reference_project):
    root = reference_project['root']
    rid = reference_project['representation']['representation_id']
    (root / f'intake/representations/{rid}.json').unlink()
    (root / f'intake/representations/{rid}.segments.jsonl').unlink()
    assert any('missing valid REP' in message
               for _, _, message in extraction_issues(root))


def test_missing_xcon_rejects_xrun_and_props(reference_project):
    root = reference_project['root']
    (root / f'intake/extraction-contracts/{reference_project["contract"]["contract_id"]}.json').unlink()
    issues = extraction_issues(root)
    assert any('missing valid XCON' in message for _, _, message in issues)
    assert any('missing valid XRUN' in message for _, _, message in issues)


def test_missing_prop_referenced_by_xrun_is_rejected(reference_project):
    root = reference_project['root']
    missing = reference_project['proposals'][0]['proposal_id']
    (root / f'intake/proposals/{missing}.json').unlink()
    assert any(missing in message and 'missing valid PROP' in message
               for _, _, message in extraction_issues(root))


def test_orphan_prop_and_prop_not_listed_by_xrun_are_rejected(reference_project):
    root = reference_project['root']
    original = reference_project['proposals'][0]
    orphan = deepcopy(original)
    orphan['run_id'] = 'XRUN-' + 'e' * 32
    orphan['proposal_id'] = proposal_id(orphan)
    (root / f'intake/proposals/{orphan["proposal_id"]}.json').write_bytes(
        serialize_receipt(orphan))
    assert any('missing valid XRUN' in message
               for _, _, message in extraction_issues(root))


def test_xrun_proposal_list_order_tamper_rejected(reference_project):
    root = reference_project['root']
    path = root / f'intake/extraction-runs/{reference_project["report"]["run_id"]}.json'
    run = json.loads(path.read_bytes())
    run['proposal_ids'].reverse()
    path.write_bytes(serialize_receipt(run))
    assert extraction_issues(root)


def test_reference_run_rejects_substituted_prop_commitment_set(reference_project):
    root = reference_project['root']
    run_path = root / f'intake/extraction-runs/{reference_project["report"]["run_id"]}.json'
    run = json.loads(run_path.read_bytes())

    for original in reference_project['proposals']:
        (root / f'intake/proposals/{original["proposal_id"]}.json').unlink()

    replacements = []
    for index, evidence in enumerate((
            [reference_project['segment_ids'][2]],
            [reference_project['segment_ids'][0]],
    )):
        replacement = {
            'schema_version': 1,
            'profile': PROPOSAL_PROFILE,
            'project_id': run['project_id'],
            'proposal_id': '',
            'run_id': run['run_id'],
            'payload_sha256': sha256(f'replacement-{index}'.encode()).hexdigest(),
            'payload_bytes': 100 + index,
            'evidence_segment_ids': evidence,
            'canonical_authority': False,
            'human_review_required': True,
        }
        replacement['proposal_id'] = proposal_id(replacement)
        replacements.append(replacement)
    replacements.sort(key=lambda item: item['payload_sha256'])
    for replacement in replacements:
        (root / f'intake/proposals/{replacement["proposal_id"]}.json').write_bytes(
            serialize_receipt(replacement))
    run['proposal_ids'] = [item['proposal_id'] for item in replacements]
    run_path.write_bytes(serialize_receipt(run))

    assert extraction_issues(root), (
        'reference XRUN accepted a substituted durable PROP commitment set')


def test_missing_required_snapshot_rejected(snapshot_project):
    root = snapshot_project['root']
    path = root / snapshot_project['run']['submission']['snapshot']['path']
    path.unlink()
    assert any('missing submission snapshot' in message
               for _, _, message in extraction_issues(root))


def test_orphan_snapshot_rejected(reference_project):
    root = reference_project['root']
    directory = root / ('intake/extraction-submissions/XRUN-' + 'f' * 32)
    directory.mkdir()
    (directory / 'submission.json').write_bytes(b'{}')
    assert any('orphan extraction submission snapshot' in message
               for _, _, message in extraction_issues(root))


@pytest.mark.parametrize('tamper', ['bytes', 'semantic'])
def test_tampered_or_noncanonical_snapshot_rejected(snapshot_project, tamper):
    root = snapshot_project['root']
    path = root / snapshot_project['run']['submission']['snapshot']['path']
    if tamper == 'bytes':
        path.write_bytes(path.read_bytes() + b' ')
    else:
        document = json.loads(path.read_bytes())
        path.write_text(json.dumps(document, indent=2, ensure_ascii=False), encoding='utf-8')
    assert any(location == path.relative_to(root).as_posix()
               for _, location, _ in extraction_issues(root))


def test_snapshot_payload_prop_binding_mismatch_rejected(snapshot_project):
    root = snapshot_project['root']
    proposal_path = root / f'intake/proposals/{snapshot_project["proposals"][0]["proposal_id"]}.json'
    document = json.loads(proposal_path.read_bytes())
    document['payload_bytes'] += 1
    document['proposal_id'] = proposal_id(document)
    new = proposal_path.with_name(document['proposal_id'] + '.json')
    proposal_path.unlink()
    new.write_bytes(serialize_receipt(document))
    assert extraction_issues(root)


def test_snapshot_checks_same_proposal_manifest_commitment(snapshot_project):
    root = snapshot_project['root']
    old_run = snapshot_project['run']
    old_run_path = root / f'intake/extraction-runs/{old_run["run_id"]}.json'
    run = json.loads(old_run_path.read_bytes())
    run['submission']['proposal_manifest_sha256'] = 'f' * 64
    run['run_id'] = extraction_run_id(run)

    old_snapshot = root / old_run['submission']['snapshot']['path']
    run['submission']['snapshot']['path'] = (
        f'intake/extraction-submissions/{run["run_id"]}/submission.json')
    new_snapshot = root / run['submission']['snapshot']['path']
    new_snapshot.parent.mkdir()
    old_snapshot.replace(new_snapshot)
    old_snapshot.parent.rmdir()

    replacements = []
    for original in snapshot_project['proposals']:
        old_proposal_path = root / f'intake/proposals/{original["proposal_id"]}.json'
        replacement = deepcopy(original)
        replacement['run_id'] = run['run_id']
        replacement['proposal_id'] = proposal_id(replacement)
        old_proposal_path.unlink()
        (root / f'intake/proposals/{replacement["proposal_id"]}.json').write_bytes(
            serialize_receipt(replacement))
        replacements.append(replacement)
    run['proposal_ids'] = [item['proposal_id'] for item in replacements]
    old_run_path.unlink()
    (root / f'intake/extraction-runs/{run["run_id"]}.json').write_bytes(
        serialize_receipt(run))

    assert any(location == new_snapshot.relative_to(root).as_posix()
               and 'manifest commitment mismatch' in message
               for _, location, message in extraction_issues(root))


@pytest.mark.parametrize(('directory', 'name'), [
    ('extraction-contracts', 'unexpected.txt'),
    ('extraction-runs', 'nested'),
    ('proposals', 'unexpected.jsonl'),
])
def test_unexpected_known_subdirectory_entries_fail_closed(reference_project, directory, name):
    root = reference_project['root']
    path = root / 'intake' / directory / name
    path.mkdir() if name == 'nested' else path.write_text('unexpected')
    assert any(location == path.relative_to(root).as_posix()
               for _, location, _ in extraction_issues(root))


def test_malformed_gitkeep_is_not_silently_ignored(reference_project):
    root = reference_project['root']
    path = root / 'intake/extraction-contracts/.gitkeep'
    path.write_text('not empty')
    assert any(location == 'intake/extraction-contracts/.gitkeep'
               for _, location, _ in extraction_issues(root))


@pytest.mark.parametrize('name', ['raw-provider-response.json', '.gitkeep'])
def test_unexpected_nested_snapshot_files_fail_closed(snapshot_project, name):
    root = snapshot_project['root']
    directory = (root / snapshot_project['run']['submission']['snapshot']['path']).parent
    extra = directory / name
    extra.write_text('' if name == '.gitkeep' else '{}')
    assert any(location == extra.relative_to(root).as_posix()
               for _, location, _ in extraction_issues(root))


def test_issue_ordering_is_deterministic(reference_project):
    root = reference_project['root']
    (root / 'intake/proposals/z.txt').write_text('z')
    (root / 'intake/extraction-contracts/a.txt').write_text('a')
    first = extraction_issues(root)
    second = extraction_issues(root)
    assert first == second == sorted(set(first))
