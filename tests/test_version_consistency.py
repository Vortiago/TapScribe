"""Guard: the three static version declarations must stay in lock-step.

``tools/bump_version.py`` stamps the same version into ``pyproject.toml``,
``tapscribe/__init__.py`` and the SpatialChat bridge manifest. This test
fails the moment one drifts (e.g. a manual edit that skipped the tool),
so a release never ships mismatched versions.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import tapscribe

REPO_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"
MANIFEST = REPO_ROOT / "bridges" / "spacialchat-bridge" / "manifest.json"


def test_static_versions_are_consistent() -> None:
    pyproject_version = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]["version"]
    manifest_version = json.loads(MANIFEST.read_text(encoding="utf-8"))["version"]

    assert pyproject_version == tapscribe.__version__ == manifest_version, (
        "version drift: "
        f"pyproject={pyproject_version!r} "
        f"tapscribe.__version__={tapscribe.__version__!r} "
        f"manifest={manifest_version!r} "
        "— run `python tools/bump_version.py <version>` to re-stamp all three."
    )
