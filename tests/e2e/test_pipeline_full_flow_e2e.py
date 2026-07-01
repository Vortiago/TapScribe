"""Full end-of-meeting flow E2E: record → strip-silence → transcribe → summarize.

Covers the parts of the user journey the cover/routing tests don't — the
strip-silence pre-step and the SUMMARIZE step — wired end to end through the real
HTTP routes. Transcription uses the deterministic FakeTranscriber and
summarization a deterministic INJECTED summarizer (no external LLM), so this runs
in CI and asserts the WIRING — a multi-speaker transcript flows through strip and
summarize, lands in the persisted summary, and is readable back — not summary
quality (that's the benchmarks' job, against a real LLM).
"""

from __future__ import annotations

import json
import wave
from pathlib import Path

import httpx
import numpy as np

from tapscribe.summarizers.local import LocalSummarizer

from .conftest import RunningRecorder
from .fake_transcriber import FakeTranscriber
from .harness import SAMPLE_RATE, stream_wav_via_tap, streams_drained, wait_until
from .test_pipeline_e2e import fake_transcriber  # noqa: F401 — re-exported pytest fixture

# 3 speakers, 3 languages — each line carries one distinctive ≥4-char word the
# test can trace from transcript → merge → summary.
MEETING = {
    "Lars": "Vi diskuterer budsjettet for det kommende kvartalet",  # da-ish
    "Ola": "Vi planlegger lanseringen av det nye produktet",  # no
    "John": "We should finalize the roadmap before launch",  # en
}
DISTINCTIVE = {"Lars": "budsjettet", "Ola": "lanseringen", "John": "roadmap"}


def _burst_wav(path: Path, *, seconds: float = 0.8, amplitude: int = 12000) -> Path:
    """A single speech burst (square wave) — clears the RMS floor, so
    strip-silence keeps it as one speech region per speaker."""
    n = int(seconds * SAMPLE_RATE)
    burst = np.tile(np.array([amplitude, -amplitude], dtype=np.int16), n // 2 + 1)[:n]
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(burst.tobytes())
    return path


async def test_full_meeting_flow_strip_transcribe_summarize_e2e(
    running_recorder: RunningRecorder,
    fake_transcriber: FakeTranscriber,  # noqa: F811 — re-imported pytest fixture
    monkeypatch,
    tmp_path: Path,
):
    """Record a 3-speaker meeting, then drive the real routes end to end:
    strip-silence → transcribe(stripped) → summarize. Asserts every speaker's
    content reaches the persisted summary — the strip + summarize wiring the
    routing tests don't exercise."""
    rec = running_recorder.recorder
    base = running_recorder.base_url
    ws_base = running_recorder.ws_base_url

    fake_transcriber.text_by_speaker.update(MEETING)

    # Inject a DETERMINISTIC summarizer (no external LLM): a marker prefix + the
    # transcript, so the test proves the merged transcript reached the summarizer
    # and the result was persisted + readable — without asserting LLM quality.
    def _fake_load_summarizer(*_args, **_kwargs):
        return LocalSummarizer(
            generate_fn=lambda transcript, _prompt: f"SUMMARY[{len(transcript.split())}w]: {transcript}"
        )

    monkeypatch.setattr("tapscribe.batch_summarize.load_summarizer", _fake_load_summarizer)

    for i, speaker in enumerate(MEETING):
        await stream_wav_via_tap(
            ws_base_url=ws_base,
            identity=speaker.lower(),
            name=speaker,
            wav_path=_burst_wav(tmp_path / f"{speaker}.wav"),
            utterance_id=f"utt-{i}",
        )
    assert await wait_until(lambda: streams_drained(rec), timeout=10.0)
    assert len(list(rec.session_dir.glob("*.wav"))) == 3

    async with httpx.AsyncClient(base_url=base, timeout=60.0) as client:
        # 1) strip-silence → one region WAV per speaker burst
        r = await client.post(
            f"/api/sessions/{rec.session_start}/strip-silence",
            json={"min_silence_ms": 400, "pad_ms": 50, "speech_floor_db": -40.0},
        )
        assert r.status_code == 200, r.text
        region_wavs = sorted((rec.session_dir / "stripped").glob("*.wav"))
        assert len(region_wavs) == 3, f"expected one region per speaker, got {[w.name for w in region_wavs]}"

        # 2) transcribe the stripped regions → merged multi-speaker transcript
        r = await client.post(
            "/api/transcribe-session",
            json={"session": rec.session_start, "model": "fake-small.en", "source": "stripped"},
        )
        assert r.status_code == 200, r.text
        merged = r.json()
        assert merged["source"] == "stripped"
        for speaker, word in DISTINCTIVE.items():
            assert word in merged["plain_text"], f"{speaker}'s content ({word!r}) missing from merged transcript"

        # 3) summarize the merged transcript
        r = await client.post(f"/api/sessions/{rec.session_start}/summarize", json={})
        assert r.status_code == 200, r.text
        summ = r.json()

    # The summarizer ran (marker) and every speaker's content flowed into it.
    assert summ["summary"].startswith("SUMMARY["), summ["summary"][:60]
    for speaker, word in DISTINCTIVE.items():
        assert word in summ["summary"], f"{speaker}'s content ({word!r}) missing from summary"

    # And it was persisted to session-summary.json, readable independently.
    summary_path = rec.session_dir / "session-summary.json"
    assert summary_path.is_file(), "summarize did not persist session-summary.json"
    stored = json.loads(summary_path.read_text(encoding="utf-8"))
    assert stored["summary"] == summ["summary"]
