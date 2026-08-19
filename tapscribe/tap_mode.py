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
from .text import atomic_write_text, file_stat_sig

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
    for value in (override, declared):
        if is_mode(value):
            return str(value)
    return TAP_MODE_SINGLE


#: Single slot, keyed on `file_stat_sig`. `overrides()` is read on every
#: `/api/state` tick AND every `/tap` open (per utterance, for the SpatialChat
#: bridge), on the event loop in both cases — the same reason `config_store`
#: caches its reads. A hit costs one `stat()` instead of a read + parse.
_OVERRIDES_CACHE: dict[str, tuple[tuple | None, dict[str, str]]] = {}


def _parse_overrides(text: str) -> dict[str, str]:
    try:
        raw = json.loads(text)
    except ValueError:
        # Torn file: fall back to the bridges' declarations rather than
        # failing a tap open.
        return {}
    if not isinstance(raw, dict):
        return {}
    return {k: v for k, v in raw.items() if isinstance(k, str) and k and is_mode(v)}


def overrides() -> dict[str, str]:
    """Every stored per-identity override. Missing or torn → `{}`.

    Returns a COPY: the cached dict must outlive the call unmutated, and the
    map is a handful of entries at most, so the copy is free next to the read
    and parse it replaces."""
    path = _store_path()
    sig = file_stat_sig(path, include_path=True)
    if sig is None:
        # Missing is the common case (no operator has overridden anything).
        # Don't cache it — re-statting is already the whole cost.
        _OVERRIDES_CACHE.pop("_slot", None)
        return {}
    hit = _OVERRIDES_CACHE.get("_slot")
    if hit is not None and hit[0] == sig:
        return dict(hit[1])
    try:
        parsed = _parse_overrides(path.read_text(encoding="utf-8"))
    except OSError:
        return {}
    _OVERRIDES_CACHE["_slot"] = (sig, parsed)
    return dict(parsed)


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
    # Structural invalidation, like PeopleRegistry.save: a route that writes then
    # reads back within one request must not see the pre-write slot.
    _OVERRIDES_CACHE.pop("_slot", None)
    return current
