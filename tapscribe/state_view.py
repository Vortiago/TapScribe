"""The State view: the read model behind `GET /api/state`.

The dashboard polls that endpoint about twice a second (ADR-0013: state
transport is poll, not push) and the response is a projection over many owners:
the session listing, the People registry, the live channel's info and log tail,
the operator's text config, the registry-derived editor support flags, and the
open taps. Assembling it is pure given snapshots, so it lives here rather than
in the route: `routes/state.py` snapshots the Recorder into one `StateInputs`,
hops to a thread, and owns the 304 branch, and nothing else needs a Recorder or
a Request to test.

Three derivations have no other owner:

- `active_rows` overlays each open tap's row with the CURRENT per-identity
  record/live preference and buckets its byte counter.
- `live_identities_of`, the set of identities with an open tap — read off the
  same rows the payload ships, by both the route (for the People mutation) and
  `StateInputs` (for the join).
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
import os
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any

from fastapi.encoders import jsonable_encoder

from . import config
from . import hallucinations as hallucinations_mod
from .batch_transcribe import resolve_batch_model
from .live import LiveSnapshot
from .name_resolution import attach_people_view
from .people import PeopleRegistry
from .recorder import ActiveStream, TapSetting
from .summarizers.catalog import ENV_GGUF_CTX, default_gguf_ctx
from .summarizers.command import ENV_TIMEOUT_S, current_summarize_timeout_s
from .text import (
    file_stat_sig,
    read_config,
    read_languages,
    read_summarizer_config,
    summarizer_default_public,
)
from .transcribers import (
    ENV_CHUNK_S,
    ENV_OVERLAP_S,
    current_idle_ttl_s,
    current_parakeet_chunk_s,
    current_parakeet_overlap_s,
)
from .transcribers.catalog import REGISTRY, effective_specialists

# Round each open tap's raw bytes_received (bumped per 20 ms audio frame) to the
# nearest bucket before it lands in /api/state, so a quiet-but-open tap's
# sub-bucket byte drift stops busting the response ETag every poll (#217). One
# constant so the round-to-nearest stays centred: half a bucket is TAP_BYTES_BUCKET // 2.
TAP_BYTES_BUCKET = 64 * 1024

# The four numeric knobs (#210) each resolve through a config-file read, and
# this payload is rebuilt ~2 Hz per connected client — so memoise them on the
# knob files' stat signatures plus the raw env values that outrank them, rather
# than opening four files per tick. Exact, not merely cheap: every write the
# dashboard can make goes through `atomic_write_text` (tempfile + os.replace),
# which lands a NEW inode, so the signature always moves. A read cache only —
# the projection stays pure given its inputs. (`idle_ttl_s` is #347's and reads
# straight through, as it did before this cache existed.)
_KNOB_SOURCES: tuple[tuple[str, str], ...] = (
    ("PARAKEET_CHUNK_S_FILE", ENV_CHUNK_S),
    ("PARAKEET_OVERLAP_S_FILE", ENV_OVERLAP_S),
    ("SUMMARIZE_TIMEOUT_S_FILE", ENV_TIMEOUT_S),
    ("SUMMARIZE_GGUF_CTX_FILE", ENV_GGUF_CTX),
)
_knob_memo: tuple[tuple, dict[str, float | int]] | None = None


def _knob_values() -> dict[str, float | int]:
    """The four operator knobs as the dashboard renders them, memoised per tick."""
    global _knob_memo
    # The file attr is resolved at call time (tests repoint the config paths),
    # and the signature carries the path so a repointed file can't hit a stale
    # entry from the previous one.
    key = tuple(
        (file_stat_sig(getattr(config, attr), include_path=True), os.environ.get(env))
        for attr, env in _KNOB_SOURCES
    )
    if _knob_memo is not None and _knob_memo[0] == key:
        return _knob_memo[1]
    values: dict[str, float | int] = {
        "parakeet_chunk_s": current_parakeet_chunk_s(),
        # The overlap IN FORCE — `current_parakeet_overlap_s` applies the joint
        # chunk/overlap clamp, so the card can't advertise an overlap every
        # transcribe silently reduces.
        "parakeet_overlap_s": current_parakeet_overlap_s(),
        "summarize_timeout_s": current_summarize_timeout_s(),
        "summarize_gguf_ctx": default_gguf_ctx(),
    }
    _knob_memo = (key, values)
    return values


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


def live_identities_of(active: list[dict[str, Any]]) -> set[str]:
    """The identities with an open tap, read off the same rows `/api/state` ships.

    The People join treats a live identity as present-in-the-meeting (ADR-0009),
    and the route needs this set BEFORE the projection runs — the registry sync
    is a mutation and must stay on the event loop. One function called from both
    places, over one `active_rows` output, is what makes "the set matches the
    rows" true rather than merely documented.
    """
    return {row["identity"] for row in active}


@dataclass(frozen=True, kw_only=True)
class StateInputs:
    """Everything one /api/state tick projects from, snapshotted.

    One value object rather than thirteen keyword arguments: the route reads a
    Recorder at ONE instant and hands the projection a frozen record of that
    instant. That is what makes the thread hop safe to reason about — nothing the
    worker touches is still being mutated — and what lets a CLI, a queue worker
    or a test build a tick by hand. `build_state_blob` no longer needs a
    keyword-only signature to keep a transposition unwritable, but `kw_only=True`
    below is what moved that guarantee here rather than dropping it: four fields
    are `list[dict[str, Any]]` (`active`, `sessions_list`, `occs`, `live_feed`),
    so a positional constructor would let two of them swap and type-check. Being
    a named field is not the guarantee; being unconstructible positionally is.

    `live_identities` is a PROPERTY, not a field. It must be the identity set of
    the open taps; while it was a thirteenth parameter that was a docstring
    promise the type could not keep, and a caller handing the join a set from a
    previous tick would render a Person "live" with no tap open. Derived, the two
    cannot disagree.

    The live channel arrives as a `LiveSnapshot` (`live.py`), which owns how a
    channel is read and how much log a poll ships. `live_feed` stays its own
    field: those settled lines come from the Recorder's `LiveTranscripts`, a
    different owner, so a snapshot carrying both could not be captured in one
    call.

    Frozen for the declaration, not for deep immutability: the list and dict
    fields stay mutable, and `frozen=True`'s synthesised `__hash__` raises on
    them — this is a record of one instant, not a cache key.

    One consequence worth knowing before reusing an instance: `build_state_blob`
    is NOT replayable over the same object. `attach_people_view` writes `names`
    into each entry of `sessions_list` and pops its `roster` back off, so a
    second projection sees sessions stripped of the roster the People join reads.
    Each poll builds a fresh listing, so this never bites in the route — but
    `dataclasses.replace` SHARES the list with the original, so a test sweeping
    variants off one baseline must rebuild it per case rather than replacing into
    it (`_peopled_inputs` in the unit tests does exactly that).
    """

    current_session: str
    active: list[dict[str, Any]]
    sessions_list: list[dict[str, Any]]
    registry: PeopleRegistry
    occs: list[dict[str, Any]]
    live_feed: list[dict[str, Any]]
    live: LiveSnapshot
    recording_enabled: bool
    backend: str
    available_backends: list[str]

    @property
    def live_identities(self) -> set[str]:
        return live_identities_of(self.active)


def build_state_blob(inputs: StateInputs) -> tuple[bytes, str]:
    """Config reads, pure people joins, payload assembly, and ETag serialization.

    Takes the tick as one `StateInputs`: `sessions_list` is pre-gathered, the
    registry is pre-synced (that mutation ran on the event loop), and every
    recorder-owned input is a snapshot. Returns (body_bytes, etag_string)."""
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

    sessions_list = inputs.sessions_list
    people = attach_people_view(sessions_list, inputs.registry, inputs.occs, inputs.live_identities)

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
        "current_session": inputs.current_session,
        "active": inputs.active,
        "sessions": sessions_list,
        "people": people,
        "default_override_counts": override_counts,
        "live_feed": inputs.live_feed,
        "live_info": inputs.live.info,
        "live_log": inputs.live.log,
        "live_supports_native_vad": inputs.live.supports_native_vad,
        "backend": inputs.backend,
        "available_backends": inputs.available_backends,
        "recording_enabled": inputs.recording_enabled,
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
        **_knob_values(),
        # Same registry-filtered view /api/languages serves — one helper, so the
        # Settings readout and the Transcript one can't drift.
        "specialists": effective_specialists(),
    }
    body = json.dumps(jsonable_encoder(payload), separators=(",", ":")).encode("utf-8")
    etag = 'W/"' + hashlib.blake2b(body, digest_size=12).hexdigest() + '"'
    return body, etag
