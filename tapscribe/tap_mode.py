"""Single-person vs multi-person tap — the reserved wire values, the precedence
ladder, and the durable per-identity override (ADR-0021).

The Bridge declares a tap's mode on `/tap`; the operator can override it per
identity. Precedence mirrors name resolution: **operator override › bridge
declaration › default single**. Only a multi-person tap is diarized.

The wire is lenient by design — absent or unrecognised means `single`, matching
how every other `/tap` param defaults. Junk must never mean `multi`: that
manufactures Voices out of one human. The operator PUT is strict instead, and
rejects a bad value at the route.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import config
from .text import atomic_write_text

#: Reserved wire spellings. Stamped into every bridge by
#: `tools/stamp_tap_wire.py` — never hand-edit the copies under `bridges/`.
TAP_MODE_SINGLE = "single"
TAP_MODE_MULTI = "multi"

#: The `tap_mode` query parameter on `/tap`.
TAP_MODE_PARAM = "tap_mode"

_MODES = (TAP_MODE_SINGLE, TAP_MODE_MULTI)

#: `{identity: mode}` at the recordings root. `TapSettings` (record/live) is
#: in-memory by design, so a preference that must outlive a restart needs its
#: own home; the shape follows `summarizer.json` — structured JSON, atomic
#: write, its own route pair.
STORE_JSON = "tap-modes.json"


def _store_path() -> Path:
    return config.RECORDINGS_DIR / STORE_JSON


def is_mode(value: object) -> bool:
    return value in _MODES


def resolve(*, declared: str | None, override: str | None) -> str:
    """The effective mode for one tap."""
    if is_mode(override):
        return override  # type: ignore[return-value]
    if is_mode(declared):
        return declared  # type: ignore[return-value]
    return TAP_MODE_SINGLE


def overrides() -> dict[str, str]:
    """Every stored per-identity override. Missing or torn → `{}`."""
    try:
        raw = json.loads(_store_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # Missing (OSError) or torn (ValueError): fall back to the bridges'
        # declarations rather than failing a tap open.
        return {}
    if not isinstance(raw, dict):
        return {}
    return {k: v for k, v in raw.items() if isinstance(k, str) and k and is_mode(v)}


def set_override(identity: str, mode: str | None) -> dict[str, str]:
    """Set or clear one identity's override. `None` clears. Raises `ValueError`
    on anything else, so a bad value can never reach the store."""
    if mode is not None and not is_mode(mode):
        raise ValueError(f"unknown tap mode: {mode!r} (expected {' or '.join(_MODES)})")
    current = overrides()
    if mode is None:
        current.pop(identity, None)
    else:
        current[identity] = mode
    atomic_write_text(_store_path(), json.dumps(current, indent=2, ensure_ascii=False))
    return current
