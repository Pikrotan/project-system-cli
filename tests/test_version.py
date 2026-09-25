from pathlib import Path
import tomllib

from project_system import __version__


def test_package_metadata_version_matches_runtime_version():
    root = Path(__file__).resolve().parents[1]
    package_version = tomllib.loads(
        (root / 'pyproject.toml').read_text(encoding='utf-8')
    )['project']['version']

    assert package_version == __version__ == '0.12.0'


def test_skills_release_assets_are_declared_and_packaged():
    root = Path(__file__).resolve().parents[1]
    pyproject = tomllib.loads((root / 'pyproject.toml').read_text(encoding='utf-8'))
    assert 'SKILLS_ARCHITECTURE_V1.md' in pyproject['tool']['setuptools']['data-files']['.']
    assets = root / 'project_cli' / 'project_system_assets'
    assert (assets / 'schemas' / 'skills.schema.json').is_file()
    assert (assets / 'skills' / 'catalog.yaml').is_file()
    assert len(list((assets / 'skills').glob('*/SKILL.md'))) == 8
