"""The State view: the read model behind `GET /api/state`.

The dashboard polls that endpoint about twice a second (ADR-0013: state
transport is poll, not push) and the response is a projection over many owners:
the session listing, the People registry, the live channel's info and log tail,
the operator's text config, the registry-derived editor support flags, and the
open taps. Assembling it is pure given snapshots, so it lives here rather than
in the route: `routes/state.py` snapshots the Recorder, hops to a thread, and
owns the 304 branch, and nothing else needs a Recorder or a Request to test.

Two derivations have no other owner:

- `active_rows` overlays each open tap's row with the CURRENT per-identity
  record/live preference and buckets its byte counter.
- the `default_override_counts` loop, which tells the config card how many
  sessions override each global default.

Request-free and Recorder-free by design, like the batch orchestrators: every
input arrives as a snapshot, the HTTP status of a failure is the route's
business, and a CLI or queue worker can render the same projection. It is not
literally fastapi-free: `jsonable_encoder` is what gives the payload its
datetime-aware encoding, and that IS the wire format.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import asdict
from typing import Any

from fastapi.encoders import jsonable_encoder

from . import config
from . import hallucinations as hallucinations_mod
from .batch_transcribe import resolve_batch_model
from .name_resolution import attach_people_view
from .people import PeopleRegistry
from .recorder import ActiveStream, TapSetting
from .text import (
    read_config,
    read_languages,
    read_summarizer_config,
    summarizer_default_public,
)
from .transcribers import current_idle_ttl_s
from .transcribers.catalog import REGISTRY

# Round each open tap's raw bytes_received (bumped per 20 ms audio frame) to the
# nearest bucket before it lands in /api/state, so a quiet-but-open tap's
# sub-bucket byte drift stops busting the response ETag every poll (#217). One
# constant so the round-to-nearest stays centred: half a bucket is TAP_BYTES_BUCKET // 2.
TAP_BYTES_BUCKET = 64 * 1024


def compute_inputs_support() -> dict[str, bool]:
    """Derive per-context support flags for the dashboard editors.

    The dashboard hides each editor when no installed model in that
    context declares the corresponding input. We compute this from the
    registry (`ModelEntry.inputs`) so adding a future Voxtral prompt
    field (or removing one) automatically updates the UI gating with
    no manual flag-flipping.

    `live_hotwords` is intentionally not exposed: WhisperLiveKit's CLI
    has no --hotwords flag (see `build_live_cmd`), so even though
    Whisper-family entries declare hotwords in `WHISPER_INPUTS`, the
    live channel can't currently consume them.
    """

    def _any_installed_has(context: str, input_name: str) -> bool:
        for entry in REGISTRY.for_context(context, only_installed=True):  # type: ignore[arg-type]
            for inp in entry.inputs:
                if inp.name == input_name:
                    return True
        return False

    return {
        "live_prompt": _any_installed_has("live", "initial_prompt"),
        "batch_prompt": _any_installed_has("batch", "initial_prompt"),
        "batch_hotwords": _any_installed_has("batch", "hotwords"),
    }


def active_rows(
    active_streams: list[ActiveStream],
    tap_setting_for: Callable[[str], TapSetting],
) -> list[dict[str, Any]]:
    """One /api/state row per open tap: the ActiveStream as a dict, with the
    operator's CURRENT per-identity record/live preference overlaid (the
    stream's own copy is the value captured at WS open) and two poll-friendly
    roundings: the level to 2 decimals and the byte counter to the nearest
    `TAP_BYTES_BUCKET`.

    `tap_setting_for` is `recorder.tap_settings.get`, passed in rather than
    reached for, so this stays a pure function of its snapshots.
    """
    rows = []
    for stream in active_streams:
        row = asdict(stream)
        pref = tap_setting_for(stream.identity)
        row["record"] = pref.record
        row["live"] = pref.live
        row["level"] = round(row["level"], 2)
        row["bytes_received"] = (
            (row["bytes_received"] + TAP_BYTES_BUCKET // 2) // TAP_BYTES_BUCKET * TAP_BYTES_BUCKET
        )
        rows.append(row)
    return rows


def build_state_blob(
    *,
    current_session: str,
    active: list[dict[str, Any]],
    sessions_list: list[dict[str, Any]],
    registry: PeopleRegistry,
    occs: list[dict[str, Any]],
    live_identities: set[str],
    live_feed: list[dict[str, Any]],
    live_info: dict[str, Any],
    live_log: list[str],
    live_supports_native_vad: bool,
    recording_enabled: bool,
    backend: str,
    available_backends: list[str],
) -> tuple[bytes, str]:
    """Config reads, pure people joins, payload assembly, and ETag serialization.

    `sessions_list` is pre-gathered; the registry is pre-synced (mutation ran on
    the event loop). All recorder-owned inputs are snapshotted. Returns
    (body_bytes, etag_string).

    Keyword-only, deliberately: thirteen parameters with two adjacent same-typed
    pairs (`active`/`sessions_list`, `live_supports_native_vad`/
    `recording_enabled`) means a transposition at the call site would type-check,
    lint clean and ship a wrong payload, and no test of the serialized bytes
    could see it.

    `live_identities` MUST be the identity set of `active` (the route derives
    both from one `streams.snapshot()`); it is passed rather than derived here
    because the route needs it for the People mutation anyway."""
    prompt = read_config("prompt")
    live_prompt = read_config("live-prompt")
    live_model_default = read_config("live-model")
    batch_model_default = read_config("batch-model")
    batch_model_effective = resolve_batch_model(warn=False)
    languages_default = list(read_languages())
    hotwords = read_config("hotwords")
    summarizer_default = summarizer_default_public(read_summarizer_config())
    halluc_rules = hallucinations_mod.parse_rules()
    hallucinations_content = read_config("hallucinations")
    inputs_support = compute_inputs_support()

    people = attach_people_view(sessions_list, registry, occs, live_identities)

    override_counts: dict[str, int] = {"prompt": 0, "hotwords": 0, "summarizer": 0}
    for s in sessions_list:
        m = s.get("session_meta") or {}
        if m.get("prompt"):
            override_counts["prompt"] += 1
        if m.get("hotwords"):
            override_counts["hotwords"] += 1
        if m.get("summary_source") or m.get("summary_prompt"):
            override_counts["summarizer"] += 1

    payload = {
        "current_session": current_session,
        "active": active,
        "sessions": sessions_list,
        "people": people,
        "default_override_counts": override_counts,
        "live_feed": live_feed,
        "live_info": live_info,
        "live_log": live_log,
        "live_supports_native_vad": live_supports_native_vad,
        "backend": backend,
        "available_backends": available_backends,
        "recording_enabled": recording_enabled,
        "prompt": {
            "path": str(config.PROMPT_FILE),
            "content": prompt,
            "length": len(prompt),
        },
        "live_prompt": {
            "path": str(config.LIVE_PROMPT_FILE),
            "content": live_prompt,
            "length": len(live_prompt),
        },
        "hotwords": {
            "path": str(config.HOTWORDS_FILE),
            "content": hotwords,
            "length": len(hotwords),
        },
        "inputs_support": inputs_support,
        "live_model_default": live_model_default,
        "batch_model_default": batch_model_default,
        "batch_model_effective": batch_model_effective,
        "languages": {
            "path": str(config.LANGUAGES_FILE),
            "default": languages_default,
        },
        "summarizer_default": summarizer_default,
        # No "rules" list here: the config card renders the raw content
        # textarea and reads only content + count — shipping every raw rule
        # string per poll tick was dead payload weight (#303 follow-up).
        "hallucinations": {
            "path": str(config.HALLUCINATIONS_FILE),
            "content": hallucinations_content,
            "count": len(halluc_rules),
        },
        "idle_ttl_s": current_idle_ttl_s(),
    }
    body = json.dumps(jsonable_encoder(payload), separators=(",", ":")).encode("utf-8")
    etag = 'W/"' + hashlib.blake2b(body, digest_size=12).hexdigest() + '"'
    return body, etag
