#!/usr/bin/env python3
"""Stamp a single version string into every place TapScribe declares it.

A release is a deliberate, human step (ADR: no release-please). This
tool keeps the static version declarations in lock-step so
``tests/test_version_consistency.py`` stays green:

  * ``pyproject.toml``                            -> ``[project].version``
  * ``tapscribe/__init__.py``                     -> ``__version__``
  * ``bridges/spacialchat-bridge/manifest.json``  -> ``"version"``

(The Windows tray exe is stamped at build time from the git tag, so it
has no static string to drift.) Adding a fourth declaring file is one
new ``_STAMPS`` row.

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

# One row per declaring file: (path, anchored version-line pattern, replacement
# template with a `{v}` slot). The version line is replaced IN PLACE so file
# formatting is preserved -- a JSON round-trip would re-flow the manifest's
# compact arrays and produce a huge spurious diff, breaking idempotency. `.json`
# rows are parsed first (fail loud on a malformed source before rewriting). The
# manifest's quote-anchored `"version"` never matches `"manifest_version"`.
_STAMPS: tuple[tuple[Path, re.Pattern[str], str], ...] = (
    (
        REPO_ROOT / "pyproject.toml",
        re.compile(r'^version = "[^"]*"', re.MULTILINE),
        'version = "{v}"',
    ),
    (
        REPO_ROOT / "tapscribe" / "__init__.py",
        re.compile(r'^__version__ = "[^"]*"', re.MULTILINE),
        '__version__ = "{v}"',
    ),
    (
        REPO_ROOT / "bridges" / "spacialchat-bridge" / "manifest.json",
        re.compile(r'^(\s*"version"\s*:\s*)"[^"]*"', re.MULTILINE),
        r'\g<1>"{v}"',
    ),
)

# Accept semver-ish versions with an optional pre-release / local suffix
# introduced by `-` or `.` (e.g. 0.2.0, 1.0.0-rc1, 0.2.0.dev1).
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+([.-].+)?$")


def _stamp(path: Path, pattern: re.Pattern[str], replacement: str) -> None:
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".json":
        json.loads(text)  # fail loud on a malformed source before rewriting
    new_text, count = pattern.subn(replacement, text, count=1)
    if count != 1:
        raise RuntimeError(f"could not find the version line to replace in {path}")
    path.write_text(new_text, encoding="utf-8")


def bump_version(version: str) -> None:
    for path, pattern, template in _STAMPS:
        _stamp(path, pattern, template.format(v=version))


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
    for path, _, _ in _STAMPS:
        print(f"  {path.relative_to(REPO_ROOT)}")
    print()
    print("Next steps:")
    print(f"  git commit -am 'chore(release): v{version}'   # open a PR and merge to main")
    print(f"  git tag v{version} && git push origin v{version}   # triggers release.yml")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
