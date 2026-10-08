"""Focused Stage 13B2a test fixtures; not production code."""

import json

from project_system.extraction_layer import PROPOSAL_KINDS
from project_system.init_project import init_project
from project_system.source_capture import capture_source
from project_system.source_extraction import create_extraction_contract, seal_extraction
from project_system.source_representation import represent_source


INSTRUCTION_SHA = 'a' * 64


def represented_project(tmp_path, raw=b'first\nsecond\nthird'):
    root = init_project('Demo', tmp_path / 'project')
    source = tmp_path / 'private-source.txt'
    source.write_bytes(raw)
    capture = capture_source(
        root, source, key='conversation', provider='generic', kind='conversation',
        retention='repository_snapshot')
    representation = represent_source(root, capture['capture_id'], adapter='utf8-lines')
    receipt_path = root / f'intake/representations/{representation["representation_id"]}.json'
    receipt = json.loads(receipt_path.read_bytes())
    index_path = receipt_path.with_suffix('.segments.jsonl')
    descriptors = [json.loads(line) for line in index_path.read_bytes().splitlines()]
    return {
        'root': root,
        'source': source,
        'capture': capture,
        'representation': representation,
        'representation_receipt': receipt,
        'descriptors': descriptors,
        'segment_ids': [item['segment_id'] for item in descriptors],
    }


def contracted_project(tmp_path, raw=b'first\nsecond\nthird', *, segments=None,
                       kinds=None):
    project = represented_project(tmp_path, raw)
    contract = create_extraction_contract(
        project['root'], project['representation']['representation_id'],
        segment_ids=segments, allowed_kinds=kinds)
    path = project['root'] / f'intake/extraction-contracts/{contract["contract_id"]}.json'
    project.update(contract=contract, contract_receipt=json.loads(path.read_bytes()))
    return project


def submission(proposals):
    return json.dumps({
        'schema_version': 1,
        'profile': 'project-system-extraction-submission-v1',
        'proposals': proposals,
    }, ensure_ascii=False).encode()


def proposal(kind, statement, evidence, support='explicit'):
    return {
        'kind': kind,
        'statement': statement,
        'support': support,
        'evidence_segment_ids': list(evidence),
    }


def seal(project, tmp_path, raw, *, retention='reference', provider='openai',
         model='example-model', instruction_sha=INSTRUCTION_SHA, filename='private.json'):
    path = tmp_path / filename
    path.write_bytes(raw)
    report = seal_extraction(
        project['root'], project['contract']['contract_id'], path,
        executor_kind='ai', provider=provider, model=model,
        instruction_sha256=instruction_sha, retention=retention)
    run_path = project['root'] / f'intake/extraction-runs/{report["run_id"]}.json'
    run = json.loads(run_path.read_bytes())
    proposals = [json.loads((project['root'] / f'intake/proposals/{item}.json').read_bytes())
                 for item in run['proposal_ids']]
    return report, run, proposals, path
