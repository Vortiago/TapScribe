"""Backend E2E for the session-management endpoints (no browser, no model).

These drive the real FastAPI app over httpx — the same wire the dashboard's
fetches use — and cover the destructive session-lifecycle routes that the
playwright suite exercises only through the UI (or not at all):

- ``DELETE /api/sessions/{s}`` — whole-session delete, with its current-session
  refusal AND its in-flight-job refusal. The job guard mirrors the sibling
  ``/audio`` and ``/absorb`` endpoints: deleting a session whose transcribe /
  strip job is still running would ``rmtree`` the folder out from under the job
  thread. (Regression guard for that fix.)
- ``DELETE /api/sessions/{s}/audio`` — reclaims audio while PRESERVING the
  merged transcript + meta, so the session survives a subsequent prune-empty.

Pure httpx + a couple of on-disk fixtures, so this runs in the lightweight
``pytest tests`` CI matrix (no playwright, no torch).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx

from tapscribe.recorder import JobState

from .conftest import RunningRecorder
from .harness import synth_speech_like_wav


def _seed_session(
    rec, sid: str, *, wavs: int = 1, transcript: bool = False, label: str | None = None
) -> Path:
    """Create an on-disk archived session: `wavs` originals, optionally a merged
    session-transcript.json and a session-meta.json carrying `label`."""
    d = rec.recordings_dir / sid
    d.mkdir(parents=True)
    for i in range(wavs):
        synth_speech_like_wav(d / f"{sid}_Spk{i}_spk{i}_0000a00{i}.wav", seconds=0.3, freq_hz=200.0 + i * 30)
    if transcript:
        (d / "session-transcript.json").write_text(
            json.dumps(
                {
                    "transcribed_at": "2025-02-01T10:00:00+00:00",
                    "segments": [
                        {"speaker": "Spk0", "text": "hello there", "abs_start": "2025-02-01T09:00:00+00:00"}
                    ],
                    "speakers": ["Spk0"],
                    "speaking_seconds": {"Spk0": 1.0},
                    "suppressed": [],
                    "suppressed_count": 0,
                    "wav_count": wavs,
                    "transcribe_ms": 10,
                    "model": "tiny.en",
                    "backend": "fake",
                    "device": "cpu",
                }
            ),
            encoding="utf-8",
        )
    if label is not None:
        (d / "session-meta.json").write_text(json.dumps({"label": label}), encoding="utf-8")
    return d


async def _session_ids(client: httpx.AsyncClient) -> set[str]:
    state = (await client.get("/api/state")).json()
    return {s["session"] for s in state.get("sessions", [])}


async def test_delete_current_session_is_refused(running_recorder: RunningRecorder):
    """The currently-recording session can't be deleted — rotate first."""
    rec = running_recorder.recorder
    async with httpx.AsyncClient(base_url=running_recorder.base_url) as client:
        r = await client.delete(f"/api/sessions/{rec.session_start}")
        assert r.status_code == 409, r.text
        assert "current" in r.text.lower()


async def test_delete_archived_session_removes_folder_and_unlists(running_recorder: RunningRecorder):
    """Deleting an archived session removes its folder and drops it from state."""
    rec = running_recorder.recorder
    sid = "2025-07-01T10-00-00Z"
    d = _seed_session(rec, sid, wavs=2, transcript=True, label="Old Sync")
    async with httpx.AsyncClient(base_url=running_recorder.base_url) as client:
        assert sid in await _session_ids(client)
        r = await client.delete(f"/api/sessions/{sid}")
        assert r.status_code == 200, r.text
        assert r.json().get("deleted") == sid
        assert not d.exists(), "delete must remove the whole folder"
        assert sid not in await _session_ids(client)


async def test_delete_and_delete_audio_refused_while_job_in_flight(running_recorder: RunningRecorder):
    """A transcribe/strip job in flight blocks BOTH whole-session delete and
    audio delete with a 409 — neither may pull the folder/WAVs out from under a
    running job thread. Releasing the job lets the delete proceed."""
    rec = running_recorder.recorder
    sid = "2025-07-02T10-00-00Z"
    d = _seed_session(rec, sid, wavs=1, transcript=True)

    claimed = await rec.jobs.claim(
        JobState(session=sid, kind="transcribe", current=0, total=1, started_at=datetime.now(UTC))
    )
    assert claimed

    async with httpx.AsyncClient(base_url=running_recorder.base_url) as client:
        r_audio = await client.delete(f"/api/sessions/{sid}/audio")
        assert r_audio.status_code == 409, r_audio.text
        r_del = await client.delete(f"/api/sessions/{sid}")
        assert r_del.status_code == 409, (
            "whole-session delete must refuse while a job is in flight, like /audio and /absorb"
        )
        assert d.exists(), "a refused delete must leave the folder intact"

        # Once the job finishes, the delete goes through.
        await rec.jobs.release(sid)
        r_ok = await client.delete(f"/api/sessions/{sid}")
        assert r_ok.status_code == 200, r_ok.text
        assert not d.exists()


async def test_delete_session_audio_preserves_transcript_and_survives_prune(
    running_recorder: RunningRecorder,
):
    """Audio delete reclaims the WAVs but KEEPS the merged transcript + meta, so
    the labelled session is still listed and survives a prune-empty sweep."""
    rec = running_recorder.recorder
    sid = "2025-07-03T10-00-00Z"
    d = _seed_session(rec, sid, wavs=2, transcript=True, label="Keep Me")
    async with httpx.AsyncClient(base_url=running_recorder.base_url) as client:
        r = await client.delete(f"/api/sessions/{sid}/audio")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body.get("wavs_deleted") == 2, body
        assert not sorted(d.glob("*.wav")), "audio delete must remove every original WAV"
        assert (d / "session-transcript.json").exists(), "merged transcript must be preserved"
        assert (d / "session-meta.json").exists(), "session meta (label) must be preserved"
        assert sid in await _session_ids(client), "the session must still be listed after audio delete"

        # Prune-empty must NOT remove it — it has a transcript + label.
        pr = await client.post("/api/sessions/prune-empty")
        assert pr.status_code == 200, pr.text
        assert sid in await _session_ids(client), "a transcript-bearing session must survive prune-empty"
        assert d.exists()
