"""Strip-meta.json — ONE owner for strip-meta.json format, I/O, and clip→original mapping.

This module is the single authority over `strip-meta.json`: the shape gate,
the owner-by-clip reverse index, the pure single-file reader, the
RECORDINGS_DIR-contained read, the atomic write, and the clip prune.

Seam convention: consumers reach the owner through the module attribute —
`import tapscribe.strip_meta as strip_meta` — NOT via `from`-import, so
monkeypatches in tests propagate.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from . import config
from .config_store import read_json_strict
from .session_paths import FILENAME_STRIP_META_JSON
from .text import atomic_write_text


def valid_strip_meta(meta: Any) -> dict[str, Any] | None:
    if not isinstance(meta, dict) or not isinstance(meta.get("files"), dict):
        return None
    return meta


def strip_meta_owner_by_clip(meta: dict[str, Any]) -> dict[str, str]:
    owner_by_clip: dict[str, str] = {}
    for orig_name, entry in meta["files"].items():
        if isinstance(entry, dict):
            for span in entry.get("spans") or []:
                if isinstance(span, dict) and isinstance(span.get("name"), str):
                    owner_by_clip[span["name"]] = orig_name
    return owner_by_clip


def read_strip_meta_file(path: Path) -> dict[str, Any] | None:
    """Pure single-file reader. No RECORDINGS_DIR check."""
    try:
        with path.open("r", encoding="utf-8") as fh:
            return valid_strip_meta(json.load(fh))
    except (OSError, ValueError):
        return None


def load_strip_meta(stripped: Path) -> dict[str, Any] | None:
    """RECORDINGS_DIR-contained strict read, for a read-modify-write. None when
    the sidecar (after symlinks are resolved) escapes RECORDINGS_DIR, is
    missing, is not a regular file, is torn, or is legacy. Any other `OSError`
    raises, so a fold never writes over a sidecar it could not read (#446).

    Containment is a point-of-ACCESS property: the FILE is realpath'd and the
    realpath string is read directly (canonical CodeQL py/path-injection
    form: the sanitiser sits at the read, and CodeQL flows it through `real`, not
    through a re-wrapped Path), so a symlinked strip-meta.json cannot turn this
    into an arbitrary-file reader. Mirrors the sessions._read_json_strict guard.
    `read_strip_meta_file` is the UNguarded pure reader for callers
    (session_merge) that own their containment separately."""
    root = os.path.realpath(config.RECORDINGS_DIR)
    try:
        real = os.path.realpath(stripped / FILENAME_STRIP_META_JSON)
    except ValueError:
        # An embedded NUL makes the path malformed, not unreadable.
        return None
    if real != root and not real.startswith(root + os.sep):
        return None
    return valid_strip_meta(read_json_strict(real))


def read_strip_meta(stripped: Path) -> dict[str, Any] | None:
    """`load_strip_meta` for display and prune: any `OSError` reads as None."""
    try:
        return load_strip_meta(stripped)
    except OSError:
        return None


def write_strip_meta(stripped: Path, meta: dict[str, Any]) -> None:
    """Atomic write of the sidecar via the shared atomic_write_text helper
    (tempfile + os.replace, no temp droppings on success)."""
    atomic_write_text(stripped / FILENAME_STRIP_META_JSON, json.dumps(meta, indent=2))


def prune_clip(stripped: Path, clip_name: str) -> None:
    """Drop one clip's span. Drops the whole original entry if last span removed.
    No-op on missing, legacy, or unknown clip. Preserves knobs/stripped_at and
    non-dict legacy entries."""
    meta = read_strip_meta(stripped)
    if meta is None:
        return
    files = meta["files"]
    changed = False
    for orig, entry in list(files.items()):
        spans = entry.get("spans") if isinstance(entry, dict) else None
        if not isinstance(spans, list):
            continue
        kept = [sp for sp in spans if not (isinstance(sp, dict) and sp.get("name") == clip_name)]
        if len(kept) == len(spans):
            continue
        changed = True
        if kept:
            entry["spans"] = kept
        else:
            del files[orig]
    if changed:
        write_strip_meta(stripped, meta)
