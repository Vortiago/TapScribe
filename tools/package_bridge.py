#!/usr/bin/env python3
"""Package a distributable Bridge into a release zip.

Currently the only distributable Bridge is the SpatialChat Chrome MV3
extension (``spacialchat``). The zip contains a single top-level
``spacialchat-bridge/`` directory so an operator can unzip it and point
Chrome's "Load unpacked" at that folder directly.

The include set is an EXPLICIT allowlist (manifest + the specific
top-level entry files + the ``lib/`` and ``components/`` subtrees), NOT a
raw walk of the whole source directory. That keeps dev-only material
(``tests/``, ``e2e/``, ``typecheck/``, ``README.md``, ``types.d.ts``,
any ``node_modules/``) out of the artifact by construction and keeps the
CodeQL path analysis happy (no filename derived from an unbounded walk
flows into a new path).

Stdlib only.
"""

from __future__ import annotations

import argparse
import sys
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# The one Bridge we distribute today. Keyed by the CLI `bridge` arg
# (constrained via argparse `choices=`), each entry describes where the
# source lives, the top-level dir the zip extracts to, and the output
# zip filename (the cross-PR canonical asset name — the dashboard's
# download URL depends on it).
SPACIALCHAT_SRC = REPO_ROOT / "bridges" / "spacialchat-bridge"

# Top-level files shipped verbatim. Everything else at the top level
# (README.md, types.d.ts) is dev-only and deliberately omitted.
SPACIALCHAT_TOP_LEVEL_GLOBS = ("*.js", "*.css", "*.html")

# Subtrees shipped recursively.
SPACIALCHAT_SUBTREES = ("lib", "components")

BRIDGES = {
    "spacialchat": {
        "src": SPACIALCHAT_SRC,
        "arc_root": "spacialchat-bridge",
        "zip_name": "tapscribe-spacialchat-bridge.zip",
    },
}


def _collect_spacialchat_files(src: Path) -> list[Path]:
    """Return the allowlisted source files, sorted for deterministic zips."""
    files: list[Path] = []

    manifest = src / "manifest.json"
    if not manifest.is_file():
        raise FileNotFoundError(f"missing manifest.json in {src}")
    files.append(manifest)

    for pattern in SPACIALCHAT_TOP_LEVEL_GLOBS:
        files.extend(p for p in src.glob(pattern) if p.is_file())

    for subtree in SPACIALCHAT_SUBTREES:
        subtree_dir = src / subtree
        if subtree_dir.is_dir():
            files.extend(p for p in subtree_dir.rglob("*") if p.is_file())

    # Sort by the path relative to `src` so ordering is stable regardless
    # of filesystem walk order.
    return sorted(set(files), key=lambda p: p.relative_to(src).as_posix())


def package_bridge(bridge: str, out_dir: Path) -> Path:
    """Build the release zip for `bridge` under `out_dir`; return its path."""
    spec = BRIDGES[bridge]
    src: Path = spec["src"]
    if not src.is_dir():
        raise FileNotFoundError(f"bridge source directory not found: {src}")

    files = _collect_spacialchat_files(src)

    out_dir.mkdir(parents=True, exist_ok=True)
    zip_path = out_dir / spec["zip_name"]
    arc_root = spec["arc_root"]

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in files:
            arcname = f"{arc_root}/{path.relative_to(src).as_posix()}"
            zf.write(path, arcname)

    return zip_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Package a distributable Bridge into a release zip.")
    parser.add_argument("bridge", choices=sorted(BRIDGES), help="which Bridge to package")
    parser.add_argument(
        "--out",
        required=True,
        type=Path,
        help="output directory for the zip (created if absent)",
    )
    args = parser.parse_args(argv)

    try:
        zip_path = package_bridge(args.bridge, args.out)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"wrote {zip_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
