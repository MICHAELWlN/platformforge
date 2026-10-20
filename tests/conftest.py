from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

from platformforge.manifest import ServiceManifest

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_MANIFEST = REPO_ROOT / "examples" / "demo-web.yaml"


@pytest.fixture
def manifest_dict() -> dict:
    return copy.deepcopy(yaml.safe_load(EXAMPLE_MANIFEST.read_text()))


@pytest.fixture
def manifest(manifest_dict: dict) -> ServiceManifest:
    return ServiceManifest.model_validate(manifest_dict)


@pytest.fixture
def write_manifest(tmp_path: Path):
    def _write(data: dict | str, name: str = "manifest.yaml") -> Path:
        path = tmp_path / name
        path.write_text(data if isinstance(data, str) else yaml.safe_dump(data))
        return path

    return _write
