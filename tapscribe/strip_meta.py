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
import tempfile
from pathlib import Path
from typing import Any

from . import config
from .session_paths import FILENAME_STRIP_META_JSON


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


def _check_contained(stripped: Path) -> bool:
    root = os.path.realpath(config.RECORDINGS_DIR)
    try:
        real = os.path.realpath(stripped)
    except (OSError, ValueError):
        return False
    return real != root and real.startswith(root + os.sep)


def read_strip_meta(stripped: Path) -> dict[str, Any] | None:
    """RECORDINGS_DIR-contained read. None if path escapes, file missing, or legacy."""
    if not _check_contained(stripped):
        return None
    return read_strip_meta_file(stripped / FILENAME_STRIP_META_JSON)


def write_strip_meta(stripped: Path, meta: dict[str, Any]) -> None:
    """Atomic write via tempfile + os.replace. Leaves no temp droppings on success."""
    path = stripped / FILENAME_STRIP_META_JSON
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".strip-meta-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(meta, indent=2))
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


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
