"""Stage 13B2a deterministic untrusted-submission sealing."""

from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
from types import SimpleNamespace
import unicodedata

import pytest

from extraction_test_helpers import (
    INSTRUCTION_SHA,
    contracted_project,
    proposal,
    seal,
    submission,
)
import project_system.extraction_layer as layer_module
import project_system.source_extraction as module
import project_system.source_layer as source_layer_module
from project_system.cli import main
from project_system.extraction_layer import (
    EXTRACTION_SEALER_PROFILE,
    MAX_EVIDENCE_SEGMENTS_PER_PROPOSAL,
    MAX_NORMALIZED_SUBMISSION_BYTES,
    MAX_PROPOSALS_PER_SUBMISSION,
    MAX_PROPOSAL_STATEMENT_BYTES,
    MAX_RAW_SUBMISSION_BYTES,
    PROPOSAL_MANIFEST_PROFILE,
    PROPOSAL_PROFILE,
    ExtractionError,
    extraction_run_id,
    normalized_proposal_manifest,
    normalize_submission,
    proposal_manifest_sha256,
    proposal_id,
)
from project_system.source_layer import canonical_bytes
from project_system.source_extraction import seal_extraction


@pytest.fixture
def project(tmp_path):
    return contracted_project(tmp_path)


def one(project, *, statement='  exact statement  ', support='explicit', evidence=None,
        kind='requirement'):
    return submission([proposal(
        kind, statement, evidence or [project['segment_ids'][0]], support=support)])


def test_extraction_sealer_profile_and_bounds_exist():
    assert EXTRACTION_SEALER_PROFILE == 'project-system-extraction-sealer-v1'
    assert MAX_RAW_SUBMISSION_BYTES == 4 * 1024 * 1024
    assert MAX_NORMALIZED_SUBMISSION_BYTES == 2 * 1024 * 1024
    assert MAX_PROPOSALS_PER_SUBMISSION == 256
    assert MAX_PROPOSAL_STATEMENT_BYTES == 8 * 1024
    assert MAX_EVIDENCE_SEGMENTS_PER_PROPOSAL == 64
    assert PROPOSAL_MANIFEST_PROFILE == 'project-system-proposal-manifest-v1'


def test_proposal_manifest_is_deterministic_exact_and_content_free(project):
    raw = submission([
        proposal('decision', 'Do the thing', [project['segment_ids'][2]], 'inferred'),
        proposal('requirement', 'Need the thing', project['segment_ids'][:2]),
    ])
    _, records = normalize_submission(raw, project['contract_receipt'])
    manifest = normalized_proposal_manifest(records)
    assert manifest == normalized_proposal_manifest(records)
    assert proposal_manifest_sha256(manifest) == sha256(canonical_bytes(manifest)).hexdigest()
    assert set(manifest) == {'profile', 'proposals'}
    assert all(set(item) == {
        'payload_sha256', 'payload_bytes', 'evidence_segment_ids'}
        for item in manifest['proposals'])
    durable = canonical_bytes(manifest)
    assert b'Do the thing' not in durable and b'Need the thing' not in durable
    assert b'statement' not in durable and b'kind' not in durable and b'support' not in durable


def test_proposal_and_evidence_input_order_do_not_change_manifest_hash(project):
    first = submission([
        proposal('decision', 'B', [project['segment_ids'][2], project['segment_ids'][0]]),
        proposal('requirement', 'A', [project['segment_ids'][1]]),
    ])
    second = submission([
        proposal('requirement', 'A', [project['segment_ids'][1]]),
        proposal('decision', 'B', [project['segment_ids'][0], project['segment_ids'][2]]),
    ])
    _, first_records = normalize_submission(first, project['contract_receipt'])
    _, second_records = normalize_submission(second, project['contract_receipt'])
    assert proposal_manifest_sha256(normalized_proposal_manifest(first_records)) == (
        proposal_manifest_sha256(normalized_proposal_manifest(second_records)))


@pytest.mark.parametrize('change', ['payload', 'evidence', 'membership'])
def test_proposal_commitment_changes_manifest_hash(project, change):
    raw = submission([
        proposal('requirement', 'A', [project['segment_ids'][0]]),
        proposal('question', 'B', [project['segment_ids'][1]], 'ambiguous'),
    ])
    _, records = normalize_submission(raw, project['contract_receipt'])
    manifest = normalized_proposal_manifest(records)
    changed = deepcopy(manifest)
    if change == 'payload':
        changed['proposals'][0]['payload_sha256'] = 'f' * 64
        changed['proposals'][0]['payload_bytes'] += 1
    elif change == 'evidence':
        changed['proposals'][0]['evidence_segment_ids'] = [project['segment_ids'][2]]
    else:
        changed['proposals'].pop()
    assert proposal_manifest_sha256(changed) != proposal_manifest_sha256(manifest)


def test_valid_seal_exact_receipts_and_non_circular_identities(project, tmp_path):
    raw = submission([
        proposal('question', 'Question?', [project['segment_ids'][2]], 'ambiguous'),
        proposal('requirement', 'Requirement.', project['segment_ids'][:2], 'explicit'),
    ])
    report, run, proposals, private = seal(project, tmp_path, raw)
    cache = project['root'] / f'.generated/source-extractions/{report["run_id"]}/submission.json'
    normalized = json.loads(cache.read_bytes())
    assert run['sealer_profile'] == EXTRACTION_SEALER_PROFILE
    assert run['run_id'] == extraction_run_id(run)
    assert run['proposal_ids'] == [item['proposal_id'] for item in proposals]
    assert all(item['proposal_id'] == proposal_id(item) for item in proposals)
    assert report['proposals'] == 2 and report['submission_retention'] == 'reference'
    assert run['submission']['snapshot'] is None
    assert run['submission']['sha256'] == sha256(cache.read_bytes()).hexdigest()
    assert run['submission']['bytes'] == len(cache.read_bytes())
    assert [sha256(canonical_bytes(item)).hexdigest()
            for item in normalized['proposals']] == sorted(
                item['payload_sha256'] for item in proposals)
    assert report['run_id'].encode() not in b''.join(
        canonical_bytes(item) for item in normalized['proposals'])
    assert private.name.encode() not in canonical_bytes(run)
    manifest = {
        'profile': PROPOSAL_MANIFEST_PROFILE,
        'proposals': [{
            'payload_sha256': item['payload_sha256'],
            'payload_bytes': item['payload_bytes'],
            'evidence_segment_ids': item['evidence_segment_ids'],
        } for item in proposals],
    }
    assert run['submission']['proposal_manifest_sha256'] == (
        proposal_manifest_sha256(manifest))
    identity_without_prop_ids = deepcopy(run)
    identity_without_prop_ids['proposal_ids'] = ['PROP-' + 'f' * 32]
    assert extraction_run_id(identity_without_prop_ids) == run['run_id']
    changed_manifest = deepcopy(run)
    changed_manifest['submission']['proposal_manifest_sha256'] = 'f' * 64
    assert extraction_run_id(changed_manifest) != run['run_id']
    assert b'XRUN-' not in canonical_bytes(manifest)
    assert b'PROP-' not in canonical_bytes(manifest)


def test_statement_is_not_trimmed_or_unicode_normalized(project, tmp_path):
    composed = 'é'
    decomposed = unicodedata.normalize('NFD', composed)
    first, run1, _, _ = seal(project, tmp_path, one(project, statement='  exact  '),
                             filename='one.json')
    second, _, _, _ = seal(project, tmp_path, one(project, statement='exact'),
                           filename='two.json')
    third, _, _, _ = seal(project, tmp_path, one(project, statement=composed),
                          filename='three.json')
    fourth, _, _, _ = seal(project, tmp_path, one(project, statement=decomposed),
                           filename='four.json')
    cache = project['root'] / f'.generated/source-extractions/{first["run_id"]}/submission.json'
    assert json.loads(cache.read_bytes())['proposals'][0]['statement'] == '  exact  '
    assert len({first['run_id'], second['run_id'], third['run_id'], fourth['run_id']}) == 4


def test_proposal_and_evidence_input_order_are_non_authoritative(project, tmp_path):
    first_proposals = [
        proposal('decision', 'B', [project['segment_ids'][2], project['segment_ids'][0]]),
        proposal('requirement', 'A', [project['segment_ids'][1]]),
    ]
    second_proposals = [
        proposal('requirement', 'A', [project['segment_ids'][1]]),
        proposal('decision', 'B', [project['segment_ids'][0], project['segment_ids'][2]]),
    ]
    first, run1, props1, _ = seal(
        project, tmp_path, submission(first_proposals), filename='first.json')
    second, run2, props2, _ = seal(
        project, tmp_path, submission(second_proposals), filename='second.json')
    assert first['run_id'] == second['run_id']
    assert run1 == run2 and props1 == props2
    decision = next(item for item in props1 if len(item['evidence_segment_ids']) == 2)
    assert decision['evidence_segment_ids'] == [
        project['segment_ids'][0], project['segment_ids'][2]]


def test_raw_json_format_and_key_order_do_not_change_identity(project, tmp_path):
    document = json.loads(one(project, statement='same'))
    compact = json.dumps(document, separators=(',', ':'), ensure_ascii=False).encode()
    pretty = json.dumps(document, indent=4, sort_keys=True, ensure_ascii=False).encode()
    first, _, _, _ = seal(project, tmp_path, compact, filename='compact.json')
    second, _, _, _ = seal(project, tmp_path, pretty, filename='pretty.json')
    assert first['run_id'] == second['run_id']


@pytest.mark.parametrize(('field', 'value'), [
    ('statement', 'changed'),
    ('support', 'inferred'),
    ('evidence', 'second'),
])
def test_semantic_payload_change_changes_xrun(project, tmp_path, field, value):
    baseline, _, _, _ = seal(project, tmp_path, one(project), filename='base.json')
    kwargs = {}
    if field == 'statement': kwargs['statement'] = value
    elif field == 'support': kwargs['support'] = value
    else: kwargs['evidence'] = [project['segment_ids'][1]]
    changed, _, _, _ = seal(project, tmp_path, one(project, **kwargs),
                             filename=f'{field}.json')
    assert baseline['run_id'] != changed['run_id']


@pytest.mark.parametrize(('field', 'value'), [
    ('provider', 'gemini'),
    ('model', 'other-model'),
    ('instruction_sha', 'b' * 64),
])
def test_declared_executor_change_changes_xrun(project, tmp_path, field, value):
    baseline, _, _, _ = seal(project, tmp_path, one(project), filename='base.json')
    kwargs = {field: value}
    changed, _, _, _ = seal(project, tmp_path, one(project),
                             filename=f'{field}.json', **kwargs)
    assert baseline['run_id'] != changed['run_id']


@pytest.mark.parametrize(('provider', 'model', 'instruction', 'message'), [
    ('bad provider', 'model', INSTRUCTION_SHA, 'executor'),
    ('ok', '', INSTRUCTION_SHA, 'executor'),
    ('ok', 'bad\nmodel', INSTRUCTION_SHA, 'executor'),
    ('ok', 'model', 'A' * 64, 'executor'),
    ('ok', 'm' * 129, INSTRUCTION_SHA, 'executor'),
])
def test_executor_declaration_is_strict(project, tmp_path, provider, model, instruction, message):
    path = tmp_path / 'input.json'
    path.write_bytes(one(project))
    with pytest.raises(ExtractionError, match=message):
        seal_extraction(
            project['root'], project['contract']['contract_id'], path,
            executor_kind='ai', provider=provider, model=model,
            instruction_sha256=instruction)


@pytest.mark.parametrize(('raw', 'message'), [
    (b'\xff', 'strict UTF-8'),
    (b'{"schema_version":1,"schema_version":1,"profile":"project-system-extraction-submission-v1","proposals":[]}', 'duplicate'),
    (b'{"schema_version":1,"profile":"project-system-extraction-submission-v1","proposals":[],"x":NaN}', 'non-finite'),
    (b'[]', 'invalid extraction submission'),
    (b'{"schema_version":1,"profile":"project-system-extraction-submission-v1","proposals":[],"unexpected":1}', 'invalid extraction submission'),
])
def test_strict_submission_parse_rejects_malformed_input(project, tmp_path, raw, message):
    path = tmp_path / 'bad.json'
    path.write_bytes(raw)
    with pytest.raises(ExtractionError, match=message):
        seal_extraction(
            project['root'], project['contract']['contract_id'], path,
            executor_kind='ai', provider='openai', model='model',
            instruction_sha256=INSTRUCTION_SHA)


@pytest.mark.parametrize('forbidden', [
    'proposal_id', 'run_id', 'contract_id', 'accepted', 'approved', 'verified',
    'canonical', 'canonical_authority', 'human_reviewed', 'target_path', 'patch',
    'object_id', 'candidate_target_ids', 'change_hint', 'status',
])
def test_unknown_authority_and_targeting_fields_are_rejected(project, tmp_path, forbidden):
    item = proposal('requirement', 'statement', [project['segment_ids'][0]])
    item[forbidden] = True
    path = tmp_path / f'{forbidden}.json'
    path.write_bytes(submission([item]))
    with pytest.raises(ExtractionError, match='invalid extraction submission'):
        seal_extraction(
            project['root'], project['contract']['contract_id'], path,
            executor_kind='ai', provider='openai', model='model',
            instruction_sha256=INSTRUCTION_SHA)


@pytest.mark.parametrize(('mutator', 'message'), [
    (lambda p, item: [], 'invalid extraction submission'),
    (lambda p, item: [dict(item, kind='feature')], 'invalid extraction submission'),
    (lambda p, item: [dict(item, statement='')], 'invalid extraction submission'),
    (lambda p, item: [dict(item, statement=' \t\n')], 'semantically empty'),
    (lambda p, item: [dict(item, statement='x' * 8193)], 'statement exceeds'),
    (lambda p, item: [dict(item, support='certain')], 'invalid extraction submission'),
    (lambda p, item: [dict(item, evidence_segment_ids=[])], 'invalid extraction submission'),
    (lambda p, item: [dict(item, evidence_segment_ids=[p['segment_ids'][0]] * 2)], 'invalid extraction submission'),
    (lambda p, item: [dict(item, evidence_segment_ids=['SEG-' + 'f' * 32])], 'not presented'),
])
def test_submission_semantic_boundaries(project, tmp_path, mutator, message):
    item = proposal('requirement', 'statement', [project['segment_ids'][0]])
    path = tmp_path / 'boundary.json'
    path.write_bytes(submission(mutator(project, item)))
    with pytest.raises(ExtractionError, match=message):
        seal_extraction(
            project['root'], project['contract']['contract_id'], path,
            executor_kind='ai', provider='openai', model='model',
            instruction_sha256=INSTRUCTION_SHA)


def test_kind_outside_xcon_allowed_set_rejected(project, tmp_path):
    limited = contracted_project(tmp_path / 'limited', kinds=['question'])
    path = tmp_path / 'limited.json'
    path.write_bytes(submission([
        proposal('requirement', 'statement', [limited['segment_ids'][0]])]))
    with pytest.raises(ExtractionError, match='not allowed'):
        seal_extraction(
            limited['root'], limited['contract']['contract_id'], path,
            executor_kind='ai', provider='openai', model='model',
            instruction_sha256=INSTRUCTION_SHA)


def test_real_but_non_presented_seg_rejected(project, tmp_path):
    subset = contracted_project(
        tmp_path / 'subset', segments=None)
    # Directly create a subset XCON in one project to retain a real non-presented SEG.
    from project_system.source_extraction import create_extraction_contract
    subset_contract = create_extraction_contract(
        subset['root'], subset['representation']['representation_id'],
        segment_ids=[subset['segment_ids'][0]])
    subset['contract'] = subset_contract
    path = tmp_path / 'non-presented.json'
    path.write_bytes(submission([
        proposal('requirement', 'statement', [subset['segment_ids'][1]])]))
    with pytest.raises(ExtractionError, match='not presented'):
        seal_extraction(
            subset['root'], subset_contract['contract_id'], path,
            executor_kind='ai', provider='openai', model='model',
            instruction_sha256=INSTRUCTION_SHA)


def test_submission_count_evidence_and_normalized_size_bounds(project, tmp_path, monkeypatch):
    path = tmp_path / 'many.json'
    item = proposal('requirement', 'statement', [project['segment_ids'][0]])
    path.write_bytes(submission([dict(item, statement=f'statement {i}') for i in range(257)]))
    with pytest.raises(ExtractionError):
        seal_extraction(project['root'], project['contract']['contract_id'], path,
                        executor_kind='ai', provider='openai', model='model',
                        instruction_sha256=INSTRUCTION_SHA)
    monkeypatch.setattr(layer_module, 'MAX_NORMALIZED_SUBMISSION_BYTES', 1)
    path.write_bytes(one(project))
    with pytest.raises(ExtractionError, match='normalized.*size limit'):
        seal_extraction(project['root'], project['contract']['contract_id'], path,
                        executor_kind='ai', provider='openai', model='model',
                        instruction_sha256=INSTRUCTION_SHA)


def test_raw_submission_bound_is_checked_before_parse(project, tmp_path, monkeypatch):
    monkeypatch.setattr(module, 'MAX_RAW_SUBMISSION_BYTES', 8)
    path = tmp_path / 'oversized-private.json'
    path.write_bytes(b'{' + b' ' * 20 + b'}')
    with pytest.raises(ExtractionError, match='cannot be read safely'):
        seal_extraction(project['root'], project['contract']['contract_id'], path,
                        executor_kind='ai', provider='openai', model='model',
                        instruction_sha256=INSTRUCTION_SHA)


def test_duplicate_normalized_proposal_rejected(project, tmp_path):
    first = proposal('requirement', 'same', [
        project['segment_ids'][1], project['segment_ids'][0]])
    second = proposal('requirement', 'same', [
        project['segment_ids'][0], project['segment_ids'][1]])
    path = tmp_path / 'duplicate.json'
    path.write_bytes(submission([first, second]))
    with pytest.raises(ExtractionError, match='duplicate normalized'):
        seal_extraction(project['root'], project['contract']['contract_id'], path,
                        executor_kind='ai', provider='openai', model='model',
                        instruction_sha256=INSTRUCTION_SHA)


def test_reference_default_and_repository_snapshot_have_distinct_runs(project, tmp_path):
    raw = one(project, statement='retained semantics')
    reference, run1, _, _ = seal(project, tmp_path, raw, filename='reference.json')
    snapshot, run2, _, _ = seal(
        project, tmp_path, raw, retention='repository_snapshot', filename='snapshot.json')
    assert reference['run_id'] != snapshot['run_id']
    assert run1['submission']['snapshot'] is None
    snapshot_path = project['root'] / run2['submission']['snapshot']['path']
    generated = project['root'] / f'.generated/source-extractions/{snapshot["run_id"]}/submission.json'
    assert snapshot_path.read_bytes() == generated.read_bytes()
    assert snapshot_path.read_bytes() != raw
    assert sha256(snapshot_path.read_bytes()).hexdigest() == run2['submission']['sha256']
    assert len(snapshot_path.read_bytes()) == run2['submission']['bytes']


def test_durable_receipts_minimize_semantic_content_and_paths(project, tmp_path):
    secret = 'CUSTOMER_SECRET_8391'
    report, run, proposals, private = seal(project, tmp_path, one(project, statement=secret))
    durable = canonical_bytes(run) + b''.join(canonical_bytes(item) for item in proposals)
    assert secret.encode() not in durable
    assert private.name.encode() not in durable and str(private.parent).encode() not in durable
    assert set(proposals[0]) == {
        'schema_version', 'profile', 'project_id', 'proposal_id', 'run_id',
        'payload_sha256', 'payload_bytes', 'evidence_segment_ids',
        'canonical_authority', 'human_review_required'}
    assert 'kind' not in proposals[0] and 'statement' not in proposals[0]
    assert run['canonical_authority'] is False and run['human_review_required'] is True


def test_submission_symlink_and_emulated_reparse_rejected(project, tmp_path, monkeypatch):
    original = tmp_path / 'original.json'
    original.write_bytes(one(project))
    link = tmp_path / 'link.json'
    try:
        link.symlink_to(original)
    except OSError:
        link = None
    if link is not None:
        with pytest.raises(ExtractionError, match='symlink|reparse'):
            seal_extraction(project['root'], project['contract']['contract_id'], link,
                            executor_kind='ai', provider='openai', model='model',
                            instruction_sha256=INSTRUCTION_SHA)
    original_lstat = source_layer_module.os.lstat

    def reparse(path, *args, **kwargs):
        info = original_lstat(path, *args, **kwargs)
        if Path(path) == original:
            return SimpleNamespace(
                st_mode=info.st_mode, st_file_attributes=0x400,
                st_dev=info.st_dev, st_ino=info.st_ino, st_size=info.st_size,
                st_mtime_ns=info.st_mtime_ns)
        return info

    monkeypatch.setattr(source_layer_module.os, 'lstat', reparse)
    with pytest.raises(ExtractionError, match='symlink|reparse'):
        seal_extraction(project['root'], project['contract']['contract_id'], original,
                        executor_kind='ai', provider='openai', model='model',
                        instruction_sha256=INSTRUCTION_SHA)


def test_submission_toctou_fails_closed(project, tmp_path, monkeypatch):
    path = tmp_path / 'mutable.json'
    path.write_bytes(one(project))
    original = Path.open
    changed = False

    class MutatingReader:
        def __init__(self, wrapped): self.wrapped = wrapped
        def __enter__(self): return self
        def __exit__(self, *args): self.wrapped.close()
        def fileno(self): return self.wrapped.fileno()
        def read(self, size):
            nonlocal changed
            data = self.wrapped.read(size)
            if not changed:
                changed = True
                with original(path, 'ab') as output:
                    output.write(b' ')
            return data

    def opening(target, *args, **kwargs):
        stream = original(target, *args, **kwargs)
        return MutatingReader(stream) if target == path and args == ('rb',) else stream

    monkeypatch.setattr(Path, 'open', opening)
    with pytest.raises(ExtractionError, match='cannot be read safely'):
        seal_extraction(project['root'], project['contract']['contract_id'], path,
                        executor_kind='ai', provider='openai', model='model',
                        instruction_sha256=INSTRUCTION_SHA)
    assert changed and not list((project['root'] / 'intake/extraction-runs').glob('XRUN-*'))


def test_cli_error_hides_private_submission_path(project, tmp_path, monkeypatch, capsys):
    private = tmp_path / 'customer-private-invalid.json'
    private.write_bytes(b'not-json')
    monkeypatch.chdir(project['root'])
    with pytest.raises(SystemExit) as error:
        main(['source', 'extraction', 'seal', project['contract']['contract_id'], str(private),
              '--executor-kind', 'ai', '--provider', 'openai', '--model', 'model',
              '--instruction-sha256', INSTRUCTION_SHA])
    output = capsys.readouterr().err
    assert error.value.code == 2 and 'source extraction seal failed' in output
    assert str(private) not in output and 'Traceback' not in output


def test_seal_cli_defaults_to_reference_and_has_no_mutating_subcommands(
        project, tmp_path, monkeypatch, capsys):
    path = tmp_path / 'submission.json'
    path.write_bytes(one(project))
    monkeypatch.chdir(project['root'])
    main(['source', 'extraction', 'seal', project['contract']['contract_id'], str(path),
          '--executor-kind', 'ai', '--provider', 'openai', '--model', 'model',
          '--instruction-sha256', INSTRUCTION_SHA])
    report = json.loads(capsys.readouterr().out)
    assert report['submission_retention'] == 'reference'
    for action in ('update', 'delete', 'reset'):
        with pytest.raises(SystemExit) as error:
            main(['source', 'extraction', action])
        assert error.value.code == 2


def test_matching_seal_idempotent_and_cache_rebuild(project, tmp_path):
    raw = one(project)
    first, run, proposals, path = seal(project, tmp_path, raw)
    durable_paths = [
        project['root'] / f'intake/extraction-runs/{first["run_id"]}.json',
        *[project['root'] / f'intake/proposals/{item["proposal_id"]}.json'
          for item in proposals],
    ]
    before = {item: (item.read_bytes(), item.stat().st_mtime_ns) for item in durable_paths}
    second, _, _, _ = seal(project, tmp_path, raw, filename='again.json')
    assert second == dict(first, status='existing')
    cache = project['root'] / f'.generated/source-extractions/{first["run_id"]}/submission.json'
    cache.unlink()
    third, _, _, _ = seal(project, tmp_path, raw, filename='third.json')
    assert third == dict(first, status='rebuilt_cache') and cache.is_file()
    cache.write_bytes(b'corrupt')
    fourth, _, _, _ = seal(project, tmp_path, raw, filename='fourth.json')
    assert fourth == dict(first, status='rebuilt_cache')
    assert before == {item: (item.read_bytes(), item.stat().st_mtime_ns)
                      for item in durable_paths}


@pytest.mark.parametrize('artifact', ['run', 'proposal', 'snapshot'])
def test_conflicting_immutable_artifact_fails_without_overwrite(project, tmp_path, artifact):
    retention = 'repository_snapshot' if artifact == 'snapshot' else 'reference'
    report, run, proposals, _ = seal(project, tmp_path, one(project), retention=retention)
    if artifact == 'run':
        target = project['root'] / f'intake/extraction-runs/{report["run_id"]}.json'
    elif artifact == 'proposal':
        target = project['root'] / f'intake/proposals/{proposals[0]["proposal_id"]}.json'
    else:
        target = project['root'] / run['submission']['snapshot']['path']
    target.write_bytes(b'contradiction')
    before = target.read_bytes()
    with pytest.raises(ExtractionError, match='invalid|conflict'):
        seal(project, tmp_path, one(project), retention=retention, filename='again.json')
    assert target.read_bytes() == before


def test_partial_publication_failure_rolls_back_only_created_files(project, tmp_path, monkeypatch):
    original = module._publish_one
    calls = 0

    def failing(root, relative, raw, created):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ExtractionError('injected publication failure')
        return original(root, relative, raw, created)

    monkeypatch.setattr(module, '_publish_one', failing)
    path = tmp_path / 'submission.json'
    path.write_bytes(one(project))
    with pytest.raises(ExtractionError, match='injected'):
        seal_extraction(project['root'], project['contract']['contract_id'], path,
                        executor_kind='ai', provider='openai', model='model',
                        instruction_sha256=INSTRUCTION_SHA)
    assert not list((project['root'] / 'intake/proposals').glob('PROP-*'))
    assert not list((project['root'] / 'intake/extraction-runs').glob('XRUN-*'))


def test_sealing_invokes_no_network_ai_or_product_truth_writes(project, tmp_path, monkeypatch):
    protected = ['knowledge', 'docs', '.project/policies', '.agents/skills']
    before = {name: sorted((item.relative_to(project['root']).as_posix(), item.read_bytes())
                           for item in (project['root'] / name).rglob('*') if item.is_file())
              for name in protected}
    import subprocess
    monkeypatch.setattr(subprocess, 'Popen', lambda *a, **k: (_ for _ in ()).throw(
        AssertionError('sealer invoked a subprocess')))
    seal(project, tmp_path, one(project))
    after = {name: sorted((item.relative_to(project['root']).as_posix(), item.read_bytes())
                          for item in (project['root'] / name).rglob('*') if item.is_file())
             for name in protected}
    assert before == after
