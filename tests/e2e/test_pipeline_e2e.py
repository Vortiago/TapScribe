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
    """ADR-0009 end-to-end with a REAL Whisper backend on the real Norwegian
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
