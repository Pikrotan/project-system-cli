from pathlib import Path
import tomllib

from project_system import __version__


def test_package_metadata_version_matches_runtime_version():
    root = Path(__file__).resolve().parents[1]
    package_version = tomllib.loads(
        (root / 'pyproject.toml').read_text(encoding='utf-8')
    )['project']['version']

    assert package_version == __version__
