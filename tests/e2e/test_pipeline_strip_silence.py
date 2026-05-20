"""End-to-end strip-silence pipeline test.

Streams one WAV containing three speech bursts separated by silence,
calls `/api/sessions/{session}/strip-silence`, then runs
`/api/transcribe-session` with `source="stripped"` and asserts:

  * The endpoint wrote one stripped WAV per detected speech region —
    not a single concatenated sibling.
  * Each region's filename encodes `origin + region_offset` so
    `parse_wav_start` places it at the right wall-clock time.
  * The merged transcript contains one segment per region, and the
    segments' `abs_start` values are spread across the original
    utterance's timeline (proving the merge no longer squashes them
    into the first few seconds).
"""

from __future__ import annotations

import wave
from pathlib import Path

import httpx
import numpy as np

from tapscribe.text import parse_wav_start

from .conftest import RunningRecorder
from .fake_transcriber import FakeTranscriber
from .harness import SAMPLE_RATE, stream_wav_via_tap, streams_drained, wait_until
from .test_pipeline_e2e import fake_transcriber  # noqa: F401 — fixture re-export

SPEAKER = "Alice"
SCRIPTED_TEXT = "stripped region segment"


def _build_speech_silence_wav(
    path: Path,
    *,
    burst_seconds: float = 0.5,
    silence_seconds: float = 0.8,
    burst_count: int = 3,
    amplitude: int = 12000,
) -> Path:
    """Write a WAV alternating `burst_count` speech bursts and silences.

    The bursts are square waves at `amplitude`, which clears the speech
    RMS floor by a wide margin. Silences are zero samples, well below
    SILENT_RMS_DBFS_FLOOR. The whole-file RMS still clears the global
    silence gate because the bursts dominate.
    """
    chunks: list[np.ndarray] = []
    for i in range(burst_count):
        n_burst = int(burst_seconds * SAMPLE_RATE)
        burst = np.tile(np.array([amplitude, -amplitude], dtype=np.int16), n_burst // 2 + 1)[:n_burst]
        chunks.append(burst)
        if i < burst_count - 1:
            n_silence = int(silence_seconds * SAMPLE_RATE)
            chunks.append(np.zeros(n_silence, dtype=np.int16))
    samples = np.concatenate(chunks)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(samples.tobytes())
    return path


async def test_strip_silence_splits_and_merge_preserves_wall_clock_times(
    running_recorder: RunningRecorder,
    fake_transcriber: FakeTranscriber,  # noqa: F811 — re-imported pytest fixture
    tmp_path: Path,
):
    rec = running_recorder.recorder
    base = running_recorder.base_url
    ws_base = running_recorder.ws_base_url

    # FakeTranscriber returns one segment whose text comes from
    # text_by_speaker keyed by parse_wav_speaker_slug(wav.name). Inject
    # the entry the regions' filenames will resolve to. We mutate the
    # fixture-owned dict in place so the route's later transcribe call
    # sees it.
    fake_transcriber.text_by_speaker[SPEAKER] = SCRIPTED_TEXT

    # One utterance with three internal speech bursts. After streaming,
    # the recorder writes a single WAV with the same speech+silence
    # pattern; strip-silence will split it into three region WAVs.
    src_wav = _build_speech_silence_wav(tmp_path / "alice-multi.wav")

    await stream_wav_via_tap(
        ws_base_url=ws_base,
        identity="alice",
        name=SPEAKER,
        wav_path=src_wav,
        utterance_id="utt-strip-multi",
    )
    assert await wait_until(lambda: streams_drained(rec), timeout=5.0)

    recorded = sorted(rec.session_dir.glob("*.wav"))
    assert len(recorded) == 1, f"expected exactly one recorded WAV, got {[w.name for w in recorded]}"
    original = recorded[0]
    original_start = parse_wav_start(original.name)
    assert original_start is not None, f"{original.name}: lost ISO prefix"

    async with httpx.AsyncClient(base_url=base, timeout=30.0) as client:
        resp = await client.post(
            f"/api/sessions/{rec.session_start}/strip-silence",
            json={
                "min_silence_ms": 400,
                "pad_ms": 50,
                "threshold_db": -30.0,
                "use_silero": False,  # CI doesn't carry torch
                "speech_floor_db": -40.0,
            },
        )
        assert resp.status_code == 200, resp.text
        strip_body = resp.json()
        assert strip_body["ok"] is True
        assert strip_body["files_processed"] == 1
        assert strip_body["files_written"] == 1

        # Per-region WAVs land in <session>/stripped/.
        stripped_dir_path = rec.session_dir / "stripped"
        region_wavs = sorted(stripped_dir_path.glob("*.wav"))
        assert len(region_wavs) == 3, (
            f"strip-silence should have written one WAV per speech region, "
            f"got {[w.name for w in region_wavs]}"
        )

        # Every region's parsed wall-clock time = original_start + offset.
        # The bursts start at 0s, ~1.3s, ~2.6s of the original timeline.
        parsed_starts = [parse_wav_start(w.name) for w in region_wavs]
        assert all(s is not None for s in parsed_starts), (
            f"some region WAV names lost their ISO prefix: {[w.name for w in region_wavs]}"
        )
        offsets_s = [(s - original_start).total_seconds() for s in parsed_starts]
        assert offsets_s == sorted(offsets_s), "region WAVs must be ordered by start time"
        # First region starts at or very near origin; last region is meaningfully later.
        assert offsets_s[0] <= 1, f"first region should hug the origin, got +{offsets_s[0]}s"
        assert offsets_s[-1] >= 2, (
            f"third region should be at least 2s after origin; got +{offsets_s[-1]}s ({offsets_s=})"
        )

        # Now transcribe the stripped source and verify the merge places
        # each region's segment at its true wall-clock time.
        resp = await client.post(
            "/api/transcribe-session",
            json={
                "session": rec.session_start,
                "model": "fake-small.en",
                "source": "stripped",
            },
        )
        assert resp.status_code == 200, resp.text
        merged = resp.json()

    assert merged["source"] == "stripped"
    assert merged["wav_count"] == 3, f"expected 3 stripped WAVs in the merge, got {merged['wav_count']}"
    segments = merged["segments"]
    assert len(segments) == 3, f"expected 3 segments (one per region), got {len(segments)}"
    assert all(s["text"] == SCRIPTED_TEXT for s in segments), (
        f"every region segment should carry the scripted text; got {[s['text'] for s in segments]}"
    )
    assert all(s["speaker"] == SPEAKER for s in segments)

    # The load-bearing assertion: absolute timestamps span the original
    # utterance's window. Pre-fix all three abs_starts collapsed to
    # within ~1s of the origin because the merge naively did
    # `wav_start + seg.start` on a concatenated stripped sibling.
    from datetime import datetime

    abs_starts = [datetime.fromisoformat(s["abs_start"]) for s in segments]
    span_s = (abs_starts[-1] - abs_starts[0]).total_seconds()
    assert span_s >= 2.0, (
        f"merged segments should span ≥2s of wall-clock time (one per speech burst across the "
        f"original utterance); got {span_s}s — likely the timestamps are squashed at the origin"
    )
