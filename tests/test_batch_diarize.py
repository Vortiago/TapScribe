"""Direct tests for `tapscribe.batch_diarize` — the stage, not the engine.

Same shape as `test_batch_strip`: the orchestration is exercised against a
tmpdir-rooted Recorder with no HTTP, and the `Diarizer` is a stub, because what
is under test is WHICH audio reaches the engine and WHAT lands on disk.

The gate is the Roster's per-identity `mode`, not the live tap setting: whether
a recording holds several humans is a property of the recording, and flipping
the setting next week must not change what a finished session means (ADR-0021).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from wav_builders import seed_session  # type: ignore[import-not-found]

import tapscribe.batch_diarize as batch_diarize
from tapscribe import voices
from tapscribe.batch_diarize import DiarizeSessionRequest, diarize_session
from tapscribe.diarizers.base import DiarizationResult, DiarizerUnavailable
from tapscribe.recorder import JobState, SessionBusy
from tapscribe.session_paths import FILENAME_ROSTER_JSON, FILENAME_TRANSCRIPT_JSON
from tapscribe.tap_mode import TAP_MODE_MULTI, TAP_MODE_SINGLE
from tapscribe.transcribers.base import TranscriptionResult, TranscriptionSegment
from tapscribe.wav_cache import cached_transcribe

T0 = datetime(2026, 1, 1, 1, 0, 0, tzinfo=UTC)


def _wav(slug: str, ident: str, at: datetime) -> str:
    return f"{at.strftime('%Y-%m-%dT%H-%M-%SZ')}_{slug}_{ident}_0000abcd.wav"


class StubDiarizer:
    """Two Voices per identity: the first second of every clip and the next."""

    engine = "stub"

    def __init__(self) -> None:
        self.seen: list[list[tuple[str, datetime]]] = []

    def diarize(self, clips):
        clips = list(clips)
        self.seen.append([(str(len(c.samples)), c.start) for c in clips])
        if not clips:
            return DiarizationResult(engine=self.engine)
        return DiarizationResult(
            voices={
                "A": [(c.start, c.start + timedelta(seconds=1)) for c in clips],
                "B": [(c.start + timedelta(seconds=1), c.start + timedelta(seconds=2)) for c in clips],
            },
            engine=self.engine,
        )


@pytest.fixture
def stub(monkeypatch: pytest.MonkeyPatch) -> StubDiarizer:
    engine = StubDiarizer()
    monkeypatch.setattr(batch_diarize, "load_diarizer", lambda: engine)
    return engine


def _seed(recorder, session: str, roster: dict, *, wavs: list[str]) -> Path:
    session_dir = seed_session(recorder.recordings_dir, session, wavs)
    (session_dir / FILENAME_ROSTER_JSON).write_text(json.dumps(roster), encoding="utf-8")
    return session_dir


class _OneSegment:
    """One 0–1 s segment, so a re-merge has something to attribute — and it sits
    squarely inside `StubDiarizer`'s Voice A rather than straddling A and B."""

    name = backend = device = model_name = "fake"

    def transcribe(self, path, *, initial_prompt=None, hotwords=None, source_lang=None):  # noqa: ARG002
        return TranscriptionResult(
            transcriber=self.name,
            backend=self.backend,
            device=self.device,
            model=self.model_name,
            language="en",
            language_probability=1.0,
            duration=1.0,
            text="hi",
            segments=(TranscriptionSegment(start=0.0, end=1.0, text="hi"),),
            initial_prompt_used="",
            hotwords_used="",
            quality_settings={},
        )


def _cache_transcript(wav: Path) -> None:
    """The per-WAV sidecar a re-merge reads. Without one the merge finds no
    segments, which is the state the empty-re-merge guard refuses to write."""
    cached_transcribe(wav, _OneSegment(), initial_prompt=None, hotwords=None, hallucination_rules=[])


def _no_engine():
    raise DiarizerUnavailable("the speaker-embedding model is not at …")


def _multi(slug: str, *, name: str = "", wavs: list[str] | None = None) -> dict:
    return {
        "name": name or slug,
        "source": "recorded",
        "slug": slug,
        "wavs": wavs or [],
        "mode": TAP_MODE_MULTI,
    }


async def test_a_session_with_no_multi_person_tap_writes_nothing(recorder_under_test, stub) -> None:
    """The common case. Diarizing a single-person tap manufactures Voices out of
    one human — a channel change or a cough becomes a second speaker."""
    wav = _wav("alice", "mic-alice", T0)
    session_dir = _seed(
        recorder_under_test,
        "s",
        {"mic-alice": {**_multi("alice"), "mode": TAP_MODE_SINGLE}},
        wavs=[wav],
    )

    out = await diarize_session(recorder_under_test, DiarizeSessionRequest(session="s"))

    assert out["identities"] == []
    assert stub.seen == [], "the engine ran on a single-person tap"
    assert voices.read_voices(session_dir) == {}


async def test_a_multi_person_tap_gets_its_voices_written(recorder_under_test, stub) -> None:
    wav = _wav("them", "sysaudio", T0)
    session_dir = _seed(recorder_under_test, "s", {"sysaudio": _multi("them")}, wavs=[wav])

    out = await diarize_session(recorder_under_test, DiarizeSessionRequest(session="s"))

    assert [row["identity"] for row in out["identities"]] == ["sysaudio"]
    stored = voices.read_voices(session_dir)
    assert sorted(stored["sysaudio"]["voices"]) == ["A", "B"]
    assert stored["sysaudio"]["run_id"] == out["run_id"]
    assert out["run_id"]


async def test_the_engine_sees_that_identity_s_wavs_in_time_order(recorder_under_test, stub) -> None:
    """Clustering runs over the identity's whole session at once, and Voice A is
    whoever spoke first — both need the clips chronological."""
    late = _wav("them", "sysaudio", T0 + timedelta(minutes=5))
    early = _wav("them", "sysaudio", T0)
    _seed(recorder_under_test, "s", {"sysaudio": _multi("them")}, wavs=[late, early])

    await diarize_session(recorder_under_test, DiarizeSessionRequest(session="s"))

    (clips,) = stub.seen
    assert [start for _, start in clips] == sorted(start for _, start in clips)
    assert len(clips) == 2


async def test_a_single_person_sibling_is_left_alone(recorder_under_test, stub) -> None:
    """A meeting is usually one mic (single) plus one system tap (multi)."""
    _seed(
        recorder_under_test,
        "s",
        {
            "mic-alice": {**_multi("alice"), "mode": TAP_MODE_SINGLE},
            "sysaudio": _multi("them"),
        },
        wavs=[_wav("alice", "mic-alice", T0), _wav("them", "sysaudio", T0)],
    )

    out = await diarize_session(recorder_under_test, DiarizeSessionRequest(session="s"))

    assert [row["identity"] for row in out["identities"]] == ["sysaudio"]
    (clips,) = stub.seen
    assert len(clips) == 1, "the mic's WAV reached the system tap's clustering"


async def test_re_running_supersedes_only_that_identity_s_run(recorder_under_test, stub) -> None:
    """A mapping is stamped with the run it was made against, so bumping a
    sibling's stamp would silently void every mapping on it."""
    session_dir = _seed(
        recorder_under_test, "s", {"sysaudio": _multi("them")}, wavs=[_wav("them", "sysaudio", T0)]
    )
    voices.record_voices(session_dir, identity="other", run_id="kept", spans={"A": [(T0, T0)]})

    first = await diarize_session(recorder_under_test, DiarizeSessionRequest(session="s"))
    second = await diarize_session(recorder_under_test, DiarizeSessionRequest(session="s"))

    assert first["run_id"] != second["run_id"]
    stored = voices.read_voices(session_dir)
    assert stored["sysaudio"]["run_id"] == second["run_id"]
    assert stored["other"]["run_id"] == "kept"


async def test_two_taps_sharing_a_display_name_are_skipped(recorder_under_test, stub) -> None:
    """Their WAVs are indistinguishable by filename, so neither identity's
    Voices could be joined back to it (#440) — running the engine would burn a
    session's worth of compute on spans nothing can apply."""
    _seed(
        recorder_under_test,
        "s",
        {"sysaudio-a": _multi("them"), "sysaudio-b": _multi("them")},
        wavs=[_wav("them", "sysaudio-a", T0)],
    )

    out = await diarize_session(recorder_under_test, DiarizeSessionRequest(session="s"))

    assert out["identities"] == []
    assert [row["identity"] for row in out["skipped"]] == ["sysaudio-a", "sysaudio-b"]
    assert stub.seen == []


async def test_an_identity_with_no_audio_is_skipped(recorder_under_test, stub) -> None:
    """A tap that connected and never spoke is in the Roster with no WAVs."""
    _seed(recorder_under_test, "s", {"sysaudio": _multi("them")}, wavs=[_wav("alice", "mic-alice", T0)])

    out = await diarize_session(recorder_under_test, DiarizeSessionRequest(session="s"))

    assert out["identities"] == []
    assert stub.seen == []


async def test_the_job_slot_is_released(recorder_under_test, stub) -> None:
    _seed(recorder_under_test, "s", {"sysaudio": _multi("them")}, wavs=[_wav("them", "sysaudio", T0)])

    await diarize_session(recorder_under_test, DiarizeSessionRequest(session="s"))
    out = await diarize_session(recorder_under_test, DiarizeSessionRequest(session="s"))

    assert out["ok"] is True


async def test_a_foreign_claim_is_left_alone(recorder_under_test, stub) -> None:
    _seed(recorder_under_test, "s", {"sysaudio": _multi("them")}, wavs=[_wav("them", "sysaudio", T0)])
    await recorder_under_test.jobs.claim(
        JobState(
            session="s", kind="transcribe", current=0, total=1, started_at=datetime.now(UTC), status="running"
        )
    )

    with pytest.raises(SessionBusy):
        await diarize_session(recorder_under_test, DiarizeSessionRequest(session="s"))

    assert recorder_under_test.jobs.snapshot()["s"].kind == "transcribe"


async def test_a_standalone_run_rekeys_an_existing_merged_transcript(recorder_under_test, stub) -> None:
    """Speaker keys are baked into `session-transcript.json` at merge time, so
    without this the operator diarizes, maps the Voices, and the transcript
    keeps saying `them:` until someone re-transcribes."""
    name = _wav("them", "sysaudio", T0)
    session_dir = _seed(recorder_under_test, "s", {"sysaudio": _multi("them")}, wavs=[name])
    _cache_transcript(session_dir / name)
    (session_dir / FILENAME_TRANSCRIPT_JSON).write_text(
        json.dumps({"segments": [{"speaker": "them", "text": "hi"}]}), encoding="utf-8"
    )

    await diarize_session(recorder_under_test, DiarizeSessionRequest(session="s"))

    after = json.loads((session_dir / FILENAME_TRANSCRIPT_JSON).read_text(encoding="utf-8"))
    assert [s["speaker"] for s in after["segments"]] == ["them#A"]


async def test_a_re_merge_that_finds_nothing_leaves_the_transcript_alone(recorder_under_test, stub) -> None:
    """Re-attribution never removes speech, so an empty re-merge means the audio
    the stored selection names is gone — the stripped dir reclaimed, or a
    re-strip that renamed every clip out from under the per-WAV caches. Writing
    it would destroy the meeting's transcript to record that its WAVs moved."""
    session_dir = _seed(
        recorder_under_test, "s", {"sysaudio": _multi("them")}, wavs=[_wav("them", "sysaudio", T0)]
    )
    # `source: stripped` with no `stripped/` on disk: `select_session_wavs`
    # returns an EMPTY selection rather than raising.
    before = json.dumps({"source": "stripped", "segments": [{"speaker": "them", "text": "hi"}]})
    (session_dir / FILENAME_TRANSCRIPT_JSON).write_text(before, encoding="utf-8")

    out = await diarize_session(recorder_under_test, DiarizeSessionRequest(session="s"))

    assert out["ok"] is True
    assert (session_dir / FILENAME_TRANSCRIPT_JSON).read_text(encoding="utf-8") == before


async def test_no_merged_transcript_is_not_an_error(recorder_under_test, stub) -> None:
    """Diarize before transcribe is the pipeline's order."""
    session_dir = _seed(
        recorder_under_test, "s", {"sysaudio": _multi("them")}, wavs=[_wav("them", "sysaudio", T0)]
    )

    out = await diarize_session(recorder_under_test, DiarizeSessionRequest(session="s"))

    assert out["ok"] is True
    assert not (session_dir / FILENAME_TRANSCRIPT_JSON).exists()


async def test_a_missing_engine_beats_a_busy_session(
    recorder_under_test, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`DiarizerUnavailable` is an install problem the operator fixes by running
    preflight; `SessionBusy` fixes itself. Loading before the claim — as
    `batch_summarize` does — is what makes the actionable one the one they see.
    """
    monkeypatch.setattr(batch_diarize, "load_diarizer", _no_engine)
    _seed(recorder_under_test, "s", {"sysaudio": _multi("them")}, wavs=[_wav("them", "sysaudio", T0)])
    await recorder_under_test.jobs.claim(
        JobState(
            session="s", kind="transcribe", current=0, total=1, started_at=datetime.now(UTC), status="running"
        )
    )

    with pytest.raises(DiarizerUnavailable):
        await diarize_session(recorder_under_test, DiarizeSessionRequest(session="s"))


async def test_a_session_with_nothing_to_do_needs_no_engine(
    recorder_under_test, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Most sessions are single-person. Loading a 30 MB graph to decide there is
    nothing to diarize would make every such call pay for the feature."""
    monkeypatch.setattr(batch_diarize, "load_diarizer", _no_engine)
    _seed(
        recorder_under_test,
        "s",
        {"mic-alice": {**_multi("alice"), "mode": TAP_MODE_SINGLE}},
        wavs=[_wav("alice", "mic-alice", T0)],
    )

    out = await diarize_session(recorder_under_test, DiarizeSessionRequest(session="s"))

    assert out["ok"] is True
    assert out["identities"] == []
