"""RED contract for issue #215 — the poll walk re-reads session-meta.json and
session-roster.json (and re-runs strptime on every WAV name) for EVERY session on
EVERY ~0.5 s tick.

`gather_sessions` memoises the per-WAV descriptors and the transcript/summary/
strip-meta sidecars on stat signatures, but `_describe_session` still reads
session-meta.json (via `read_session_meta` → `_read_json_or_none`) and
session-roster.json (via `read_roster` → `path.read_text`) UNCACHED per session
per tick, and recomputes `starts = [parse_wav_start(w["name"]) for w in wavs]`
via strptime even though each cached WAV descriptor already carries its
`wav_start` ISO. On a large archive that is an O(sessions) disk-read + parse storm
every tick, forever.

The fix routes read_session_meta and read_roster through the existing
`_read_session_json_cached` stat-sig cache (both files are written via
`atomic_write_text`, so the (mtime_ns, size) signature always moves on change —
the same invariant the transcript/summary/strip-meta caches already rely on), and
derives earliest/latest from the cached descriptor's `wav_start` instead of
re-parsing the WAV name.

These tests pin the OBSERVABLE poll-path waste at the read seam the suite's other
cache tests use (`sessions._read_json_or_none`, the reader
`_read_session_json_cached` funnels a cache MISS through) and at
`sessions.parse_wav_start`: a warm second `gather_sessions` walk over unchanged
sidecars must re-parse NOTHING. A staleness guardrail pins that the cache still
invalidates when a sidecar is rewritten (stat-sig-keyed, not cached forever) — the
same cache-hit + invalidate-on-change shape the rest of test_poll_caching locks in.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from wav_builders import seed_wav  # type: ignore[import-not-found]

from tapscribe import config as _config
from tapscribe import sessions
from tapscribe.session_paths import FILENAME_META_JSON, FILENAME_ROSTER_JSON

_SESSION = "20260101T010000Z"


@pytest.fixture(autouse=True)
def _clear_poll_caches():
    """Each test starts and ends with the module-level poll caches empty, so a
    warm tick is only warm because THIS test's first walk populated it."""

    def _reset():
        sessions._WAV_DESC_CACHE.clear()
        sessions._SESSION_JSON_CACHE.clear()

    _reset()
    yield
    _reset()


def _seed_session(root: Path) -> Path:
    """A session dir with one WAV, a session-meta.json, and a session-roster.json."""
    sd = root / _SESSION
    sd.mkdir()
    seed_wav(sd / "2026-01-01T01-00-00Z_alice_abc_u1.wav")
    (sd / FILENAME_META_JSON).write_text(json.dumps({"label": "Standup"}), encoding="utf-8")
    (sd / FILENAME_ROSTER_JSON).write_text(
        json.dumps({"alice": {"name": "Alice", "source": "recorded", "slug": "alice", "wavs": ["abc.wav"]}}),
        encoding="utf-8",
    )
    return sd


def test_session_meta_not_reparsed_on_a_warm_poll_tick(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """session-meta.json must be parsed once and then served from the stat-sig
    cache on the next walk — not re-read + re-parsed every tick."""
    monkeypatch.setattr(_config, "RECORDINGS_DIR", tmp_path)
    _seed_session(tmp_path)

    calls = {"n": 0}
    real = sessions._read_json_or_none

    def _spy(p):
        if str(p).endswith(FILENAME_META_JSON):
            calls["n"] += 1
        return real(p)

    monkeypatch.setattr(sessions, "_read_json_or_none", _spy)

    out1 = sessions.gather_sessions(current_session=_SESSION)
    out2 = sessions.gather_sessions(current_session=_SESSION)

    assert calls["n"] == 1, (
        "session-meta.json was re-parsed on a warm poll tick — route it through the "
        "stat-sig cache so an unchanged sidecar is a dict lookup, not a per-tick disk read"
    )
    # The cache must still surface the right value, not a stale/empty stand-in.
    assert out1[0]["session_meta"].get("label") == "Standup"
    assert out2[0]["session_meta"].get("label") == "Standup"


def test_roster_not_reparsed_on_a_warm_poll_tick(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """session-roster.json must go through the same stat-sig cache
    (`_read_session_json_cached` funnels a miss through `_read_json_or_none`), so a
    warm walk re-parses it zero times rather than doing a plain read_text per tick."""
    monkeypatch.setattr(_config, "RECORDINGS_DIR", tmp_path)
    _seed_session(tmp_path)

    calls = {"n": 0}
    real = sessions._read_json_or_none

    def _spy(p):
        if str(p).endswith(FILENAME_ROSTER_JSON):
            calls["n"] += 1
        return real(p)

    monkeypatch.setattr(sessions, "_read_json_or_none", _spy)

    out1 = sessions.gather_sessions(current_session=_SESSION)
    out2 = sessions.gather_sessions(current_session=_SESSION)

    assert calls["n"] == 1, (
        "session-roster.json was not served from the shared stat-sig cache on a warm "
        "poll tick — route read_roster through `_read_session_json_cached` like the "
        "other sidecars so an unchanged roster is not re-read every tick"
    )
    assert out1[0]["roster"]["alice"]["name"] == "Alice"
    assert out2[0]["roster"]["alice"]["name"] == "Alice"


def test_roster_coercion_branch_table_holds_through_the_poll_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The cached poll path must coerce roster entries with the SAME branch table
    as `read_roster` — source not in {recorded,live} -> 'live', non-str name -> '',
    non-str wav member dropped, non-dict entry skipped. Pinning a coercion BRANCH
    (not just a happy-path value) forbids a hand-rolled/looser mirror of the
    coercion from passing green while diverging from `roster.coerce_roster` on junk
    input — the whole point of routing both paths through one shared coercer."""
    monkeypatch.setattr(_config, "RECORDINGS_DIR", tmp_path)
    sd = tmp_path / _SESSION
    sd.mkdir()
    seed_wav(sd / "2026-01-01T01-00-00Z_alice_abc_u1.wav")
    (sd / FILENAME_ROSTER_JSON).write_text(
        json.dumps(
            {
                "alice": {"name": 123, "source": "bogus", "slug": "alice", "wavs": ["ok.wav", 7]},
                "bob": "not-a-dict",
            }
        ),
        encoding="utf-8",
    )

    roster = sessions.gather_sessions(current_session=_SESSION)[0]["roster"]

    assert "bob" not in roster, "a non-dict entry must be dropped, not carried through"
    entry = roster["alice"]
    assert entry["source"] == "live", "an out-of-allowlist source must coerce to 'live'"
    assert entry["name"] == "", "a non-str name must coerce to ''"
    assert entry["wavs"] == ["ok.wav"], "non-str wav members must be dropped"


def test_wav_start_not_recomputed_via_strptime_on_a_warm_tick(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """`_describe_session` derives earliest/latest from the WAV descriptors. On a
    warm tick the descriptors are cached, so their `wav_start` must be reused — not
    recomputed by re-running `parse_wav_start` (strptime) over every WAV name."""
    monkeypatch.setattr(_config, "RECORDINGS_DIR", tmp_path)
    sd = tmp_path / _SESSION
    sd.mkdir()
    seed_wav(sd / "2026-01-01T01-00-00Z_alice_abc_u1.wav")
    seed_wav(sd / "2026-01-01T01-05-00Z_bob_def_u2.wav")

    calls = {"n": 0}
    real = sessions.parse_wav_start

    def _spy(name):
        calls["n"] += 1
        return real(name)

    monkeypatch.setattr(sessions, "parse_wav_start", _spy)

    sessions.gather_sessions(current_session=_SESSION)  # tick 1 warms the per-WAV descriptor cache
    calls["n"] = 0  # count ONLY the warm tick
    out = sessions.gather_sessions(current_session=_SESSION)  # tick 2: fully warm

    assert calls["n"] == 0, (
        "parse_wav_start (strptime) ran again on a warm tick — derive earliest/latest "
        "from each cached descriptor's wav_start ISO instead of re-parsing the WAV name"
    )
    # ...and the derived bounds are still correct (earliest = the 01:00:00 WAV).
    # Pin the exact ISO round-trip: each descriptor stores
    # `parse_wav_start(name).isoformat()` — a fixed-width UTC seconds-precision
    # string — so lexicographic min/max must surface that value verbatim.
    assert out[0]["earliest_iso"] == "2026-01-01T01:00:00+00:00"
    assert out[0]["latest_iso"] == "2026-01-01T01:05:00+00:00"


def test_meta_and_roster_caches_invalidate_when_a_sidecar_is_rewritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Staleness guardrail: caching the sidecars must NOT serve stale data. A
    rewrite (operator renames the session / a new occurrence rewrites the roster)
    changes the file's (mtime, size), so the next walk must reflect the new
    content — distinguishing the stat-sig cache from a cache-forever fix that would
    pass the warm-tick counts above while freezing the dashboard."""
    monkeypatch.setattr(_config, "RECORDINGS_DIR", tmp_path)
    sd = _seed_session(tmp_path)

    first = sessions.gather_sessions(current_session=_SESSION)[0]
    assert first["session_meta"].get("label") == "Standup"
    assert first["roster"]["alice"]["name"] == "Alice"

    # Rewrite both sidecars with different-length content so (mtime, size) moves.
    (sd / FILENAME_META_JSON).write_text(json.dumps({"label": "Renamed Standup"}), encoding="utf-8")
    (sd / FILENAME_ROSTER_JSON).write_text(
        json.dumps({"alice": {"name": "Alice Cooper", "source": "recorded", "slug": "alice", "wavs": []}}),
        encoding="utf-8",
    )

    second = sessions.gather_sessions(current_session=_SESSION)[0]
    assert second["session_meta"].get("label") == "Renamed Standup", (
        "session-meta cache served stale data after a rewrite"
    )
    assert second["roster"]["alice"]["name"] == "Alice Cooper", (
        "session-roster cache served stale data after a rewrite"
    )


def test_cached_roster_preserves_the_full_coercion_taxonomy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The cached roster read must apply the SAME coercion branch table as the
    uncached `roster.read_roster` — not pass the raw values through, nor a
    hand-rolled copy that can drift from it. Pin representative branches: an
    unknown source coerces to 'live', a non-str name to '', a non-list wavs to [].
    The value-only assertions above (name == 'Alice') would pass a divergent
    coercion, so this pins the taxonomy they miss."""
    monkeypatch.setattr(_config, "RECORDINGS_DIR", tmp_path)
    sd = tmp_path / _SESSION
    sd.mkdir()
    seed_wav(sd / "2026-01-01T01-00-00Z_alice_abc_u1.wav")
    (sd / FILENAME_ROSTER_JSON).write_text(
        json.dumps({"alice": {"name": 123, "source": "bogus", "slug": "alice", "wavs": "notalist"}}),
        encoding="utf-8",
    )

    entry = sessions.gather_sessions(current_session=_SESSION)[0]["roster"]["alice"]
    assert entry["source"] == "live", "an unknown roster source must coerce to 'live' (not passed through)"
    assert entry["name"] == "", "a non-str roster name must coerce to ''"
    assert entry["wavs"] == [], "a non-list roster wavs must coerce to []"
