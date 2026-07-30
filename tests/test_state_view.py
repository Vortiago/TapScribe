"""Unit contract for the State view (#229, #365): the /api/state read model.

`GET /api/state` is the dashboard's ~2 Hz poll and the payload it returns was
assembled inline in `app.py`: config reads, the People join, the
default-override counts, the active-row overlay, the JSON serialization and the
ETag. Extracted into `tapscribe/state_view.py` so the projection is testable
without a route, a Recorder or a TestClient: one `StateInputs` — a frozen record
of everything recorder-owned at one instant — arrives from the route, which owns
the thread hops and the 304 branch.

What is pinned here is the part `test_routes.py` cannot see cheaply: the wire
contract's key set and order, the ETag contract (equal inputs give the same tag,
ANY changed input gives a different one, because the tag is a digest of the
body) and the three derivations that have no other owner (the live-identity set,
the override counts, and the active-row overlay with its byte bucketing).
"""

from __future__ import annotations

import json
from dataclasses import fields, replace
from datetime import UTC, datetime

import pytest

from tapscribe.live import LiveSnapshot
from tapscribe.people import PeopleRegistry
from tapscribe.recorder import ActiveStream, TapSetting
from tapscribe.state_view import (
    TAP_BYTES_BUCKET,
    StateInputs,
    active_rows,
    build_state_blob,
    live_identities_of,
)


def _live(**overrides) -> LiveSnapshot:
    """One tick's live-channel read, defaulted to a stopped channel."""
    kwargs = dict(info={"state": "stopped"}, log=[], supports_native_vad=False)
    kwargs.update(overrides)
    return LiveSnapshot(**kwargs)  # type: ignore[arg-type]


def _inputs(**overrides) -> StateInputs:
    """One /api/state tick with every input defaulted to an empty snapshot."""
    kwargs = dict(
        current_session="20260101T000000Z",
        active=[],
        sessions_list=[],
        registry=PeopleRegistry([]),
        occs=[],
        live_feed=[],
        live=_live(),
        recording_enabled=True,
        backend="cpu",
        available_backends=["cpu"],
    )
    kwargs.update(overrides)
    return StateInputs(**kwargs)  # type: ignore[arg-type]


def _blob(**overrides):
    """`build_state_blob` over a defaulted tick; `overrides` names only what the
    test is about."""
    return build_state_blob(_inputs(**overrides))


def _stream(**overrides) -> ActiveStream:
    """One open tap, defaulted; `overrides` names only what the test is about."""
    fields_ = dict(
        conn_id="c1",
        identity="alice",
        name="Alice",
        filename="a.wav",
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
        session="s1",
        record=True,
        live=True,
        level=0.0,
        bytes_received=0,
    )
    fields_.update(overrides)
    return ActiveStream(**fields_)


#: The payload's top-level keys, in the order `build_state_blob` writes them. The
#: ETag is a digest of the BYTES, so the ORDER is part of the wire format, and
#: `web/js/types.d.ts`'s `AppState` is a hand-maintained mirror of this set.
_PAYLOAD_KEYS = [
    "current_session",
    "active",
    "sessions",
    "people",
    "default_override_counts",
    "live_feed",
    "live_info",
    "live_log",
    "live_supports_native_vad",
    "backend",
    "available_backends",
    "recording_enabled",
    "prompt",
    "live_prompt",
    "hotwords",
    "inputs_support",
    "live_model_default",
    "batch_model_default",
    "batch_model_effective",
    "languages",
    "summarizer_default",
    "hallucinations",
    "idle_ttl_s",
    "parakeet_chunk_s",
    "parakeet_overlap_s",
    "summarize_timeout_s",
    "summarize_gguf_ctx",
    "specialists",
]


def test_the_payload_keys_are_the_wire_contract(tmp_config_dir):
    """The /api/state body is the dashboard's whole model of the Recorder. The
    ETag sweep below catches a key that stops varying with its input; only this
    catches one RENAMED or REORDERED, and either changes the bytes under every
    client that already holds an ETag for them."""
    assert list(json.loads(_blob()[0])) == _PAYLOAD_KEYS


def test_state_blob_is_json_with_an_etag_over_its_own_bytes(tmp_config_dir):
    body, etag = _blob()
    payload = json.loads(body)
    assert payload["current_session"] == "20260101T000000Z"
    assert payload["recording_enabled"] is True
    assert etag.startswith('W/"') and etag.endswith('"')
    # Same inputs, same tag: the poll's 304 path depends on this being stable
    # across ticks when nothing changed.
    assert _blob()[1] == etag


#: A one-session, one-person baseline. The three People-join inputs
#: (`sessions_list`, `occs`, and the derived live-identity set) are structurally
#: coupled: `occs` is one roster PER session, and neither a roster nor a live
#: identity reaches the payload unless there is a session to attach it to and a
#: registry that knows the identity. So the sweep below varies each input on top
#: of this, rather than pretending they are independent.
def _peopled_inputs(**overrides) -> StateInputs:
    kwargs = {
        "sessions_list": [{"session": "a", "session_meta": {}}],
        "occs": [{"alice": {"slug": "Alice", "name": "Alice"}}],
        "registry": PeopleRegistry([{"id": "p_a", "name": "Alice", "identities": ["alice"]}]),
    }
    kwargs.update(overrides)
    return _inputs(**kwargs)


def _peopled(**overrides):
    return build_state_blob(_peopled_inputs(**overrides))


#: One CHANGED value per LEAF input of `StateInputs`, each different from what
#: `_peopled()` passes. Every leaf appears exactly once, so a payload that stops
#: carrying one of them (a dropped key, a rename) is caught rather than leaving
#: the ETag stable for something the operator can actually change.
#:
#: `live_identities` is deliberately absent, and that is not lost coverage: it
#: stopped being an input when it became a property derived from `active`, so the
#: `active` case below now varies both at once. The two were never independent —
#: passing them separately only made it possible for them to disagree.
_CHANGED_INPUT = {
    "current_session": "20260101T010000Z",
    "active": [{"identity": "alice", "record": True, "live": True}],
    "sessions_list": [{"session": "a", "session_meta": {"prompt": "an override"}}],
    "registry": PeopleRegistry([{"id": "p_a", "name": "Renamed", "identities": ["alice"]}]),
    "occs": [{"bob": {"slug": "Bob", "name": "Bob"}}],
    "live_feed": [{"text": "hello"}],
    "live.info": {"state": "running"},
    "live.log": ["a log line"],
    "live.supports_native_vad": True,
    "recording_enabled": False,
    "backend": "cuda",
    "available_backends": ["cpu", "cuda"],
}


def _leaf_inputs() -> set[str]:
    """Every LEAF input of `StateInputs`, dotted through its one nested value
    object.

    `live` is ONE field but THREE payload inputs, so a guard that counted fields
    would let `live_log` stop reaching the payload with the table still
    "complete". Spelled out rather than walked reflectively: with one nesting the
    walk was the longer way round, and descending on `is_dataclass` would demand
    the table enumerate the internals of any input that later becomes a dataclass
    (`PeopleRegistry` is a plain class today) even though those are not
    independently-varying inputs.

    Every drift mode still fails, and loudly: a new field on either object shows
    up unmatched, and flattening `LiveSnapshot` back to a bare dict makes
    `fields()` raise rather than silently shrink the sweep.

    Derived properties (`live_identities`) are deliberately absent: they are not
    inputs, they are consequences of one, and the sweep covers them through the
    field they derive from.
    """
    outer = {f.name for f in fields(StateInputs)} - {"live"}
    return outer | {f"live.{f.name}" for f in fields(LiveSnapshot)}


def _with_input(inputs: StateInputs, dotted: str, value) -> StateInputs:
    """Replace ONE leaf input, addressed by the same dotted name `_leaf_inputs`
    derives ("live.log"), so the sweep can vary a nested field without knowing
    which value object owns it. `dataclasses.replace` is what the frozen value
    objects are FOR — rebuilding the whole object around the one changed field is
    how a sweep quietly starts varying two things."""
    group, _, leaf = dotted.partition(".")
    if not leaf:
        return replace(inputs, **{group: value})
    return replace(inputs, **{group: replace(getattr(inputs, group), **{leaf: value})})


def test_every_state_input_is_covered_by_the_etag_cases():
    """The table above must name every LEAF input, or the sweep below silently
    stops covering one. A new input to the payload arrives with a changed value
    here, in the same commit."""
    leaves = _leaf_inputs()
    assert set(_CHANGED_INPUT) == leaves, (
        f"missing from _CHANGED_INPUT: {sorted(leaves - set(_CHANGED_INPUT))}; "
        f"unknown: {sorted(set(_CHANGED_INPUT) - leaves)}"
    )


@pytest.mark.parametrize("field", sorted(_CHANGED_INPUT))
def test_state_blob_etag_changes_when_any_input_changes(tmp_config_dir, field):
    """ANY changed input yields a different tag, because the tag is a digest of
    the body. The dashboard's 304 path is only correct while that holds: an input
    that stops reaching the payload makes the poll answer 304 forever to a change
    the operator just made."""
    _, baseline = _peopled()
    _, changed = build_state_blob(_with_input(_peopled_inputs(), field, _CHANGED_INPUT[field]))
    assert changed != baseline, f"changing {field} left the ETag unchanged"


def test_live_identities_are_the_identity_set_of_the_open_taps():
    """`live_identities` is derived, not passed. The People join marks a Person
    live when one of its identities is streaming (ADR-0009), so a set that
    disagreed with `active` would render a Person live with no tap open — and
    while it was a thirteenth parameter, "these two agree" was a docstring
    promise the type could not keep. Two taps for one person collapse to one
    identity; no taps means nobody is live. The route derives its own copy
    through the same function, BEFORE this object exists (the registry sync is a
    mutation and must run on the event loop first), which is what makes the two
    call sites agree by construction."""
    assert _inputs(active=[]).live_identities == set()

    rows = [{"identity": "alice"}, {"identity": "bob"}, {"identity": "alice"}]
    assert _inputs(active=rows).live_identities == {"alice", "bob"}
    assert live_identities_of(rows) == _inputs(active=rows).live_identities


def test_an_open_tap_marks_its_person_live_in_the_payload(tmp_config_dir):
    """The derivation reaches the wire: the `people` row for an identity with an
    open tap carries `live: true`, and the same tick with no taps carries false.
    Pinned over the SERIALIZED body, so a derivation that is right in isolation
    but never reaches `attach_people_view` fails here rather than in the browser."""
    body, _ = _peopled(active=[{"identity": "alice"}])
    assert [(p["name"], p["live"]) for p in json.loads(body)["people"]] == [("Alice", True)]

    body, _ = _peopled()
    assert [p["live"] for p in json.loads(body)["people"]] == [False]


def test_state_blob_counts_per_session_default_overrides(tmp_config_dir):
    """`default_override_counts` tells the config card how many sessions
    override each global default. A summarizer override is EITHER a source or a
    prompt, so a session that sets both still counts once."""
    sessions = [
        {"session": "a", "session_meta": {"prompt": "hi"}},
        {"session": "b", "session_meta": {"prompt": "yo", "hotwords": "TapScribe"}},
        {"session": "c", "session_meta": {"summary_source": "local", "summary_prompt": "x"}},
        {"session": "d", "session_meta": {}},
        {"session": "e"},
    ]
    # `occs` is parallel to `sessions_list` (one roster per session); empty
    # rosters keep the People join a no-op so the counts are what's under test.
    body, _ = _blob(sessions_list=sessions, occs=[{} for _ in sessions])
    assert json.loads(body)["default_override_counts"] == {
        "prompt": 2,
        "hotwords": 1,
        "summarizer": 1,
    }


def test_active_rows_overlay_tap_settings_and_bucket_bytes():
    """Each open tap's row carries the operator's per-identity record/live
    preference (not the value captured at WS open) and a bucketed byte count:
    the raw counter is bumped per 20 ms frame, so shipping it verbatim would
    bust the response ETag on every poll of a quiet-but-open tap (#217)."""
    stream = _stream(identity="alice", name="Alice", level=0.123456, bytes_received=TAP_BYTES_BUCKET + 10)
    rows = active_rows([stream], {"alice": TapSetting(record=False, live=True)}.get)
    assert len(rows) == 1
    assert rows[0]["record"] is False
    assert rows[0]["live"] is True
    assert rows[0]["level"] == 0.12
    assert rows[0]["bytes_received"] == TAP_BYTES_BUCKET


def test_active_rows_bucketing_rounds_to_nearest():
    """Round to NEAREST bucket, not down: the constant is centred on
    TAP_BYTES_BUCKET // 2 so a tap just past the halfway mark reports the next
    bucket rather than under-reporting by almost a whole one."""

    def row(byte_count):
        stream = _stream(bytes_received=byte_count)
        return active_rows([stream], lambda _identity: TapSetting(record=True, live=True))[0]

    assert row(0)["bytes_received"] == 0
    assert row(TAP_BYTES_BUCKET // 2 - 1)["bytes_received"] == 0
    assert row(TAP_BYTES_BUCKET // 2)["bytes_received"] == TAP_BYTES_BUCKET
    assert row(TAP_BYTES_BUCKET * 3)["bytes_received"] == TAP_BYTES_BUCKET * 3
