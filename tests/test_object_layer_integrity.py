from pathlib import Path

import pytest

from project_system.cli import main
from project_system.context import build_context
from project_system.generation import generate, GenerationBlockedError
from project_system.graph import build_graph
from project_system.frontmatter import read_object, write_object
from project_system.init_project import init_project
from project_system.object_loader import load_object_layer
from project_system.objects import create_object
from project_system.tasking import task
from project_system.validation import validate
import project_system.ids as ids


def _fatal(issues):
    return [issue for issue in issues if issue[0] in {'BLOCKING', 'ERROR'}]


def _with_slug(path, slug='search'):
    target = path.with_name(f'{path.stem}-{slug}.md')
    path.rename(target)
    return target


@pytest.mark.parametrize('suffix', ['.yaml', '.yml'])
def test_validation_rejects_unsupported_atomic_yaml(tmp_path, suffix):
    root = init_project('Demo', tmp_path / 'demo')
    path = root / 'knowledge' / 'decisions' / f'DEC-20260903-deadbeef{suffix}'
    path.write_text('id: DEC-20260903-deadbeef\ntype: decision\n', encoding='utf-8')

    issues = validate(root)

    assert any(
        issue[0] == 'ERROR'
        and str(path.relative_to(root)) == issue[1]
        and 'unsupported atomic object format' in issue[2]
        for issue in issues
    )


def test_nonempty_knowledge_with_zero_recognized_objects_is_error(tmp_path):
    root = init_project('Demo', tmp_path / 'demo')
    (root / 'knowledge' / 'README.txt').write_text('not an atomic object', encoding='utf-8')

    issues = validate(root)

    assert any(
        issue[0] == 'ERROR'
        and issue[1] == 'knowledge'
        and 'no atomic objects were recognized' in issue[2]
        for issue in issues
    )


def test_gitkeep_only_knowledge_is_valid(tmp_path):
    root = init_project('Demo', tmp_path / 'demo')
    assert not _fatal(validate(root))


def test_loader_is_shared_source_of_object_counts(tmp_path):
    root = init_project('Demo', tmp_path / 'demo')
    create_object(root, 'decision', 'D', 'product', 'owner')
    create_object(root, 'feature', 'F', 'product', 'owner')

    layer = load_object_layer(root)

    assert len(layer.objects) == 2
    assert layer.counts_by_type() == {'decision': 1, 'feature': 1}


def test_id_only_filename_is_valid(tmp_path):
    root = init_project('Demo', tmp_path / 'demo')
    path, object_id = create_object(root, 'feature', 'Search', 'product', 'owner')

    assert path.name == f'{object_id}.md'
    assert not _fatal(validate(root))


def test_id_slug_filename_is_valid_and_transparent_to_consumers(tmp_path):
    root = init_project('Demo', tmp_path / 'demo')
    path, object_id = create_object(root, 'feature', 'Search', 'product', 'owner')
    path = _with_slug(path)

    layer = load_object_layer(root)
    graph, _, _ = build_graph(root)
    _, context_manifest = build_context(root, object_id, 'small')
    _, task_manifest = task(root, object_id, 'implement', 'small')
    generated = generate(root)

    assert not _fatal(validate(root))
    assert layer.objects[object_id]['path'] == path
    assert object_id in graph
    assert object_id in context_manifest['included_objects']
    assert str(path.relative_to(root)) in task_manifest['allowed_write_set']
    assert object_id in (generated / 'indexes' / 'FEATURES.md').read_text(encoding='utf-8')
    assert str(path.relative_to(root)) in (generated / 'indexes' / 'PROJECT_MAP.md').read_text(encoding='utf-8')


def test_filename_id_prefix_must_match_internal_id(tmp_path):
    root = init_project('Demo', tmp_path / 'demo')
    path, object_id = create_object(root, 'feature', 'Search', 'product', 'owner')
    data, body = read_object(path)
    data['id'] = object_id[:-8] + ('cafebabe' if not object_id.endswith('cafebabe') else 'deadbeef')
    write_object(path, data, body)

    issues = validate(root)

    assert any(
        issue[0] == 'ERROR'
        and issue[1] == str(path.relative_to(root))
        and 'filename ID prefix does not match object id' in issue[2]
        for issue in issues
    )


def test_malformed_object_filename_is_rejected(tmp_path):
    root = init_project('Demo', tmp_path / 'demo')
    path, _ = create_object(root, 'feature', 'Search', 'product', 'owner')
    path = path.rename(path.with_name('search.md'))

    issues = validate(root)

    assert any(
        issue[0] == 'ERROR'
        and issue[1] == str(path.relative_to(root))
        and 'filename must be ID.md or ID-slug.md' in issue[2]
        for issue in issues
    )


def test_duplicate_internal_id_with_different_slugs_is_rejected(tmp_path):
    root = init_project('Demo', tmp_path / 'demo')
    path, object_id = create_object(root, 'feature', 'Search', 'product', 'owner')
    first = _with_slug(path, 'first')
    second = first.with_name(f'{object_id}-second.md')
    second.write_text(first.read_text(encoding='utf-8'), encoding='utf-8')

    layer = load_object_layer(root)
    issues = validate(root)

    assert len(layer.records) == 2
    assert list(layer.objects) == [object_id]
    assert any(issue == ('ERROR', object_id, 'duplicate ID') for issue in issues)


def test_id_collision_detection_uses_loaded_markdown_objects(tmp_path, monkeypatch):
    root = init_project('Demo', tmp_path / 'demo')
    suffixes = iter(['deadbeef', 'deadbeef', 'cafebabe'])
    monkeypatch.setattr(ids.secrets, 'token_hex', lambda _: next(suffixes))

    first_path, first_id = create_object(root, 'feature', 'First', 'product', 'owner')
    _with_slug(first_path, 'first')
    _, second_id = create_object(root, 'feature', 'Second', 'product', 'owner')

    assert first_id.endswith('-deadbeef')
    assert second_id.endswith('-cafebabe')


def test_generation_uses_loaded_markdown_objects(tmp_path):
    root = init_project('Demo', tmp_path / 'demo')
    _, object_id = create_object(root, 'feature', 'Search', 'product', 'owner')

    generated = generate(root)

    assert object_id in (generated / 'indexes' / 'FEATURES.md').read_text(encoding='utf-8')


@pytest.mark.parametrize('sync', [False, True])
def test_task_and_sync_target_loaded_markdown_object(tmp_path, sync):
    root = init_project('Demo', tmp_path / 'demo')
    object_path, object_id = create_object(root, 'feature', 'Search', 'product', 'owner')

    _, manifest = task(root, object_id, 'sync' if sync else 'implement', 'small', sync)

    assert object_id in manifest['included_objects']
    assert str(object_path.relative_to(root)) in manifest['allowed_write_set']


def test_validate_cli_prints_recognized_object_counts(tmp_path, monkeypatch, capsys):
    root = init_project('Demo', tmp_path / 'demo')
    create_object(root, 'feature', 'Search', 'product', 'owner')
    monkeypatch.chdir(root)

    with pytest.raises(SystemExit) as exited:
        main(['validate'])

    assert exited.value.code == 0
    output = capsys.readouterr().out
    assert 'Objects: 1' in output
    assert '- feature: 1' in output


def test_generate_stops_before_writing_when_validation_fails(tmp_path):
    root = init_project('Demo', tmp_path / 'demo')
    bad = root / 'knowledge' / 'features' / 'FEAT-20260903-deadbeef.yaml'
    bad.write_text('id: FEAT-20260903-deadbeef\ntype: feature\n', encoding='utf-8')

    with pytest.raises(GenerationBlockedError, match='generation blocked by validation'):
        generate(root)

    assert not (root / '.generated' / 'indexes').exists()
    assert not (root / '.generated' / 'graphs').exists()
    assert not (root / '.generated' / 'reports').exists()


def test_cli_version_uses_package_runtime_version(capsys):
    from project_system import __version__

    with pytest.raises(SystemExit) as exited:
        main(['--version'])

    assert exited.value.code == 0
    assert capsys.readouterr().out.strip() == f'project-system-cli {__version__}'
