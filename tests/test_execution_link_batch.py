"""B3 batch traversal counts with independently sealed attempt/run/link chains."""
from collections import Counter
import json

import pytest

from extraction_test_helpers import seal
from project_system import execution_link as m, execution_fixture as fixture
from project_system import source_extraction as sealer
from project_system.source_layer import canonical_bytes
from project_system.utils import load_yaml
from test_execution_link import project, prepare, cap, blob
from project_system import execution_attempt as e


def chains(project, tmp_path, count):
    result = []
    for index in range(count):
        attempt = prepare(project)
        e.dispatch_attempt(project['root'], attempt.attempt_id, cap(project, attempt))
        raw = e.read_response(project['root'], attempt.attempt_id)
        _, run, props, _ = seal(
            project, tmp_path, raw, provider='test-declared-' + str(index),
            model='synthetic-not-ai', instruction_sha=attempt.contract['instruction_sha256'],
            filename='separate-' + str(index) + '.json')
        link = m.create_execution_link(project['root'], attempt.attempt_id, run['run_id'])
        result.append((attempt, run, link))
    assert len({a.attempt_id for a, _, _ in result}) == count
    assert len({r['run_id'] for _, r, _ in result}) == count
    assert len({l.link_id for _, _, l in result}) == count
    return result


def counters(monkeypatch):
    counts = Counter()
    def watch(owner, name, key):
        original = getattr(owner, name)
        def counted(*args, **kwargs):
            counts[key] += 1
            return original(*args, **kwargs)
        monkeypatch.setattr(owner, name, counted)
    watch(m, '_inventory', 'inventory')
    watch(sealer, 'inspect_extraction_layer', 'extraction')
    watch(sealer, 'inspect_source_layer', 'source')
    watch(sealer, 'inspect_representation_layer', 'representation')
    watch(m, 'normalize_submission', 'raw_normalization')
    return counts


@pytest.mark.parametrize('count', [2, 4])
def test_batch_has_one_inventory_and_one_extraction_scan(project, tmp_path, monkeypatch, count):
    chains(project, tmp_path, count)
    counts = counters(monkeypatch)
    assert m.inspect_execution_link_layer(project['root'], load_yaml(project['root'] / 'project.yaml')) == ()
    print('MEASURED', count, dict(counts))
    assert counts['inventory'] == 1, dict(counts)
    assert counts['extraction'] == counts['source'] == counts['representation'] == 1, dict(counts)
    assert counts['raw_normalization'] == count


def test_direct_inspection_revalidates_fresh_each_call(project, tmp_path, monkeypatch):
    pairs = chains(project, tmp_path, 2)
    counts = counters(monkeypatch)
    for _, _, link in pairs:
        assert m.inspect_execution_link(project['root'], link.link_id).reproducibility == 'AVAILABLE'
    print('DIRECT', dict(counts))
    assert counts['inventory'] == counts['extraction'] == 2
    assert counts['raw_normalization'] == 2
    # A completed call must not leave a cached success or verified layer.
    a, _, link = pairs[-1]
    blob(project, a).write_bytes(b'corrupt after completed call')
    with pytest.raises(m.LinkError):
        m.inspect_execution_link(project['root'], link.link_id)


@pytest.mark.parametrize('mode', ['missing', 'upstream', 'duplicate', 'unknown', 'malformed'])
def test_batch_preserves_existing_failure_modes(project, tmp_path, mode):
    pairs = chains(project, tmp_path, 3)
    a, run, link = pairs[-1]
    root = project['root']
    target = root / ('intake/execution-links/' + link.link_id + '.json')
    if mode == 'missing':
        blob(project, a).unlink()
    elif mode == 'upstream':
        upstream = root / ('intake/extraction-runs/' + run['run_id'] + '.json')
        upstream.write_bytes(upstream.read_bytes() + b' ')
    elif mode == 'duplicate':
        # A schema/identity-valid second receipt claims the same ATTEMPT.
        doc = json.loads(target.read_bytes())
        doc['run']['sha256'] = '0' * 64
        doc['link_id'] = m.link_identity(doc)
        target.with_name(doc['link_id'] + '.json').write_bytes(canonical_bytes(doc))
    elif mode == 'unknown':
        target.with_name('unknown.json').write_bytes(b'{}')
    else:
        target.write_bytes(b'{}')
    issues = m.inspect_execution_link_layer(root, load_yaml(root / 'project.yaml'))
    if mode == 'missing':
        assert any(level == 'WARNING' and 'unavailable' in message for level, _, message in issues)
        assert not any(level == 'ERROR' for level, _, _ in issues)
    else:
        assert any(level == 'ERROR' for level, _, _ in issues)


@pytest.mark.parametrize('kind', ['xcon', 'prop', 'seg', 'new_link'])
def test_changes_during_batch_cannot_become_stale_success(project, tmp_path, monkeypatch, kind):
    pairs = chains(project, tmp_path, 3)
    a, run, link = pairs[-1]
    root = project['root']
    original = m.normalize_submission
    changed = []
    def mutate(raw, contract):
        result = original(raw, contract)
        if not changed:
            changed.append(True)
            if kind == 'xcon':
                p = root / ('intake/extraction-contracts/' + a.contract['contract_id'] + '.json')
            elif kind == 'prop':
                p = root / ('intake/proposals/' + run['proposal_ids'][0] + '.json')
            elif kind == 'seg':
                p = root / ('intake/representations/' + project['representation']['representation_id'] + '.segments.jsonl')
            else:
                p = root / 'intake/execution-links/unknown.json'
            p.write_bytes(p.read_bytes() + b' ' if p.exists() else b'{}')
        return result
    monkeypatch.setattr(m, 'normalize_submission', mutate)
    issues = m.inspect_execution_link_layer(root, load_yaml(root / 'project.yaml'))
    assert changed and any(level == 'ERROR' for level, _, _ in issues)


def test_caller_config_cannot_supply_batch_provenance(project, tmp_path):
    pairs = chains(project, tmp_path, 2)
    a, _, _ = pairs[-1]
    xcon = project['root'] / ('intake/extraction-contracts/' + a.contract['contract_id'] + '.json')
    xcon.write_bytes(xcon.read_bytes() + b' ')
    assert any(level == 'ERROR' for level, _, _ in
               m.inspect_execution_link_layer(project['root'], {'pretend_verified': True}))


def test_full_project_validation_does_not_rescan_extraction_per_v4_attempt(project, tmp_path, monkeypatch):
    from project_system import validation
    chains(project, tmp_path, 4)
    counts = counters(monkeypatch)
    for name, key in (('inspect_extraction_layer', 'extraction'),
                      ('inspect_source_layer', 'source'),
                      ('inspect_representation_layer', 'representation')):
        original = getattr(validation, name)
        def counted(*args, _original=original, _key=key, **kwargs):
            counts[_key] += 1
            return _original(*args, **kwargs)
        monkeypatch.setattr(validation, name, counted)
    issues = validation.validate(project['root'])
    print('FULL PROJECT', dict(counts))
    assert not any(level in {'ERROR', 'BLOCKING'} for level, _, _ in issues)
    # Existing extraction pass, independent execution batch, independent XLINK batch.
    assert counts['extraction'] == counts['source'] == counts['representation'] == 3
    assert counts['inventory'] == 1
    assert counts['raw_normalization'] == 4


def test_failed_extraction_scan_is_not_retried_per_link(project, tmp_path, monkeypatch):
    pairs = chains(project, tmp_path, 3)
    _, run, _ = pairs[-1]
    upstream = project['root'] / ('intake/extraction-runs/' + run['run_id'] + '.json')
    upstream.write_bytes(b'{}')
    counts = counters(monkeypatch)
    issues = m.inspect_execution_link_layer(project['root'], load_yaml(project['root'] / 'project.yaml'))
    assert any(level == 'ERROR' for level, _, _ in issues)
    assert counts['inventory'] == counts['extraction'] == 1
    assert counts['raw_normalization'] == 0
    # Independent later calls must retry verification, not inherit the failure latch.
    with pytest.raises(m.LinkError):
        m.inspect_execution_link(project['root'], pairs[0][2].link_id)
    assert counts['extraction'] == 2


def test_public_single_link_never_inherits_an_outer_batch_success(project, tmp_path, monkeypatch):
    pairs = chains(project, tmp_path, 2)
    counts = counters(monkeypatch)
    with fixture._validation_batch(project['root']):
        fixture.response_commitment(project['root'], project['contract']['contract_id'])
        assert counts['extraction'] == 1
        m.inspect_execution_link(project['root'], pairs[0][2].link_id)
        assert counts['extraction'] == 2
    assert counts['inventory'] == 1 and counts['raw_normalization'] == 1


def test_execution_v4_batch_rejects_changed_segment_inputs_before_return(project, tmp_path, monkeypatch):
    chains(project, tmp_path, 3)
    original = fixture.normalize_submission
    changed = []
    def mutate(raw, contract):
        result = original(raw, contract)
        if not changed:
            changed.append(True)
            index = project['root'] / ('intake/representations/' +
                project['representation']['representation_id'] + '.segments.jsonl')
            index.write_bytes(index.read_bytes() + b' ')
        return result
    monkeypatch.setattr(fixture, 'normalize_submission', mutate)
    result = e.inspect_execution_layer(project['root'], load_yaml(project['root'] / 'project.yaml'))
    assert changed and result.attempts == {}
    assert any(level == 'ERROR' for level, _, _ in result.issues)
