"""End-to-end pipeline test: bridge → WAV → live feed → transcribed session.

Runs a real uvicorn server (via the `running_recorder` fixture), opens
one or two `/tap` WebSockets from real `websockets` clients, streams
WAV-derived PCM into them, then walks the same HTTP routes the
dashboard's JavaScript walks to surface state.

The default path uses a FakeTranscriber so the suite stays runnable
without faster-whisper / mlx-whisper installed. A second test
(`test_pipeline_with_real_whisper`) is gated by the `real_audio` marker;
it runs when both (a) real audio fixtures exist under
`tests/fixtures/audio/` and (b) `faster_whisper` is importable.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import re
import wave
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
import websockets

from tapscribe import transcribers as _transcribers

from .conftest import RunningRecorder
from .fake_transcriber import FakeTranscriber
from .harness import (
    SAMPLE_RATE,
    stream_wav_via_tap,
    streams_drained,
    synth_speech_like_wav,
    wait_until,
)

ALICE_SCRIPTED_TEXT = "The quick brown fox jumps over the lazy dog."
BOB_SCRIPTED_TEXT = "Hello operator, this is a transcription pipeline check."

# Keys match the safe_name() of each display name — that's what the
# recorder bakes into the WAV filename and what parse_wav_speaker_slug
# recovers when the FakeTranscriber looks up its scripted text.
FAKE_TEXT_BY_SPEAKER = {
    "Alice": ALICE_SCRIPTED_TEXT,
    "Bob": BOB_SCRIPTED_TEXT,
}


@pytest.fixture
def fake_transcriber(monkeypatch: pytest.MonkeyPatch) -> FakeTranscriber:
    """Replace the real factory + cache with a single FakeTranscriber the
    test owns. The orchestrator module imported `load_transcriber` at
    module load, so we patch both the canonical reference and that
    local binding."""
    fake = FakeTranscriber(text_by_speaker=FAKE_TEXT_BY_SPEAKER)

    def _factory(model_name: str, **_kwargs) -> FakeTranscriber:  # noqa: ARG001
        return fake

    monkeypatch.setattr(_transcribers, "load_transcriber", _factory)
    import tapscribe.batch_transcribe as _bt

    monkeypatch.setattr(_bt, "load_transcriber", _factory)
    _transcribers.clear_cache()
    return fake


@pytest.fixture
def synthetic_wavs(tmp_path: Path) -> dict[str, Path]:
    """Two short 16 kHz mono int16 WAVs the bridges will stream. Loud
    enough to clear SILENT_RMS_DBFS_FLOOR; the FakeTranscriber path
    doesn't care about content."""
    return {
        "alice": synth_speech_like_wav(tmp_path / "alice.wav", seconds=0.8, freq_hz=220.0),
        "bob": synth_speech_like_wav(tmp_path / "bob.wav", seconds=0.6, freq_hz=440.0),
    }


async def test_two_bridges_stream_then_session_is_transcribed(
    running_recorder: RunningRecorder,
    fake_transcriber: FakeTranscriber,
    synthetic_wavs: dict[str, Path],
):
    """Headline E2E: two bridges, real server, every dashboard surface.

    Walks `/api/state.active` mid-stream, `/api/state.live_feed`
    attribution, on-disk WAVs, `/api/wav` download, the
    transcribe-entire-session button, and the pause toggle.
    """
    rec = running_recorder.recorder
    fake_wlk = running_recorder.fake_wlk
    base = running_recorder.base_url
    ws_base = running_recorder.ws_base_url

    async with httpx.AsyncClient(base_url=base, timeout=10.0) as client:
        # Pace frames so the WSes overlap long enough for /api/state to
        # see both. 40 frames × 25 ms ≈ 1 s of wall clock — plenty.
        alice_task = asyncio.create_task(
            stream_wav_via_tap(
                ws_base_url=ws_base,
                identity="alice",
                name="Alice",
                wav_path=synthetic_wavs["alice"],
                utterance_id="utt-alice-1",
                frame_interval_s=0.025,
            )
        )
        bob_task = asyncio.create_task(
            stream_wav_via_tap(
                ws_base_url=ws_base,
                identity="bob",
                name="Bob",
                wav_path=synthetic_wavs["bob"],
                utterance_id="utt-bob-1",
                frame_interval_s=0.025,
            )
        )

        async def _both_active() -> bool:
            resp = await client.get("/api/state")
            ids = {row["identity"] for row in resp.json().get("active", [])}
            return {"alice", "bob"}.issubset(ids)

        assert await wait_until(_both_active, timeout=3.0), (
            "expected /api/state.active to surface both bridges mid-stream"
        )

        # The fake WlK broadcasts to every connected relay, so each
        # push lands twice in the feed — once per speaker.
        fake_wlk.push_committed("first settled line")
        fake_wlk.push_committed("second settled line")

        await asyncio.gather(alice_task, bob_task)
        assert await wait_until(lambda: streams_drained(rec), timeout=5.0)

        state = (await client.get("/api/state")).json()
        feed = state["live_feed"]
        feed_texts = {(e["identity"], e["text"]) for e in feed}
        assert ("alice", "first settled line") in feed_texts
        assert ("bob", "first settled line") in feed_texts
        assert ("alice", "second settled line") in feed_texts
        assert ("bob", "second settled line") in feed_texts
        for entry in feed:
            assert entry["session"] == rec.session_start
            assert entry["name"] in ("Alice", "Bob")
            assert "ts" in entry

        wavs = sorted(rec.session_dir.glob("*.wav"))
        assert len(wavs) == 2, f"expected 2 WAVs, got {[p.name for p in wavs]}"
        alice_wav = next(p for p in wavs if "Alice" in p.name)
        bob_wav = next(p for p in wavs if "Bob" in p.name)
        for wav, expected_seconds in [(alice_wav, 0.8), (bob_wav, 0.6)]:
            with wave.open(str(wav), "rb") as w:
                assert w.getframerate() == SAMPLE_RATE
                assert w.getnchannels() == 1
                assert w.getsampwidth() == 2
                # Trailing partial 20 ms frame is dropped by the bridge,
                # so allow one frame's worth of slack on the low side.
                expected_frames = int(expected_seconds * SAMPLE_RATE)
                assert expected_frames - 320 <= w.getnframes() <= expected_frames

        resp = await client.get(f"/api/wav/{rec.session_start}/{alice_wav.name}")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "audio/wav"
        assert resp.content == alice_wav.read_bytes()

        resp = await client.post(
            "/api/transcribe-session",
            json={"session": rec.session_start, "model": "fake-small.en"},
            timeout=30.0,
        )
        assert resp.status_code == 200, resp.text
        merged = resp.json()

        assert merged["session"] == rec.session_start
        assert merged["transcriber"] == "fake-whisper"
        assert merged["wav_count"] == 2
        assert set(merged["speakers"]) == {"Alice", "Bob"}
        assert set(merged["speaking_seconds"].keys()) == {"Alice", "Bob"}
        assert ALICE_SCRIPTED_TEXT in merged["plain_text"]
        assert BOB_SCRIPTED_TEXT in merged["plain_text"]
        assert {s["speaker"] for s in merged["segments"]} == {"Alice", "Bob"}

        assert (rec.session_dir / "session-transcript.json").is_file()
        assert (rec.session_dir / "session-transcript.txt").is_file()
        from tapscribe.wav_cache import read_cached

        for wav in wavs:
            assert read_cached(wav) is not None, f"no cached transcript for {wav.name}"

        resp = await client.post("/api/recording/toggle", json={"enabled": False})
        assert resp.json() == {"ok": True, "enabled": False}
        assert rec.recording_enabled is False

        # The send side may or may not raise depending on which side
        # closes first; the load-bearing assertion is "no new WAV".
        before = len(list(rec.session_dir.glob("*.wav")))
        try:
            await stream_wav_via_tap(
                ws_base_url=ws_base,
                identity="alice",
                name="Alice",
                wav_path=synthetic_wavs["alice"],
                utterance_id="utt-while-paused",
                frame_interval_s=0.025,
            )
        except websockets.ConnectionClosed:
            pass
        assert len(list(rec.session_dir.glob("*.wav"))) == before

        await client.post("/api/recording/toggle", json={"enabled": True})
        assert rec.recording_enabled is True

        # Second transcribe-session should hit the per-WAV cache —
        # the fake's call count must not advance.
        prior_calls = len(fake_transcriber.calls)
        resp = await client.post(
            "/api/transcribe-session",
            json={"session": rec.session_start, "model": "fake-small.en"},
            timeout=30.0,
        )
        assert resp.status_code == 200
        assert len(fake_transcriber.calls) == prior_calls, (
            "second transcribe-session should hit the per-WAV cache"
        )


async def test_session_label_persists_through_meta_endpoint(
    running_recorder: RunningRecorder,
    fake_transcriber: FakeTranscriber,  # noqa: ARG001 — keeps the patched factory
    synthetic_wavs: dict[str, Path],
):
    """The dashboard's "name this session" input PUTs to
    `/api/session-meta/{session}`. Round-trip it across a real bridge
    cycle so the meta endpoint stays wired."""
    rec = running_recorder.recorder
    base = running_recorder.base_url

    await stream_wav_via_tap(
        ws_base_url=running_recorder.ws_base_url,
        identity="alice",
        name="Alice",
        wav_path=synthetic_wavs["alice"],
        utterance_id="utt-label-test",
    )
    assert await wait_until(lambda: streams_drained(rec), timeout=3.0)

    async with httpx.AsyncClient(base_url=base, timeout=5.0) as client:
        resp = await client.put(
            f"/api/session-meta/{rec.session_start}",
            json={"label": "Kickoff meeting"},
        )
        assert resp.status_code == 200
        assert resp.json()["meta"]["label"] == "Kickoff meeting"

        sessions = (await client.get("/api/state")).json()["sessions"]
        session = next(s for s in sessions if s["session"] == rec.session_start)
        assert session["session_meta"].get("label") == "Kickoff meeting"


async def test_two_detached_sessions_capture_concurrently_without_cross_leak(
    running_recorder: RunningRecorder,
    fake_transcriber: FakeTranscriber,
    synthetic_wavs: dict[str, Path],
):
    """Per-bridge isolation, full stack: two bridges, each carrying its OWN
    detached ?session=<id>, stream CONCURRENTLY into one real Recorder.

    This is the structural promise per-bridge Sessions exist for (PRD #99
    user story 13 — "two concurrent meetings produce two clean sessions
    instead of one muddled folder"). The route suite pins it with the
    serialized TestClient (`test_session_param_isolates_tap_from_concurrent_
    global_tap`); here it runs as real, overlapping WS streams on a real
    event loop, and each session is transcribed independently so a cross-leak
    would show up in the merged transcript, not just on disk.
    """
    rec = running_recorder.recorder
    fake_wlk = running_recorder.fake_wlk
    base = running_recorder.base_url
    ws_base = running_recorder.ws_base_url

    async with httpx.AsyncClient(base_url=base, timeout=10.0) as client:
        # Two detached sessions of the bridges' own. Neither rotates the
        # global current session — the no-yank property of #100.
        global_session = rec.session_start
        sid_alice = (await client.post("/api/tap/new-session", json={"detached": True})).json()["session"]
        sid_bob = (await client.post("/api/tap/new-session", json={"detached": True})).json()["session"]
        assert sid_alice != sid_bob, "two detached creates must mint distinct sessions"
        assert rec.session_start == global_session, "detached create must not rotate the global session"

        # Stream BOTH bridges at once, each pinned to its own detached session.
        # Pace frames so the WSes overlap long enough for /api/state and the
        # relay broadcast to see both (mirrors the two-bridges test).
        alice_task = asyncio.create_task(
            stream_wav_via_tap(
                ws_base_url=ws_base,
                identity="alice",
                name="Alice",
                wav_path=synthetic_wavs["alice"],
                utterance_id="utt-alice-detached",
                session=sid_alice,
                frame_interval_s=0.025,
            )
        )
        bob_task = asyncio.create_task(
            stream_wav_via_tap(
                ws_base_url=ws_base,
                identity="bob",
                name="Bob",
                wav_path=synthetic_wavs["bob"],
                utterance_id="utt-bob-detached",
                session=sid_bob,
                frame_interval_s=0.025,
            )
        )

        async def _both_active() -> bool:
            resp = await client.get("/api/state")
            ids = {row["identity"] for row in resp.json().get("active", [])}
            return {"alice", "bob"}.issubset(ids)

        assert await wait_until(_both_active, timeout=3.0), (
            "expected /api/state.active to surface both concurrent detached-session bridges"
        )

        # Settled lines broadcast to every open relay; each tap's live feed
        # entry must be stamped with ITS OWN detached session, not the global.
        fake_wlk.push_committed("settled line")

        await asyncio.gather(alice_task, bob_task)
        assert await wait_until(lambda: streams_drained(rec), timeout=5.0)

        # 1) WAVs landed in the RIGHT folders — and nowhere else.
        alice_wavs = list((rec.recordings_dir / sid_alice).glob("*.wav"))
        bob_wavs = list((rec.recordings_dir / sid_bob).glob("*.wav"))
        assert len(alice_wavs) == 1 and "Alice" in alice_wavs[0].name, [p.name for p in alice_wavs]
        assert len(bob_wavs) == 1 and "Bob" in bob_wavs[0].name, [p.name for p in bob_wavs]
        # The global current session captured nothing — no leak across the bracket.
        assert list((rec.recordings_dir / global_session).glob("*.wav")) == []

        # 2) Live feed attribution is per detached session (issue #100's
        #    `test_detached_tap_live_captions_attributed_to_its_session`,
        #    here through the real relay over two concurrent taps).
        feed = (await client.get("/api/state")).json()["live_feed"]
        by_identity = {e["identity"]: e["session"] for e in feed if e["text"] == "settled line"}
        assert by_identity.get("alice") == sid_alice, feed
        assert by_identity.get("bob") == sid_bob, feed

        # 3) Each session transcribes INDEPENDENTLY: its merged transcript
        #    holds only its own speaker + scripted text. A cross-leak would
        #    surface the other speaker's line here.
        merged_alice = (
            await client.post(
                "/api/transcribe-session",
                json={"session": sid_alice, "model": "fake-small.en"},
                timeout=30.0,
            )
        ).json()
        merged_bob = (
            await client.post(
                "/api/transcribe-session",
                json={"session": sid_bob, "model": "fake-small.en"},
                timeout=30.0,
            )
        ).json()

        assert merged_alice["session"] == sid_alice
        assert set(merged_alice["speakers"]) == {"Alice"}
        assert ALICE_SCRIPTED_TEXT in merged_alice["plain_text"]
        assert BOB_SCRIPTED_TEXT not in merged_alice["plain_text"]

        assert merged_bob["session"] == sid_bob
        assert set(merged_bob["speakers"]) == {"Bob"}
        assert BOB_SCRIPTED_TEXT in merged_bob["plain_text"]
        assert ALICE_SCRIPTED_TEXT not in merged_bob["plain_text"]


# Real-audio variant: gated by both a fixture and the optional dependency.
# When either is missing the test is skipped with a clear message; adding
# real audio later is just dropping a file + reference transcript in
# tests/fixtures/audio/ (see README there).

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "audio"

# Match alphabetic runs only — Whisper output has punctuation, the
# reference has parenthetical clauses; we want lowercased word tokens.
_WORD_RE = re.compile(r"[^\W\d_]+", flags=re.UNICODE)


@dataclass
class AudioFixture:
    """One real-audio fixture: a WAV and the text of what it says."""

    wav: Path
    reference: str


def _real_audio_fixtures() -> list[AudioFixture]:
    """Walk FIXTURES_DIR for `<name>.wav` paired with a non-empty
    `<name>.reference.txt`."""
    if not FIXTURES_DIR.is_dir():
        return []
    out: list[AudioFixture] = []
    for wav in sorted(FIXTURES_DIR.glob("*.wav")):
        ref_file = wav.with_suffix(".reference.txt")
        if not ref_file.is_file():
            continue
        text = ref_file.read_text(encoding="utf-8").strip()
        if text:
            out.append(AudioFixture(wav=wav, reference=text))
    return out


def _word_tokens(text: str, *, min_len: int = 4) -> set[str]:
    """Lowercased alphabetic word tokens of at least `min_len` chars."""
    return {m.group(0).lower() for m in _WORD_RE.finditer(text) if len(m.group(0)) >= min_len}


@pytest.mark.real_audio
async def test_pipeline_with_real_whisper(running_recorder: RunningRecorder):
    """Run the full pipeline against real audio with a real Whisper backend.

    Skipped unless `faster_whisper` is importable and at least one
    `<name>.wav` + `<name>.reference.txt` pair sits under
    `tests/fixtures/audio/` (see README there).

    The "we heard something real" check is intentionally soft: each
    fixture's per-WAV sidecar must contain at least one ≥ 4-char word
    from the reference transcript. tiny/base Whisper models can't
    produce verbatim output on noisy speech (and confuse Scandinavian
    languages outright), so an exact word-for-word assertion just
    flakes. Catching one shared meaningful word still rules out
    silence-floor failures and full-on hallucinations.
    """
    if importlib.util.find_spec("faster_whisper") is None:
        pytest.skip("faster_whisper not installed — install with `pip install -e .[whisper]`")
    fixtures = _real_audio_fixtures()
    if not fixtures:
        pytest.skip(
            "no real-audio fixtures present — add one via tests/fixtures/audio/README.md to enable",
        )

    rec = running_recorder.recorder
    base = running_recorder.base_url
    ws_base = running_recorder.ws_base_url

    for idx, fx in enumerate(fixtures):
        await stream_wav_via_tap(
            ws_base_url=ws_base,
            identity=f"fixture-{idx}",
            name=fx.wav.stem,
            wav_path=fx.wav,
            utterance_id=f"utt-{idx}",
        )
    assert await wait_until(lambda: streams_drained(rec), timeout=10.0)
    assert len(list(rec.session_dir.glob("*.wav"))) == len(fixtures)

    async with httpx.AsyncClient(base_url=base, timeout=600.0) as client:
        resp = await client.post(
            "/api/transcribe-session",
            json={"session": rec.session_start, "model": "base"},
        )
        assert resp.status_code == 200, resp.text

    from tapscribe.wav_cache import read_cached

    sidecars_by_speaker = {}
    for wav in rec.session_dir.glob("*.wav"):
        cached = read_cached(wav)
        if cached is None:
            continue
        sidecars_by_speaker[cached.speaker_name] = cached

    for fx in fixtures:
        sidecar = sidecars_by_speaker.get(fx.wav.stem)
        assert sidecar is not None, (
            f"no sidecar for speaker {fx.wav.stem!r} (have: {sorted(sidecars_by_speaker)})"
        )
        transcript = sidecar.result.text
        reference_words = _word_tokens(fx.reference)
        transcript_words = _word_tokens(transcript)
        overlap = reference_words & transcript_words
        assert overlap, (
            f"{fx.wav.name}: no ≥ 4-char reference word appears in "
            f"transcript.\n  reference: {fx.reference!r}\n"
            f"  transcript: {transcript!r}\n"
            f"  reference words: {sorted(reference_words)}\n"
            f"  transcript words: {sorted(transcript_words)}"
        )

    on_disk = json.loads((rec.session_dir / "session-transcript.json").read_text(encoding="utf-8"))
    assert on_disk["wav_count"] == len(fixtures)
    assert len(on_disk["plain_text"]) > 20, (
        f"session transcript suspiciously short: {on_disk['plain_text']!r}"
    )


@pytest.mark.real_audio
async def test_candidate_languages_control_real_whisper_on_norwegian_audio(
    running_recorder: RunningRecorder,
):
    """ADR-0010 end-to-end with a REAL Whisper backend on the real Norwegian
    fixture (`marlene-nb`): the operator's declared candidate-language set,
    carried per-meeting on session-meta, actually controls the model — through
    the real `/api/session-meta` + `/api/transcribe-session` routes, the
    resolution layer, and the faster-whisper adapter.

    Two guarantees, both on the confusable da/no pair where Whisper's own
    auto-detect is unreliable (the bug this feature fixes):

    1. A singleton `{no}` candidate set PINS the per-region run to Norwegian —
       the per-WAV sidecar's resolved language is exactly `no`, and the text is
       Norwegian (a reference word survives). If any link in the chain dropped
       the declared language, base Whisper would be free to mis-detect the clip
       (it confuses Scandinavian languages outright) and this would fail.
    2. A `{da, no}` set CONSTRAINS detection to that set — a forced re-run's
       resolved language stays WITHIN `{da, no}`, never drifting to e.g. `en`
       or `sv`. (Disambiguating da-vs-no itself is slice 2's nb-whisper job;
       v1's guarantee is "stays within the declared set".)

    Skipped unless `faster_whisper` is importable and the `marlene-nb` fixture
    is present (the `real_audio` gate, same as the sibling tests).
    """
    if importlib.util.find_spec("faster_whisper") is None:
        pytest.skip("faster_whisper not installed — install with `pip install -e .[whisper-cpu]`")
    nb = next((fx for fx in _real_audio_fixtures() if fx.wav.stem == "marlene-nb"), None)
    if nb is None:
        pytest.skip("marlene-nb fixture absent — add tests/fixtures/audio/marlene-nb.wav to enable")

    rec = running_recorder.recorder
    base = running_recorder.base_url
    ws_base = running_recorder.ws_base_url

    await stream_wav_via_tap(
        ws_base_url=ws_base,
        identity="nb",
        name="marlene-nb",
        wav_path=nb.wav,
        utterance_id="utt-nb",
    )
    assert await wait_until(lambda: streams_drained(rec), timeout=10.0)
    wav = next(rec.session_dir.glob("*.wav"))

    from tapscribe.wav_cache import read_cached

    async with httpx.AsyncClient(base_url=base, timeout=600.0) as client:
        # (1) Singleton {no} → pin. A multilingual checkpoint ("base", not
        # "base.en") so the language is genuinely the operator's to set.
        r = await client.put(f"/api/session-meta/{rec.session_start}", json={"languages": ["no"]})
        assert r.status_code == 200, r.text
        r = await client.post(
            "/api/transcribe-session",
            json={"session": rec.session_start, "model": "base", "force": True},
        )
        assert r.status_code == 200, r.text

        pinned = read_cached(wav)
        assert pinned is not None, "no sidecar after the {no} run"
        assert pinned.result.language == "no", (
            f"declaring {{no}} must pin the per-region run to Norwegian, "
            f"got language={pinned.result.language!r}"
        )
        # It really transcribed Norwegian, not just stamped a language code.
        assert _word_tokens(nb.reference) & _word_tokens(pinned.result.text), (
            f"pinned-Norwegian transcript shares no reference word: {pinned.result.text!r}"
        )

        # (2) {da, no} → constrained auto-detect. Force a fresh run so the
        # constrained detect actually executes (not a cache hit on (1)).
        r = await client.put(f"/api/session-meta/{rec.session_start}", json={"languages": ["da", "no"]})
        assert r.status_code == 200, r.text
        r = await client.post(
            "/api/transcribe-session",
            json={"session": rec.session_start, "model": "base", "force": True},
        )
        assert r.status_code == 200, r.text

        constrained = read_cached(wav)
        assert constrained is not None, "no sidecar after the {da,no} run"
        assert constrained.result.language in {"da", "no"}, (
            f"declaring {{da, no}} must keep the result WITHIN the set (no drift "
            f"to e.g. en/sv), got language={constrained.result.language!r}"
        )


def _english_fixtures() -> list[AudioFixture]:
    """English fixtures only. Parakeet (`parakeet-tdt-0.6b-v3`) covers 25 EU
    languages but NOT Norwegian, so the `-nb` clip would legitimately
    mis-transcribe; the `-en` suffix convention (see fixtures README) picks
    the ones Parakeet can handle."""
    return [fx for fx in _real_audio_fixtures() if fx.wav.stem.endswith("-en")]


@pytest.mark.real_audio
async def test_pipeline_with_real_parakeet(running_recorder: RunningRecorder):
    """Full pipeline against real audio with the real `transformers` Parakeet
    backend (`backend="parakeet-hf"`): bridge `/tap` → finalized WAV on disk
    → `POST /api/transcribe-session` → merged session transcript + per-WAV
    sidecar. The non-MLX counterpart to `test_pipeline_with_real_whisper`,
    and the end-to-end proof that the NeMo→transformers migration works
    through the real route, factory, cache, and merge.

    Skipped unless `transformers` + `librosa` are importable and at least one
    English `<name>-en.wav` + `<name>-en.reference.txt` pair sits under
    `tests/fixtures/audio/`.

    Two layers of assertion. (1) Content: each fixture's sidecar transcript
    must share at least one ≥ 4-char word with the reference — soft, like the
    Whisper test, because a 0.6 B model on a 12 s NASA clip won't be verbatim,
    but enough to rule out silence / hallucination / a broken bridge.
    (2) Structure: Parakeet's headline feature is real word-level timestamps,
    so at least one segment must carry `words`, and every segment/word time
    must land inside the clip — proof the token→word→segment aggregation
    survived the full round-trip, not just the unit test's mocked decode.
    """
    if importlib.util.find_spec("transformers") is None or importlib.util.find_spec("librosa") is None:
        pytest.skip("transformers/librosa not installed — install with `pip install -e .[parakeet-cpu]`")
    fixtures = _english_fixtures()
    if not fixtures:
        pytest.skip(
            "no English real-audio fixtures present — add one via tests/fixtures/audio/README.md to enable",
        )

    rec = running_recorder.recorder
    base = running_recorder.base_url
    ws_base = running_recorder.ws_base_url

    for idx, fx in enumerate(fixtures):
        await stream_wav_via_tap(
            ws_base_url=ws_base,
            identity=f"fixture-{idx}",
            name=fx.wav.stem,
            wav_path=fx.wav,
            utterance_id=f"utt-{idx}",
        )
    assert await wait_until(lambda: streams_drained(rec), timeout=10.0)
    assert len(list(rec.session_dir.glob("*.wav"))) == len(fixtures)

    async with httpx.AsyncClient(base_url=base, timeout=600.0) as client:
        resp = await client.post(
            "/api/transcribe-session",
            json={"session": rec.session_start, "model": "parakeet-tdt-0.6b-v3"},
        )
        assert resp.status_code == 200, resp.text

    from tapscribe.wav_cache import read_cached

    sidecars_by_speaker = {}
    for wav in rec.session_dir.glob("*.wav"):
        cached = read_cached(wav)
        if cached is None:
            continue
        sidecars_by_speaker[cached.speaker_name] = cached

    for fx in fixtures:
        sidecar = sidecars_by_speaker.get(fx.wav.stem)
        assert sidecar is not None, (
            f"no sidecar for speaker {fx.wav.stem!r} (have: {sorted(sidecars_by_speaker)})"
        )
        result = sidecar.result

        # It ran on the transformers backend, not a fallback or a fake.
        assert result.backend == "parakeet-hf", (
            f"{fx.wav.name}: expected backend 'parakeet-hf', got {result.backend!r}"
        )

        # (1) Content — soft word overlap with the reference.
        reference_words = _word_tokens(fx.reference)
        transcript_words = _word_tokens(result.text)
        overlap = reference_words & transcript_words
        assert overlap, (
            f"{fx.wav.name}: no ≥ 4-char reference word appears in "
            f"transcript.\n  reference: {fx.reference!r}\n"
            f"  transcript: {result.text!r}\n"
            f"  reference words: {sorted(reference_words)}\n"
            f"  transcript words: {sorted(transcript_words)}"
        )

        # (2) Structure — real word-level timestamps, all inside the clip.
        assert result.segments, f"{fx.wav.name}: Parakeet produced no segments"
        segs_with_words = [s for s in result.segments if s.words]
        assert segs_with_words, (
            f"{fx.wav.name}: no segment carried word-level timestamps "
            "(Parakeet's headline feature didn't survive the pipeline)"
        )
        ceiling = result.duration + 0.5  # one rounding-step of slack at the tail
        for seg in result.segments:
            assert 0.0 <= seg.start <= seg.end <= ceiling, (
                f"{fx.wav.name}: segment time {seg.start}-{seg.end} outside [0, {ceiling}]"
            )
            for word in seg.words or ():
                assert 0.0 <= word.start <= word.end <= ceiling, (
                    f"{fx.wav.name}: word {word.word!r} time {word.start}-{word.end} outside [0, {ceiling}]"
                )

    on_disk = json.loads((rec.session_dir / "session-transcript.json").read_text(encoding="utf-8"))
    assert on_disk["wav_count"] == len(fixtures)
    assert len(on_disk["plain_text"]) > 20, (
        f"session transcript suspiciously short: {on_disk['plain_text']!r}"
    )


# ---------------------------------------------------------------------------
# Multi-language cover + per-region selector (ADR-0010 slice 2), end-to-end
# through the REAL routes, cover, selector, cache, and merge.
#
# Two e2e proofs, mirroring the slice-1 pair:
#  - a DETERMINISTIC one that always runs in CI — real bridge → WAV → cache →
#    merge, with controlled-confidence fakes per cover model so the routing
#    outcome is exact; it streams the committed da/no/en fixtures so the whole
#    bridge/recorder path runs on real audio bytes.
#  - a REAL-BACKEND one (real_audio-gated) that runs a real Whisper generalist
#    AND a real NB-Whisper specialist on the fixtures, proving the wiring with
#    actual models — both run on every region, the selector lands a primary,
#    the merge stitches real content.
# ---------------------------------------------------------------------------


class _ConfidenceFake:
    """A `Transcriber` whose per-segment avg_logprob is chosen by which marker
    substring is in the WAV name — so the acoustic-confidence selector's winner
    is deterministic per region. Distinct (backend, model) per instance, the way
    two real cover models land as separate sidecars. `scored=False` emits
    segments with NO avg_logprob, modelling a Parakeet/Voxtral generalist."""

    name = "fake-whisper"

    def __init__(
        self,
        *,
        backend: str,
        model: str,
        logprob_by_marker: dict[str, float],
        language_by_marker: dict[str, str] | None = None,
        scored: bool = True,
    ):
        self.backend = backend
        self.model_name = model
        self.device = "fake-cpu"
        self.logprob_by_marker = logprob_by_marker
        # The per-region detected language the generalist reports — the
        # SpecialistRoutingSelector's routing key; defaults to "no".
        self.language_by_marker = language_by_marker or {}
        self.scored = scored

    def transcribe(
        self,
        path: Path,
        *,
        initial_prompt: str | None = None,
        hotwords: str | None = None,
        source_lang: str | None = None,
        target_lang: str | None = None,  # noqa: ARG002 — protocol parity
    ):
        from tapscribe.audio import wav_duration_s
        from tapscribe.transcribers.base import TranscriptionSegment, build_transcription_result

        # Derive the marker from BOTH maps' keys — an unscored generalist has an
        # empty logprob map but still needs its per-region language matched.
        marker = next(
            (m for m in {*self.logprob_by_marker, *self.language_by_marker} if m in path.name), None
        )
        logprob = self.logprob_by_marker.get(marker, -1.0) if self.scored else None
        language = self.language_by_marker.get(marker) or source_lang or "no"
        text = f"{self.model_name}:{marker or 'unknown'}"
        duration = wav_duration_s(path) or 1.0
        return build_transcription_result(
            self,
            text=text,
            segments=(
                TranscriptionSegment(start=0.0, end=round(duration, 2), text=text, avg_logprob=logprob),
            ),
            duration=round(duration, 2),
            language=language,
            language_probability=1.0,
            initial_prompt=initial_prompt,
            hotwords=hotwords,
            source_lang=source_lang,
        )


def _cover_fixture_pair() -> tuple[Path, Path] | None:
    """The committed (Norwegian, English) fixture WAVs, or None if either is
    absent — both are in recorder wire format so they stream through /tap."""
    nb = FIXTURES_DIR / "marlene-nb.wav"
    en = FIXTURES_DIR / "armstrong-en.wav"
    return (nb, en) if (nb.is_file() and en.is_file()) else None


def _reference_for(stem: str) -> str:
    """The reference transcript for a committed fixture by stem (e.g.
    'marlene-nb'), or '' if absent."""
    return next((fx.reference for fx in _real_audio_fixtures() if fx.wav.stem == stem), "")


async def _stream_cover_pair(running_recorder: RunningRecorder, nb: Path, en: Path) -> tuple[Path, Path]:
    """Stream the Norwegian + English fixtures through /tap and return their
    finalized on-disk WAV paths (marlene = Norwegian, armstrong = English)."""
    rec = running_recorder.recorder
    ws_base = running_recorder.ws_base_url
    await stream_wav_via_tap(
        ws_base_url=ws_base, identity="nb", name="marlene-nb", wav_path=nb, utterance_id="utt-nb"
    )
    await stream_wav_via_tap(
        ws_base_url=ws_base, identity="en", name="armstrong-en", wav_path=en, utterance_id="utt-en"
    )
    assert await wait_until(lambda: streams_drained(rec), timeout=10.0)
    wavs = list(rec.session_dir.glob("*.wav"))
    marlene = next(w for w in wavs if "marlene" in w.name)
    armstrong = next(w for w in wavs if "armstrong" in w.name)
    return marlene, armstrong


def _install_cover_fakes(monkeypatch, *, generalist: _ConfidenceFake, specialist: _ConfidenceFake) -> None:
    """Patch `load_transcriber` (both bindings) to dispatch the cover models to
    `specialist` (by its model id) else `generalist` — the way the batch path
    loads each cover model in turn."""

    def _factory(model_name: str, **_kwargs):
        return specialist if model_name == specialist.model_name else generalist

    monkeypatch.setattr(_transcribers, "load_transcriber", _factory)
    import tapscribe.batch_transcribe as _bt

    monkeypatch.setattr(_bt, "load_transcriber", _factory)
    _transcribers.clear_cache()


async def _run_cover_session(
    base: str, session: str, *, model: str, languages: tuple[str, ...] = ("no", "en"), timeout: float = 30.0
) -> dict:
    """Declare `languages` on the session, then transcribe it with `model`
    through the real routes; assert both return 200 and return the merged dict."""
    async with httpx.AsyncClient(base_url=base, timeout=timeout) as client:
        r = await client.put(f"/api/session-meta/{session}", json={"languages": list(languages)})
        assert r.status_code == 200, r.text
        r = await client.post("/api/transcribe-session", json={"session": session, "model": model})
        assert r.status_code == 200, r.text
        return r.json()


async def test_cover_routes_each_region_to_its_best_model_e2e(running_recorder: RunningRecorder, monkeypatch):
    """Deterministic slice-2 headline through the real stack: a {no, en}
    meeting transcribes EVERY region with both the generalist ("base") and the
    Norwegian specialist ("nb-whisper-medium"), and the selector points each
    region's _primary at the higher-confidence transcript — so the Norwegian
    clip's merged text comes from nb-whisper and the English clip's from the
    generalist, in one `/api/transcribe-session` call. Streams the committed
    fixtures so the bridge → WAV → cache → merge path runs on real audio."""
    pair = _cover_fixture_pair()
    if pair is None:
        pytest.skip("cover fixtures (marlene-nb.wav + armstrong-en.wav) absent")
    nb, en = pair

    generalist = _ConfidenceFake(
        backend="faster-whisper",
        model="base",
        logprob_by_marker={"marlene": -0.90, "armstrong": -0.10},
        language_by_marker={"marlene": "no", "armstrong": "en"},
    )
    specialist = _ConfidenceFake(
        backend="faster-whisper",
        model="nb-whisper-medium",
        logprob_by_marker={"marlene": -0.20, "armstrong": -0.80},
    )
    _install_cover_fakes(monkeypatch, generalist=generalist, specialist=specialist)

    rec = running_recorder.recorder
    base = running_recorder.base_url
    marlene, armstrong = await _stream_cover_pair(running_recorder, nb, en)
    merged = await _run_cover_session(base, rec.session_start, model="base")

    from tapscribe.wav_cache import read_all_cached, read_cached

    # Both cover models ran on EVERY region — two sidecars apiece.
    assert {c.result.model for c in read_all_cached(marlene)} == {"base", "nb-whisper-medium"}
    assert {c.result.model for c in read_all_cached(armstrong)} == {"base", "nb-whisper-medium"}
    # …and _primary points at the per-region winner.
    assert read_cached(marlene).result.model == "nb-whisper-medium"
    assert read_cached(armstrong).result.model == "base"
    # The merged transcript stitched the WINNERS, not whichever model ran last.
    assert "nb-whisper-medium:marlene" in merged["plain_text"]
    assert "base:armstrong" in merged["plain_text"]
    assert "base:marlene" not in merged["plain_text"]
    on_disk = json.loads((rec.session_dir / "session-transcript.json").read_text(encoding="utf-8"))
    assert on_disk["wav_count"] == 2


@pytest.mark.real_audio
async def test_cover_real_whisper_plus_nb_specialist_e2e(running_recorder: RunningRecorder, monkeypatch):
    """The same flow with REAL models: a {no, en} meeting runs a real Whisper
    generalist ("base") AND the real NB-Whisper Norwegian specialist on every
    region, the selector lands a primary, and the merge stitches real content.

    HARD assertions are on the MECHANISM (both real models produced a sidecar
    per region; _primary is one of them; the merged transcript has real text).
    Which model WINS each region is the acoustic-confidence call the ADR flags
    as empirical / the human spot-check, so it is observed, not asserted, here.

    Patches the specialist table to nb-whisper-tiny for download speed (the
    production default nb-whisper-medium is heavier); pre-fetches its weights so
    the test SKIPS cleanly when offline rather than failing inside the route.
    Skipped unless faster-whisper is importable and the fixtures are present.
    """
    if importlib.util.find_spec("faster_whisper") is None:
        pytest.skip("faster_whisper not installed — install with `pip install -e .[whisper-cpu]`")
    pair = _cover_fixture_pair()
    if pair is None:
        pytest.skip("cover fixtures (marlene-nb.wav + armstrong-en.wav) absent")
    nb_fx, en_fx = pair

    # Shrink the specialist + pre-fetch its weights; skip if they can't be had.
    from tapscribe.transcribers import catalog

    monkeypatch.setitem(catalog.SPECIALIST_MODELS, "no", "nb-whisper-tiny")
    try:
        from tapscribe.nb_whisper import download_nb_whisper_ct2_dir

        await asyncio.to_thread(download_nb_whisper_ct2_dir, "nb-whisper-tiny")
    except Exception as e:  # noqa: BLE001 — any fetch failure (offline, hub down) → skip, not fail
        pytest.skip(f"nb-whisper-tiny weights unavailable (offline?): {e}")

    rec = running_recorder.recorder
    base = running_recorder.base_url
    marlene, armstrong = await _stream_cover_pair(running_recorder, nb_fx, en_fx)
    await _run_cover_session(base, rec.session_start, model="base", timeout=600.0)

    from tapscribe.wav_cache import read_all_cached, read_cached

    expected_models = {"base", "nb-whisper-tiny"}
    for wav in (marlene, armstrong):
        ran = {c.result.model for c in read_all_cached(wav)}
        assert ran == expected_models, f"{wav.name}: expected both cover models to run, got {ran}"
        primary = read_cached(wav)
        assert primary is not None and primary.result.model in expected_models, (
            f"{wav.name}: selector did not land a primary among the cover models"
        )

    # The NB-Whisper sidecar really transcribed the Norwegian clip as Norwegian
    # (it is pinned to 'no' by name) — a reference word survives.
    nb_sidecar = next(c for c in read_all_cached(marlene) if c.result.model == "nb-whisper-tiny")
    assert _word_tokens(_reference_for("marlene-nb")) & _word_tokens(nb_sidecar.result.text), (
        f"nb-whisper transcript shares no Norwegian reference word: {nb_sidecar.result.text!r}"
    )
    on_disk = json.loads((rec.session_dir / "session-transcript.json").read_text(encoding="utf-8"))
    assert on_disk["wav_count"] == 2
    assert len(on_disk["plain_text"]) > 20, on_disk["plain_text"]


async def test_cover_unscored_generalist_keeps_generalist_e2e(running_recorder: RunningRecorder, monkeypatch):
    """Cross-architecture safety end-to-end (the cross-architecture half of the
    cover): when the generalist's backend emits no avg_logprob (Parakeet /
    Voxtral), the acoustic selector keeps the GENERALIST on every region instead
    of handing them all to the scored nb-whisper specialist — which, pinned to
    Norwegian, would stitch Norwegian over the English clip. Both transcripts are
    still cached (a future text-LID selector / manual flip can use the nb one).
    Deterministic fakes prove the wiring; the real-Parakeet variant below proves
    it with actual models."""
    pair = _cover_fixture_pair()
    if pair is None:
        pytest.skip("cover fixtures (marlene-nb.wav + armstrong-en.wav) absent")
    nb, en = pair

    # An UNSCORED generalist (modelling Parakeet, which can't do Norwegian so it
    # detects English-ish — no "en" specialist, so routing defers to the acoustic
    # cross-arch guard) + a confident scored specialist.
    generalist = _ConfidenceFake(
        backend="parakeet-hf",
        model="parakeet-tdt-0.6b-v3",
        logprob_by_marker={},
        language_by_marker={"marlene": "en", "armstrong": "en"},
        scored=False,
    )
    specialist = _ConfidenceFake(
        backend="faster-whisper",
        model="nb-whisper-medium",
        logprob_by_marker={"marlene": -0.20, "armstrong": -0.20},
    )
    _install_cover_fakes(monkeypatch, generalist=generalist, specialist=specialist)

    rec = running_recorder.recorder
    base = running_recorder.base_url
    marlene, armstrong = await _stream_cover_pair(running_recorder, nb, en)
    await _run_cover_session(base, rec.session_start, model="parakeet-tdt-0.6b-v3")

    from tapscribe.wav_cache import read_all_cached, read_cached

    # Every region kept the (unscored) generalist — no region was wrongly handed
    # to the scored specialist…
    assert read_cached(marlene).result.model == "parakeet-tdt-0.6b-v3"
    assert read_cached(armstrong).result.model == "parakeet-tdt-0.6b-v3"
    # …while both transcripts remain cached for a future text-LID selector.
    assert {c.result.model for c in read_all_cached(marlene)} == {
        "parakeet-tdt-0.6b-v3",
        "nb-whisper-medium",
    }


@pytest.mark.real_audio
async def test_cover_real_parakeet_generalist_keeps_generalist_on_english_e2e(
    running_recorder: RunningRecorder, monkeypatch
):
    """The cross-architecture guard with REAL models: a real `transformers`
    Parakeet generalist (emits no avg_logprob) plus the real NB-Whisper
    specialist on the English fixture. The acoustic selector must keep Parakeet
    for the English region — NOT the scored nb-whisper, which would re-render the
    English audio as Norwegian. Proves Bug-1's fix end-to-end with actual decode
    output, not a `scored=False` fake.

    Patches the specialist to nb-whisper-tiny and pre-fetches both models' weights
    so the test SKIPS cleanly offline. Skipped unless transformers + librosa +
    faster-whisper are importable and the English fixture is present.
    """
    for mod in ("transformers", "librosa", "faster_whisper"):
        if importlib.util.find_spec(mod) is None:
            pytest.skip(f"{mod} not installed — install the parakeet/whisper extras to enable")
    en = FIXTURES_DIR / "armstrong-en.wav"
    if not en.is_file():
        pytest.skip("armstrong-en.wav fixture absent")

    from tapscribe.transcribers import catalog

    monkeypatch.setitem(catalog.SPECIALIST_MODELS, "no", "nb-whisper-tiny")
    try:
        from tapscribe.nb_whisper import download_nb_whisper_ct2_dir

        await asyncio.to_thread(download_nb_whisper_ct2_dir, "nb-whisper-tiny")
    except Exception as e:  # noqa: BLE001 — any fetch failure (offline, hub down) → skip, not fail
        pytest.skip(f"nb-whisper-tiny weights unavailable (offline?): {e}")

    rec = running_recorder.recorder
    base = running_recorder.base_url
    ws_base = running_recorder.ws_base_url
    await stream_wav_via_tap(
        ws_base_url=ws_base, identity="en", name="armstrong-en", wav_path=en, utterance_id="utt-en"
    )
    assert await wait_until(lambda: streams_drained(rec), timeout=10.0)
    armstrong = next(rec.session_dir.glob("*.wav"))
    await _run_cover_session(base, rec.session_start, model="parakeet-tdt-0.6b-v3", timeout=600.0)

    from tapscribe.wav_cache import read_all_cached, read_cached

    ran = {c.result.model for c in read_all_cached(armstrong)}
    assert ran == {"parakeet-tdt-0.6b-v3", "nb-whisper-tiny"}, (
        f"expected both real cover models to run, got {ran}"
    )
    # Parakeet emits no avg_logprob → the selector keeps it for the English region,
    # never the scored nb-whisper (which would be Norwegian garbage on English).
    primary = read_cached(armstrong)
    assert primary is not None and primary.result.model == "parakeet-tdt-0.6b-v3", (
        f"unscored Parakeet generalist must be kept, primary={primary.result.model if primary else None}"
    )
    # And it really transcribed English (a reference word survives).
    assert _word_tokens(_reference_for("armstrong-en")) & _word_tokens(primary.result.text), (
        f"Parakeet primary shares no English reference word: {primary.result.text!r}"
    )


# ---------------------------------------------------------------------------
# da/no routing BENCHMARK (ADR-0010). Real audio, real models, raw numbers.
#
# Streams the Danish (`solen-da`) + Norwegian (`marlene-nb`) fixtures as two
# regions of one meeting, declares {da, no}, runs the cover with REAL models,
# and measures — per region, per cover model — word-recall + WER against that
# region's reference, plus which transcript the selector chose. It then ASSERTS
# the routing is correct: each region's winner must be Danish-for-Danish /
# Norwegian-for-Norwegian (the confusable pair this feature exists to separate),
# not Norwegianised Danish. The PRD bar is 100% correct routing; this is the
# yardstick that proves it now and for every future model.
#
# Models are env-overridable so a new model is one run away:
#   TAPSCRIBE_BENCH_GENERALIST=parakeet-tdt-0.6b-v3 \
#   TAPSCRIBE_BENCH_NB=nb-whisper-large \
#   pytest tests/e2e/test_pipeline_e2e.py -k da_no_routing_benchmark -m real_audio -s
# Run with `-s` to see the table; it is also written to TAPSCRIBE_BENCH_OUT
# (default <repo>/.bench-da-no.json) for tracking across model versions.
# ---------------------------------------------------------------------------

BENCH_GENERALIST = os.environ.get("TAPSCRIBE_BENCH_GENERALIST", "large-v3-turbo")
BENCH_NB = os.environ.get("TAPSCRIBE_BENCH_NB", "nb-whisper-medium")
# The winning transcript must recover at least this fraction of the reference's
# ≥4-char content words — i.e. it actually transcribed the region's language,
# not a same-spelling neighbour.
BENCH_MIN_PRIMARY_RECALL = 0.55
# The selector must pick (essentially) the best-matching candidate for each
# region — the PRD's 100% bar: the Danish region gets the best Danish transcript
# and the Norwegian region the best Norwegian one. A small tolerance absorbs
# float noise / genuine ties; a real mis-route (e.g. picking the generalist's
# weaker Norwegian over the specialist's) exceeds it and fails.
BENCH_ROUTING_EPSILON = 0.02

_BENCH_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)


def _norm_words(text: str) -> list[str]:
    """Lowercased, punctuation-stripped word list for WER."""
    return _BENCH_PUNCT.sub("", text.lower()).split()


def _wer(reference: str, hypothesis: str) -> float:
    """Word error rate = word-level edit distance / reference length."""
    r, h = _norm_words(reference), _norm_words(hypothesis)
    if not r:
        return 0.0 if not h else 1.0
    prev = list(range(len(h) + 1))
    for i, rw in enumerate(r, 1):
        cur = [i]
        for j, hw in enumerate(h, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (rw != hw)))
        prev = cur
    return round(prev[-1] / len(r), 3)


def _word_recall(reference: str, hypothesis: str) -> float:
    """Fraction of the reference's ≥4-char content words present in the
    hypothesis — robust to da/no near-identity (the shared words match either
    way; the discriminating ones are what move this)."""
    ref = _word_tokens(reference)
    if not ref:
        return 0.0
    return round(len(ref & _word_tokens(hypothesis)) / len(ref), 3)


@pytest.mark.real_audio
async def test_da_no_routing_benchmark(running_recorder: RunningRecorder, monkeypatch):
    """Measure + assert correct da/no routing on real Danish + Norwegian audio
    with real models. Reports raw per-region/per-model numbers and fails if any
    region's winner is not the best transcript for that region's language."""
    if importlib.util.find_spec("faster_whisper") is None:
        pytest.skip("faster_whisper not installed — install with `pip install -e .[whisper-cpu]`")
    da = next((fx for fx in _real_audio_fixtures() if fx.wav.stem == "solen-da"), None)
    no = next((fx for fx in _real_audio_fixtures() if fx.wav.stem == "marlene-nb"), None)
    if da is None or no is None:
        pytest.skip("da/no benchmark needs both solen-da.wav and marlene-nb.wav fixtures")

    # Route the specialist to the benchmark's nb model + pre-fetch its weights
    # (skip cleanly offline). The generalist downloads via faster-whisper on use.
    from tapscribe.transcribers import catalog

    monkeypatch.setitem(catalog.SPECIALIST_MODELS, "no", BENCH_NB)
    try:
        from tapscribe.nb_whisper import download_nb_whisper_ct2_dir

        await asyncio.to_thread(download_nb_whisper_ct2_dir, BENCH_NB)
    except Exception as e:  # noqa: BLE001 — offline / hub down → skip, not fail
        pytest.skip(f"{BENCH_NB} weights unavailable (offline?): {e}")

    rec = running_recorder.recorder
    base = running_recorder.base_url
    ws_base = running_recorder.ws_base_url
    for stem, fx in (("solen-da", da), ("marlene-nb", no)):
        await stream_wav_via_tap(
            ws_base_url=ws_base, identity=stem, name=stem, wav_path=fx.wav, utterance_id=f"utt-{stem}"
        )
    assert await wait_until(lambda: streams_drained(rec), timeout=15.0)

    # One cover run over {da, no} with the real generalist; the catalog adds the
    # nb specialist because "no" is declared.
    await _run_cover_session(
        base, rec.session_start, model=BENCH_GENERALIST, languages=("da", "no"), timeout=900.0
    )

    from tapscribe.wav_cache import read_all_cached, read_cached

    regions = [
        ("solen-da", "da", da.reference, next(w for w in rec.session_dir.glob("*.wav") if "solen" in w.name)),
        (
            "marlene-nb",
            "no",
            no.reference,
            next(w for w in rec.session_dir.glob("*.wav") if "marlene" in w.name),
        ),
    ]

    report: list[dict] = []
    lines = [
        "",
        f"=== da/no routing benchmark — generalist={BENCH_GENERALIST!r} specialist={BENCH_NB!r} ===",
    ]
    for stem, lang, ref, wav in regions:
        primary = read_cached(wav)
        cands = read_all_cached(wav)
        cand_rows = []
        for c in cands:
            is_primary = (
                primary is not None
                and c.result.backend == primary.result.backend
                and c.result.model == primary.result.model
            )
            cand_rows.append(
                {
                    "model": c.result.model,
                    "detected_language": c.result.language,
                    "recall": _word_recall(ref, c.result.text),
                    "wer": _wer(ref, c.result.text),
                    "is_primary": is_primary,
                    "text": c.result.text,
                }
            )
        cand_rows.sort(key=lambda r: r["recall"], reverse=True)
        best_recall = max((r["recall"] for r in cand_rows), default=0.0)
        primary_row = next((r for r in cand_rows if r["is_primary"]), None)
        primary_recall = primary_row["recall"] if primary_row else 0.0
        report.append(
            {
                "region": stem,
                "language": lang,
                "reference": ref,
                "candidates": cand_rows,
                "best_recall": best_recall,
                "primary_recall": primary_recall,
                "primary_is_best": primary_row is not None and primary_recall >= best_recall - 1e-9,
            }
        )
        lines.append(f"\n[{stem}] declared lang={lang}  reference: {ref!r}")
        for r in cand_rows:
            mark = " <- PRIMARY" if r["is_primary"] else ""
            lines.append(
                f"   {r['model']:<22} det={r['detected_language']:<3} "
                f"recall={r['recall']:.2f} wer={r['wer']:.2f}{mark}\n"
                f"      {r['text']!r}"
            )

    out_path = Path(
        os.environ.get("TAPSCRIBE_BENCH_OUT", Path(__file__).resolve().parents[2] / ".bench-da-no.json")
    )
    out_path.write_text(
        json.dumps(
            {"generalist": BENCH_GENERALIST, "specialist": BENCH_NB, "regions": report},
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    lines.append(f"\n(raw numbers written to {out_path})")
    print("\n".join(lines))

    # ── The PRD bar: every region's winner is correct for its language ──
    failures = []
    for r in report:
        if r["primary_recall"] < BENCH_MIN_PRIMARY_RECALL:
            failures.append(
                f"{r['region']}: winning transcript recall {r['primary_recall']:.2f} < "
                f"{BENCH_MIN_PRIMARY_RECALL} — the {r['language']} region was not transcribed correctly"
            )
        if r["primary_recall"] < r["best_recall"] - BENCH_ROUTING_EPSILON:
            failures.append(
                f"{r['region']}: selector picked a worse transcript (recall {r['primary_recall']:.2f}) "
                f"than the best candidate ({r['best_recall']:.2f}) — mis-routed"
            )
    assert not failures, (
        "da/no routing benchmark FAILED:\n  " + "\n  ".join(failures) + "\n" + "\n".join(lines)
    )
