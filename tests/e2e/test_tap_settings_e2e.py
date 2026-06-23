"""Backend E2E for per-tap record/live preferences (PUT /api/tap-settings).

The Taps view lets the operator flip a per-identity ``rec`` / ``live`` toggle.
The unit tests cover the endpoint mutating the in-memory pref; what was untested
is the USER-FACING EFFECT: a ``record=false`` pref must suppress that identity's
NEXT utterance WAV (it takes effect on the next /tap open, per the endpoint
contract), and flipping it back must resume recording.

Pure websockets + httpx against the real app, so it runs in the lightweight
``pytest tests`` CI matrix.
"""

from __future__ import annotations

from pathlib import Path

import httpx

from .conftest import RunningRecorder
from .harness import stream_wav_via_tap, streams_drained, synth_speech_like_wav, wait_until


def _wav_count(rec) -> int:
    return len(sorted((rec.recordings_dir / rec.session_start).glob("*.wav")))


async def test_per_tap_record_pref_suppresses_then_resumes_next_utterance(
    running_recorder: RunningRecorder, tmp_path: Path
):
    """Streaming an identity records a WAV by default. After PUT
    /api/tap-settings {record: false} for that identity, its NEXT utterance
    produces NO new WAV. Flipping record back on resumes recording on the
    following utterance — the per-tap rec toggle's real effect."""
    rr = running_recorder
    rec = rr.recorder
    wav = synth_speech_like_wav(tmp_path / "alice.wav", seconds=1.0, freq_hz=200.0)

    async def utter(uid: str) -> None:
        await stream_wav_via_tap(
            ws_base_url=rr.ws_base_url, identity="alice", name="Alice", wav_path=wav, utterance_id=uid
        )
        assert await wait_until(lambda: streams_drained(rec), timeout=12.0)

    # 1) Default (record on): the utterance lands a WAV.
    await utter("utt-1")
    assert _wav_count(rec) == 1, "a default tap must record its utterance"

    async with httpx.AsyncClient(base_url=rr.base_url) as client:
        # 2) Turn record OFF for alice; the next utterance must NOT add a WAV.
        r = await client.put("/api/tap-settings", json={"identity": "alice", "record": False})
        assert r.status_code == 200 and r.json()["record"] is False, r.text
        await utter("utt-2")
        assert _wav_count(rec) == 1, "a record=off tap must NOT write a WAV for its next utterance"

        # 3) Turn record back ON; recording resumes on the following utterance.
        r = await client.put("/api/tap-settings", json={"identity": "alice", "record": True})
        assert r.status_code == 200 and r.json()["record"] is True, r.text
        await utter("utt-3")
        assert _wav_count(rec) == 2, "flipping record back on must resume recording"
