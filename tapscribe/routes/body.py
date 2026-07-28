"""The request-body boundary: read a JSON body, and parse one field of it.

Every router that accepts a body crosses this module, so the strictness rules
have one owner instead of one per resource group. Two axes:

- **Reading the body**: `json_body` (any failure is `{}`) vs
  `require_json_object_body` (a non-object body is a 400). The strict twin is
  for routes where an empty dict would DESTROY state.
- **Parsing one field**: the `parse_*` / `require_*` family. Every one of them
  treats absent as `None` passthrough (so the downstream "field not supplied"
  semantics keep working) and anything malformed as a 400 at the HTTP edge,
  never a silent coercion. `bool("false") is True` is the reason
  `parse_opt_bool` exists at all.

These names are public (no leading underscore) because they are the seam the
routers import; they were private helpers while every route lived in one module.
"""

from __future__ import annotations

import json
import math
from typing import Any

from fastapi import HTTPException, Request


async def json_body(req: Request) -> dict[str, Any]:
    """Return the request body parsed as a dict, or {} on any failure.
    Routes that want to *require* a JSON object body call this then
    branch on emptiness; routes that treat the body as optional just use
    the dict directly."""
    try:
        body = await req.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


async def require_json_object_body(req: Request, *, allow_empty: bool) -> dict[str, Any]:
    """`json_body`'s strict twin: a body that isn't a JSON object is a 400
    rather than a silent `{}`. Routes where the empty dict would DESTROY state
    (rotate the global session, wipe the summarizer default) use this.
    `allow_empty=True` still accepts a missing body as `{}` — the legacy
    no-body call."""
    raw = await req.body()
    if not raw:
        if allow_empty:
            return {}
        raise HTTPException(400, "a JSON object body is required (send {} to clear the config)")
    try:
        body = json.loads(raw)
    except ValueError:
        raise HTTPException(400, "malformed JSON body") from None
    if not isinstance(body, dict):
        raise HTTPException(400, "JSON body must be an object")
    return body


def parse_bounded_float(raw, field: str, *, lo: float, hi: float) -> float | None:
    """Parse an optional numeric body field with range enforcement.
    None / missing → returned unchanged so the downstream "field not
    supplied" semantics still work. Anything else must round-trip
    through `float()`, be finite, and land in [lo, hi]; otherwise
    raise 400. The explicit finite check matters because
    `lo <= NaN <= hi` is always False AND `NaN` happily survives
    `float()` — without the check a `{"gate_speech_threshold": NaN}`
    payload would slip past with a confusing "must be in […]" error
    that names NaN as the offending value."""
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        # `from None` — CodeQL py/stack-trace-exposure: chain adds nothing
        # the detail message doesn't already convey.
        raise HTTPException(400, f"{field} must be a number, got {raw!r}") from None
    if not math.isfinite(value):
        raise HTTPException(400, f"{field} must be a finite number, got {value}")
    if not (lo <= value <= hi):
        raise HTTPException(400, f"{field} must be in [{lo}, {hi}], got {value}")
    return value


def parse_bounded_int(raw, field: str, *, lo: int, hi: int) -> int | None:
    if raw is None:
        return None
    try:
        # Accept JSON numerics (which arrive as float in some clients)
        # by routing through float→int — rejects "3.5" implicitly.
        value = int(raw)
    except (TypeError, ValueError):
        # `from None` — see parse_bounded_float for the rationale.
        raise HTTPException(400, f"{field} must be an integer, got {raw!r}") from None
    if not (lo <= value <= hi):
        raise HTTPException(400, f"{field} must be in [{lo}, {hi}], got {value}")
    return value


def require_opt_str(raw, field: str) -> str | None:
    """The type boundary for optional string body fields — the ONE owner of
    the non-string 400 (a non-string JSON value 400s like every other
    malformed field in the parse_* family; the `(body.get(x) or
    "").strip()` idiom 500s with an AttributeError before any validation
    runs). Returns the string VERBATIM — strip/blank policy belongs to the
    thin wrappers below (or the call site, for fields where whitespace is
    meaningful, e.g. the summarize route's prompt/api_key)."""
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise HTTPException(400, f"{field} must be a string, got {type(raw).__name__}")
    return raw


def parse_opt_str(raw, field: str) -> str | None:
    """Optional string body field: absent/blank → None, non-string → 400,
    otherwise the stripped value."""
    value = require_opt_str(raw, field)
    return None if value is None else (value.strip() or None)


def parse_opt_str_keep_empty(raw, field: str) -> str | None:
    """`parse_opt_str` for fields where the EMPTY string is meaningful
    (an explicit clear — e.g. the summarize route's command/base_url
    overrides): absent → None, non-string → 400, otherwise the stripped
    value — "" included."""
    value = require_opt_str(raw, field)
    return None if value is None else value.strip()


def parse_opt_bool(raw, field: str) -> bool | None:
    """Optional boolean body field: absent → None passthrough; anything
    that isn't a JSON true/false 400s — same strictness as the rest of
    the parse_* family. A truthy non-bool like "false" must never
    silently coerce (bool("false") is True)."""
    if raw is None:
        return None
    if not isinstance(raw, bool):
        raise HTTPException(400, f"{field} must be a boolean, got {type(raw).__name__}")
    return raw
