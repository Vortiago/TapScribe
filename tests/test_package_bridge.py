"""Tests for tools/package_bridge.py — the SpatialChat extension zip.

Packaging is a standalone stdlib-only script, imported via path
manipulation like the other tools tests. The assertions pin the
cross-PR asset contract: the zip extracts to a single
``spacialchat-bridge/`` dir, carries the manifest + the ``lib/`` and
``components/`` subtrees, and never leaks dev-only material.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import package_bridge


def _names(out_dir: Path) -> list[str]:
    zip_path = package_bridge.package_bridge("spacialchat", out_dir)
    assert zip_path.name == "tapscribe-spacialchat-bridge.zip"
    with zipfile.ZipFile(zip_path) as zf:
        return zf.namelist()


def test_zip_contains_shippable_entries(tmp_path: Path) -> None:
    names = _names(tmp_path)

    assert "spacialchat-bridge/manifest.json" in names
    assert any(n.startswith("spacialchat-bridge/lib/") and n.endswith(".js") for n in names), names
    assert any(n.startswith("spacialchat-bridge/components/") for n in names), names
    # Every entry lives under the single top-level dir Chrome loads.
    assert all(n.startswith("spacialchat-bridge/") for n in names), names


def test_zip_excludes_dev_only_material(tmp_path: Path) -> None:
    names = _names(tmp_path)

    forbidden = ("tests/", "e2e/", "typecheck/", "node_modules")
    for name in names:
        rel = name.removeprefix("spacialchat-bridge/")
        assert not rel.startswith(forbidden), f"dev-only entry leaked: {name}"
        assert rel != "README.md", f"README.md leaked: {name}"
        assert rel != "types.d.ts", f"types.d.ts leaked: {name}"


def test_zip_ordering_is_deterministic(tmp_path: Path) -> None:
    first = _names(tmp_path / "a")
    second = _names(tmp_path / "b")
    assert first == second == sorted(first)


def test_missing_source_dir_errors(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setitem(package_bridge.BRIDGES["spacialchat"], "src", tmp_path / "does-not-exist")
    ret = package_bridge.main(["spacialchat", "--out", str(tmp_path / "out")])
    assert ret == 1
