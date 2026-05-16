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
    test owns. The route module imported `load_transcriber` at module
    load, so we patch both the canonical reference and that local
    binding."""
    fake = FakeTranscriber(text_by_speaker=FAKE_TEXT_BY_SPEAKER)

    def _factory(model_name: str, *, use_mlx: bool) -> FakeTranscriber:  # noqa: ARG001
        return fake

    monkeypatch.setattr(_transcribers, "load_transcriber", _factory)
    import tapscribe.app as _app

    monkeypatch.setattr(_app, "load_transcriber", _factory)
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
        for wav in wavs:
            assert wav.with_suffix(".json").is_file()

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

    sidecars_by_speaker = {}
    for p in rec.session_dir.glob("*.json"):
        if p.name == "session-transcript.json":
            continue
        data = json.loads(p.read_text(encoding="utf-8"))
        sidecars_by_speaker[data.get("speaker_name", "")] = data

    for fx in fixtures:
        sidecar = sidecars_by_speaker.get(fx.wav.stem)
        assert sidecar is not None, (
            f"no sidecar for speaker {fx.wav.stem!r} (have: {sorted(sidecars_by_speaker)})"
        )
        transcript = sidecar.get("text", "")
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
