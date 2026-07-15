#!/usr/bin/env python3
"""Stamp a single version string into every place TapScribe declares it.

A release is a deliberate, human step (ADR: no release-please). This
tool keeps the three static version declarations in lock-step so
``tests/test_version_consistency.py`` stays green:

  * ``pyproject.toml``                        -> ``[project].version``
  * ``tapscribe/__init__.py``                 -> ``__version__``
  * ``bridges/spacialchat-bridge/manifest.json`` -> ``"version"``

(The Windows tray exe is stamped at build time from the git tag, so it
has no static string to drift.)

Usage::

    python tools/bump_version.py 0.2.0

Idempotent: re-running with the current version is a no-op. Stdlib only.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

PYPROJECT = REPO_ROOT / "pyproject.toml"
INIT_PY = REPO_ROOT / "tapscribe" / "__init__.py"
MANIFEST = REPO_ROOT / "bridges" / "spacialchat-bridge" / "manifest.json"

# Accept semver-ish versions with an optional pre-release / local suffix
# introduced by `-` or `.` (e.g. 0.2.0, 1.0.0-rc1, 0.2.0.dev1).
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+([.-].+)?$")


def _sub_once(text: str, pattern: re.Pattern[str], replacement: str, path: Path) -> str:
    new_text, count = pattern.subn(replacement, text, count=1)
    if count != 1:
        raise RuntimeError(f"could not find the version line to replace in {path}")
    return new_text


def bump_pyproject(version: str) -> None:
    text = PYPROJECT.read_text(encoding="utf-8")
    # Match the [project] table's `version = "..."` line specifically.
    pattern = re.compile(r'^version = "[^"]*"', re.MULTILINE)
    PYPROJECT.write_text(_sub_once(text, pattern, f'version = "{version}"', PYPROJECT), encoding="utf-8")


def bump_init(version: str) -> None:
    text = INIT_PY.read_text(encoding="utf-8")
    pattern = re.compile(r'^__version__ = "[^"]*"', re.MULTILINE)
    INIT_PY.write_text(_sub_once(text, pattern, f'__version__ = "{version}"', INIT_PY), encoding="utf-8")


def bump_manifest(version: str) -> None:
    text = MANIFEST.read_text(encoding="utf-8")
    # Parse first to fail loudly on a malformed manifest, but rewrite via a
    # targeted replacement of the `"version"` line only: a full json.dump
    # would re-flow the manifest's compact single-line arrays and produce a
    # huge spurious diff, breaking the idempotency this tool guarantees.
    # `"version"` (quote-anchored) does not match `"manifest_version"`.
    json.loads(text)
    pattern = re.compile(r'^(\s*"version"\s*:\s*)"[^"]*"', re.MULTILINE)
    MANIFEST.write_text(_sub_once(text, pattern, rf'\g<1>"{version}"', MANIFEST), encoding="utf-8")


def bump_version(version: str) -> None:
    bump_pyproject(version)
    bump_init(version)
    bump_manifest(version)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stamp a release version into all declaring files.")
    parser.add_argument("version", help="the version to stamp, e.g. 0.2.0")
    args = parser.parse_args(argv)

    version = args.version
    if not VERSION_RE.match(version):
        print(
            f"error: {version!r} is not a valid version (expected N.N.N with an optional suffix)",
            file=sys.stderr,
        )
        return 2

    bump_version(version)

    print(f"Stamped version {version} into:")
    print(f"  {PYPROJECT.relative_to(REPO_ROOT)}")
    print(f"  {INIT_PY.relative_to(REPO_ROOT)}")
    print(f"  {MANIFEST.relative_to(REPO_ROOT)}")
    print()
    print("Next steps:")
    print(f"  git commit -am 'chore(release): v{version}'   # open a PR and merge to main")
    print(f"  git tag v{version} && git push origin v{version}   # triggers release.yml")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
