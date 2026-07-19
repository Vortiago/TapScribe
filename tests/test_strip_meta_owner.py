"""RED contract for #367 — strip-meta.json gets ONE owner module.

`strip-meta.json` is read/written by five accessors across four modules
(sessions, session_merge, session_maintenance x2, plus the batch_strip
writer). This contract pins a deep `tapscribe.strip_meta` module that owns
the sidecar's format: the shape gate, the owner-by-clip reverse index, the
pure single-file reader, the RECORDINGS_DIR-contained read, the atomic
write, and the clip prune. Consumers cross this interface instead of the
raw file.

Seam convention pinned here (so the seams stay patchable): consumers reach
the owner through the module attribute — `import tapscribe.strip_meta as
strip_meta; strip_meta.read_strip_meta_file(...)` — NOT via
`from tapscribe.strip_meta import read_strip_meta_file`. The
`test_select_session_wavs_crosses_the_owner_seam` test patches the owner
module's attribute and expects `session_merge` to see it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import tapscribe.strip_meta as strip_meta

ORIGINAL_NAME = "2026-05-12T09-19-55Z_alice_ident01_00000001.wav"
ORIGINAL_2_NAME = "2026-05-12T09-21-30Z_bob_ident02_00000002.wav"
CLIP_NAME = "2026-05-12T09-20-00Z_alice_ident01_ab12cd34.wav"
CLIP_2_NAME = "2026-05-12T09-20-05Z_alice_ident01_ef56ab78.wav"
CLIP_3_NAME = "2026-05-12T09-21-40Z_bob_ident02_cd90ef12.wav"


def _span(clip: str) -> dict:
    return {"name": clip, "start_s": 0.0, "end_s": 1.0}


def _two_original_meta() -> dict:
    """files ordering deliberately disagrees with clip-name ordering so an
    implementation that keys on the wrong side can't pass by accident."""
    return {
        "stripped_at": "2026-05-12T09:22:00+00:00",
        "knobs": {"floor_dbfs": -40},
        "files": {
            ORIGINAL_2_NAME: {"spans": [_span(CLIP_3_NAME)]},
            ORIGINAL_NAME: {"spans": [_span(CLIP_NAME), _span(CLIP_2_NAME)]},
        },
    }


# ---------------------------------------------------------------------------
# The owner's format authority: shape gate + reverse index
# ---------------------------------------------------------------------------


def test_shape_gate_rejects_non_v2_and_passes_valid_identity():
    assert strip_meta.valid_strip_meta(None) is None
    assert strip_meta.valid_strip_meta("files") is None
    assert strip_meta.valid_strip_meta(["files"]) is None
    assert strip_meta.valid_strip_meta({}) is None
    # Legacy v1 carried a LIST under "files"; the v2 gate must reject it.
    assert strip_meta.valid_strip_meta({"files": []}) is None
    good = _two_original_meta()
    # Identity, not a copy: callers mutate the returned dict in place
    # (absorb merges entries) and then persist it.
    assert strip_meta.valid_strip_meta(good) is good


def test_owner_by_clip_reverse_index_tolerates_junk_spans():
    meta = {
        "files": {
            ORIGINAL_2_NAME: {
                "spans": [
                    _span(CLIP_3_NAME),
                    "hand-edited-junk",
                    {"start_s": 1.0},  # span without a name
                    {"name": 42},  # non-str name
                ]
            },
            ORIGINAL_NAME: {"spans": [_span(CLIP_NAME)]},
            "legacy-entry.wav": "not-a-dict",
            "no-spans.wav": {"wav_size": 1},
        }
    }
    assert strip_meta.strip_meta_owner_by_clip(meta) == {
        CLIP_3_NAME: ORIGINAL_2_NAME,
        CLIP_NAME: ORIGINAL_NAME,
    }


# ---------------------------------------------------------------------------
# The pure single-file reader (the purity crossing session_merge preserves)
# ---------------------------------------------------------------------------


def test_pure_file_reader_none_on_missing_unparseable_legacy(tmp_path: Path):
    p = tmp_path / "strip-meta.json"
    assert strip_meta.read_strip_meta_file(p) is None  # missing
    p.write_text("{not json", encoding="utf-8")
    assert strip_meta.read_strip_meta_file(p) is None  # unparseable
    p.write_text(json.dumps({"files": []}), encoding="utf-8")
    assert strip_meta.read_strip_meta_file(p) is None  # legacy shape
    good = _two_original_meta()
    p.write_text(json.dumps(good), encoding="utf-8")
    assert strip_meta.read_strip_meta_file(p) == good


# ---------------------------------------------------------------------------
# Contained read + atomic write round-trip
# ---------------------------------------------------------------------------


@pytest.fixture
def recordings_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from tapscribe import config as _config

    root = tmp_path / "recordings"
    root.mkdir()
    monkeypatch.setattr(_config, "RECORDINGS_DIR", root)
    return root


def test_contained_read_and_atomic_write_round_trip(recordings_root: Path, tmp_path: Path):
    stripped = recordings_root / "sess" / "stripped"
    stripped.mkdir(parents=True)
    assert strip_meta.read_strip_meta(stripped) is None

    meta = _two_original_meta()
    strip_meta.write_strip_meta(stripped, meta)
    assert strip_meta.read_strip_meta(stripped) == meta
    # Atomic write leaves no temp droppings behind.
    assert [p.name for p in stripped.iterdir()] == ["strip-meta.json"]

    # The contained reader keeps sessions' containment semantics: a sidecar
    # OUTSIDE RECORDINGS_DIR reads as absent, not as content.
    outside = tmp_path / "elsewhere" / "stripped"
    outside.mkdir(parents=True)
    (outside / "strip-meta.json").write_text(json.dumps(meta), encoding="utf-8")
    assert strip_meta.read_strip_meta(outside) is None


# ---------------------------------------------------------------------------
# Prune: the owner carries the span-drop semantics
# ---------------------------------------------------------------------------


def test_prune_clip_drops_span_keeps_siblings_and_drops_empty_entries(recordings_root: Path):
    stripped = recordings_root / "sess" / "stripped"
    stripped.mkdir(parents=True)
    meta = _two_original_meta()
    meta["files"]["legacy-entry.wav"] = "not-a-dict"
    (stripped / "strip-meta.json").write_text(json.dumps(meta), encoding="utf-8")

    # Dropping one of two spans keeps the entry (and the knobs/stamp).
    strip_meta.prune_clip(stripped, CLIP_NAME)
    after = json.loads((stripped / "strip-meta.json").read_text(encoding="utf-8"))
    assert [s["name"] for s in after["files"][ORIGINAL_NAME]["spans"]] == [CLIP_2_NAME]
    assert after["knobs"] == {"floor_dbfs": -40}
    assert after["files"]["legacy-entry.wav"] == "not-a-dict"

    # Dropping an original's LAST span drops the whole entry.
    strip_meta.prune_clip(stripped, CLIP_3_NAME)
    after = json.loads((stripped / "strip-meta.json").read_text(encoding="utf-8"))
    assert ORIGINAL_2_NAME not in after["files"]
    assert ORIGINAL_NAME in after["files"]


def test_prune_clip_is_a_noop_on_unknown_clip_missing_or_legacy_meta(recordings_root: Path):
    stripped = recordings_root / "sess" / "stripped"
    stripped.mkdir(parents=True)

    # No sidecar: nothing happens, and none is created.
    strip_meta.prune_clip(stripped, CLIP_NAME)
    assert not (stripped / "strip-meta.json").exists()

    # Legacy sidecar: left alone byte-for-byte.
    (stripped / "strip-meta.json").write_text(json.dumps({"files": []}), encoding="utf-8")
    before = (stripped / "strip-meta.json").read_text(encoding="utf-8")
    strip_meta.prune_clip(stripped, CLIP_NAME)
    assert (stripped / "strip-meta.json").read_text(encoding="utf-8") == before

    # Unknown clip: content unchanged.
    meta = _two_original_meta()
    (stripped / "strip-meta.json").write_text(json.dumps(meta), encoding="utf-8")
    strip_meta.prune_clip(stripped, "2026-05-12T09-59-59Z_alice_ident01_99999999.wav")
    assert json.loads((stripped / "strip-meta.json").read_text(encoding="utf-8")) == meta


# ---------------------------------------------------------------------------
# Consumers cross the owner, not the raw file
# ---------------------------------------------------------------------------


def test_sessions_no_longer_owns_the_accessors():
    """One authority: if sessions still exposes the accessor names (as
    re-exports for compatibility), they must BE the owner's objects — not
    parallel copies of the logic."""
    import tapscribe.sessions as sessions

    for name in ("valid_strip_meta", "strip_meta_owner_by_clip", "read_strip_meta"):
        if hasattr(sessions, name):
            assert getattr(sessions, name) is getattr(strip_meta, name), name


def test_select_session_wavs_crosses_the_owner_seam(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """session_merge keeps its deliberate purity (no RECORDINGS_DIR
    assumption) but must cross the OWNER's pure reader rather than
    re-reading the raw JSON: with NO sidecar on disk, a patched
    `strip_meta.read_strip_meta_file` supplies the clip->original mapping,
    and the silence gate must follow it to the (silent) true original."""
    from wav_builders import seed_silent_wav, seed_wav  # type: ignore[import-not-found]

    from tapscribe.session_merge import select_session_wavs

    session_dir = tmp_path / "session"
    stripped = session_dir / "stripped"
    stripped.mkdir(parents=True)
    seed_silent_wav(session_dir / ORIGINAL_NAME)
    seed_wav(stripped / CLIP_NAME)

    meta = {"files": {ORIGINAL_NAME: {"spans": [_span(CLIP_NAME)]}}}
    seen: list[Path] = []

    def fake_reader(path: Path):
        seen.append(path)
        return meta

    monkeypatch.setattr(strip_meta, "read_strip_meta_file", fake_reader)

    selection = select_session_wavs(session_dir, source="stripped")

    assert seen, "select_session_wavs never consulted the owner's reader"
    assert selection.wavs == ()
    assert CLIP_NAME in selection.skipped_silent


def test_delete_stripped_wav_prunes_via_the_owner(recordings_root: Path, monkeypatch: pytest.MonkeyPatch):
    """The maintenance delete flow routes clip-pruning through the owner:
    deleting a stripped region clip drops exactly its span from the sidecar
    (sibling spans and other originals untouched)."""
    from tapscribe.session_maintenance import delete_session_wav

    session = "sess"
    session_dir = recordings_root / session
    stripped = session_dir / "stripped"
    stripped.mkdir(parents=True)
    for clip in (CLIP_NAME, CLIP_2_NAME, CLIP_3_NAME):
        (stripped / clip).write_bytes(b"RIFFfake")
    (stripped / "strip-meta.json").write_text(json.dumps(_two_original_meta()), encoding="utf-8")

    calls: list[tuple[Path, str]] = []
    real_prune = strip_meta.prune_clip

    def spying_prune(stripped_dir_arg: Path, clip_name: str) -> None:
        calls.append((Path(stripped_dir_arg), clip_name))
        real_prune(stripped_dir_arg, clip_name)

    monkeypatch.setattr(strip_meta, "prune_clip", spying_prune)

    delete_session_wav(session, CLIP_NAME, source="stripped")

    assert calls == [(stripped, CLIP_NAME)]
    after = json.loads((stripped / "strip-meta.json").read_text(encoding="utf-8"))
    assert [s["name"] for s in after["files"][ORIGINAL_NAME]["spans"]] == [CLIP_2_NAME]
    assert [s["name"] for s in after["files"][ORIGINAL_2_NAME]["spans"]] == [CLIP_3_NAME]
