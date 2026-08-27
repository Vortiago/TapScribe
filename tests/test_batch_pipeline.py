"""Direct tests for tapscribe.batch_pipeline — the end-of-meeting pipeline.

Same deepening as the sibling orchestrator suites: the chain (one
`kind="pipeline"` claim, strip → diarize → transcribe → summarize stage ordering,
mid-chain failure verdicts, the poll record lifecycle) is testable WITHOUT
HTTP or a real model. Tests fake the three STAGES (`run_*_stage`, the
pipeline's per-stage seam) in `tapscribe.batch_pipeline`'s namespace — the
same patch-the-consumer convention the transcribe suite uses for
`load_transcriber`."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from wav_builders import seed_silent_wav  # type: ignore[import-not-found]

from tapscribe.batch_pipeline import PipelineRequest, start_pipeline
from tapscribe.recorder import JobState, SessionBusy

pytestmark = pytest.mark.asyncio


@pytest.fixture
def fake_stages(monkeypatch: pytest.MonkeyPatch):
    """Swap the four stage functions for fakes that log their order.
    Returns the call log."""
    calls: list[str] = []

    async def _strip(req, *, job):  # noqa: ARG001
        calls.append("strip")

    async def _diarize(req, *, job):  # noqa: ARG001
        calls.append("diarize")

    async def _transcribe(req, *, job, model, backend):  # noqa: ARG001
        calls.append("transcribe")

    async def _summarize(req, *, job):  # noqa: ARG001
        calls.append("summarize")

    monkeypatch.setattr("tapscribe.batch_pipeline.run_strip_stage", _strip)
    monkeypatch.setattr("tapscribe.batch_pipeline.run_diarize_stage", _diarize)
    monkeypatch.setattr("tapscribe.batch_pipeline.run_transcribe_stage", _transcribe)
    monkeypatch.setattr("tapscribe.batch_pipeline.run_summarize_stage", _summarize)
    return calls


async def test_pipeline_runs_stages_in_order_under_one_claim(recorder_under_test, fake_stages, monkeypatch):
    """The tracer bullet: one trigger claims ONE `kind="pipeline"` slot, runs
    the three stages in order, records done, and releases the slot."""
    claims: list[JobState] = []
    real_claim = recorder_under_test.jobs.claim

    async def _spy_claim(state: JobState):
        claims.append(state)
        return await real_claim(state)

    monkeypatch.setattr(recorder_under_test.jobs, "claim", _spy_claim)

    task = await start_pipeline(recorder_under_test, PipelineRequest(session="s"))
    await task

    assert fake_stages == ["strip", "diarize", "transcribe", "summarize"]
    assert len(claims) == 1 and claims[0].kind == "pipeline"
    assert recorder_under_test.jobs.get("s") is None  # slot released at the end
    record = recorder_under_test.pipelines.get("s")
    assert record is not None and record.state == "done"


async def test_pipeline_updates_stage_between_stages(recorder_under_test, monkeypatch):
    """The dashboard's job bar and the tap poll endpoint both read the live
    JobState — each stage must see the slot already relabelled to itself."""
    stages_seen: list[tuple[str, str | None, str]] = []

    def _snap(name: str) -> None:
        held = recorder_under_test.jobs.get("s")
        assert held is not None
        stages_seen.append((name, held.stage, held.status))

    async def _strip(req, *, job):  # noqa: ARG001
        _snap("strip")

    async def _transcribe(req, *, job, model, backend):  # noqa: ARG001
        _snap("transcribe")

    async def _summarize(req, *, job):  # noqa: ARG001
        _snap("summarize")

    monkeypatch.setattr("tapscribe.batch_pipeline.run_strip_stage", _strip)
    monkeypatch.setattr("tapscribe.batch_pipeline.run_transcribe_stage", _transcribe)
    monkeypatch.setattr("tapscribe.batch_pipeline.run_summarize_stage", _summarize)

    task = await start_pipeline(recorder_under_test, PipelineRequest(session="s"))
    await task

    assert stages_seen == [
        ("strip", "strip", "stripping"),
        ("transcribe", "transcribe", "transcribing"),
        ("summarize", "summarize", "summarizing"),
    ]


async def test_pipeline_busy_raises_session_busy_and_starts_no_task(recorder_under_test, fake_stages):
    """A concurrent trigger (or manual transcribe) must get a deterministic
    busy verdict in the REQUEST path — no stage runs, the foreign claim and
    any previous pipeline record are left alone."""
    claimed = await recorder_under_test.jobs.claim(
        JobState(
            session="s", kind="transcribe", current=0, total=1, started_at=datetime.now(UTC), status="running"
        )
    )
    assert claimed

    with pytest.raises(SessionBusy):
        await start_pipeline(recorder_under_test, PipelineRequest(session="s"))

    assert fake_stages == []  # no stage ever started
    held = recorder_under_test.jobs.get("s")
    assert held is not None and held.kind == "transcribe"  # foreign claim intact
    assert recorder_under_test.pipelines.get("s") is None  # no record seeded


async def test_pipeline_mid_chain_failure_records_stage_and_error_and_releases(
    recorder_under_test, monkeypatch
):
    """A stage failure aborts the chain — later stages never run, the record
    names the failing stage and its domain error, and the slot is released
    so the session isn't wedged."""
    from tapscribe.session_merge import NoUsableWavs

    calls: list[str] = []

    async def _strip(req, *, job):  # noqa: ARG001
        calls.append("strip")

    async def _diarize(req, *, job):  # noqa: ARG001
        calls.append("diarize")

    async def _transcribe(req, *, job, model, backend):  # noqa: ARG001
        raise NoUsableWavs("no usable WAVs after stripping — no speech detected in this session")

    async def _summarize(req, *, job):  # noqa: ARG001
        calls.append("summarize")

    monkeypatch.setattr("tapscribe.batch_pipeline.run_strip_stage", _strip)
    monkeypatch.setattr("tapscribe.batch_pipeline.run_diarize_stage", _diarize)
    monkeypatch.setattr("tapscribe.batch_pipeline.run_transcribe_stage", _transcribe)
    monkeypatch.setattr("tapscribe.batch_pipeline.run_summarize_stage", _summarize)

    task = await start_pipeline(recorder_under_test, PipelineRequest(session="s"))
    await task

    assert calls == ["strip", "diarize"]  # summarize never ran
    record = recorder_under_test.pipelines.get("s")
    assert record is not None
    assert record.state == "failed"
    assert record.stage == "transcribe"
    assert "no usable WAVs" in (record.error or "")
    assert record.error_kind == "NoUsableWavs"
    assert recorder_under_test.jobs.get("s") is None  # released despite failure


async def test_pipeline_cancellation_records_a_failure_instead_of_hanging_running(
    recorder_under_test, monkeypatch
):
    """`asyncio.CancelledError` is a BaseException, so a plain `except
    Exception` misses it: the `finally` releases the slot but the
    PipelineRecord stays `state="running"` forever, and a Bridge's meeting
    card polling `GET /api/tap/sessions/{s}/pipeline` spins indefinitely
    instead of surfacing a failure. Cancellation must be recorded AND
    re-raised (never swallowed)."""
    started = asyncio.Event()

    async def _strip(req, *, job):  # noqa: ARG001
        started.set()
        await asyncio.Event().wait()  # suspend so the task is cancellable mid-stage

    async def _transcribe(req, *, job, model, backend):  # noqa: ARG001
        raise AssertionError("transcribe must not run after cancellation")

    async def _summarize(req, *, job):  # noqa: ARG001
        raise AssertionError("summarize must not run after cancellation")

    monkeypatch.setattr("tapscribe.batch_pipeline.run_strip_stage", _strip)
    monkeypatch.setattr("tapscribe.batch_pipeline.run_transcribe_stage", _transcribe)
    monkeypatch.setattr("tapscribe.batch_pipeline.run_summarize_stage", _summarize)

    task = await start_pipeline(recorder_under_test, PipelineRequest(session="s"))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    record = recorder_under_test.pipelines.get("s")
    assert record is not None
    assert record.state == "failed"
    assert record.stage == "strip"
    assert record.error_kind == "CancelledError"
    assert recorder_under_test.jobs.get("s") is None  # slot released


async def test_pipeline_record_overwritten_on_next_trigger(recorder_under_test, fake_stages):
    """One current record per session: a re-trigger replaces a previous
    outcome rather than accumulating history."""
    recorder_under_test.pipelines.begin("s")
    recorder_under_test.pipelines.finish_failed(
        "s", stage="transcribe", error="old failure", error_kind="NoUsableWavs"
    )

    task = await start_pipeline(recorder_under_test, PipelineRequest(session="s"))
    await task

    record = recorder_under_test.pipelines.get("s")
    assert record is not None
    assert record.state == "done"
    assert record.error is None  # the failed record was replaced, not patched


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        ("tiny.en", "tiny.en"),  # operator default, catalog-listed → used
        ("", "large-v3-turbo"),  # unset → bundled default (multilingual, ADR-0010)
        ("evil/repo", "large-v3-turbo"),  # not in the catalog → never reaches a loader
        # In the catalog but LIVE-ONLY (no batch adapter) — resolving it would
        # raise a raw NotImplementedError at the transcribe stage.
        ("moonshine-tiny", "large-v3-turbo"),
    ],
)
async def test_pipeline_resolves_model_from_batch_model_config_else_default(
    recorder_under_test, monkeypatch, configured, expected
):
    """The transcribe stage's model comes from the operator's batch-model
    config — validated against the catalog — never from the request. Model
    resolution now lives in the shared `batch_transcribe.resolve_batch_model`
    (ADR-0011), so patch `read_config` there."""
    monkeypatch.setattr("tapscribe.batch_transcribe.read_config", lambda key: configured)
    seen: dict = {}

    async def _strip(req, *, job):  # noqa: ARG001
        pass

    async def _transcribe(req, *, job, model, backend):  # noqa: ARG001
        seen["model"] = model
        seen["backend"] = backend

    async def _summarize(req, *, job):  # noqa: ARG001
        pass

    monkeypatch.setattr("tapscribe.batch_pipeline.run_strip_stage", _strip)
    monkeypatch.setattr("tapscribe.batch_pipeline.run_transcribe_stage", _transcribe)
    monkeypatch.setattr("tapscribe.batch_pipeline.run_summarize_stage", _summarize)

    task = await start_pipeline(recorder_under_test, PipelineRequest(session="s"))
    await task

    assert seen["model"] == expected
    assert seen["backend"] == recorder_under_test.backend  # operator launch preference


async def test_pipeline_end_to_end_produces_stripped_transcript_and_summary(recorder_under_test, monkeypatch):
    """Issue #102's first acceptance criterion, with REAL stages and fakes
    only at the model seams (transcriber stub, summarizer fake): one trigger
    yields a stripped/ directory, a merged session transcript, and a
    persisted session summary — each stage consuming the previous one's
    on-disk output."""
    from conftest import TranscriberStub  # type: ignore[import-not-found]
    from wav_builders import seed_session  # type: ignore[import-not-found]

    from tapscribe.sessions import read_session_summary

    monkeypatch.setattr(
        "tapscribe.transcribers.load_transcriber",
        lambda *a, **kw: TranscriberStub(backend="fake-be", model="fake-m", text="meeting words"),  # noqa: ARG005
    )

    class _FakeResult:
        @staticmethod
        def to_mapping():
            return {"summary": "decided to ship", "source": "local", "model": "fake-sum", "took_ms": 1}

    class _FakeSummarizer:
        @staticmethod
        def summarize(text, *, prompt, names=()):  # noqa: ARG004
            assert "meeting words" in text  # the transcribe stage's output reached us
            return _FakeResult()

    monkeypatch.setattr(
        "tapscribe.batch_pipeline.load_summarizer",
        lambda **kw: _FakeSummarizer(),  # noqa: ARG005
    )

    sd = seed_session(recorder_under_test.recordings_dir, "s", ["2026-01-01T01-00-00Z__alice__abc.wav"])

    task = await start_pipeline(recorder_under_test, PipelineRequest(session="s"))
    await task

    record = recorder_under_test.pipelines.get("s")
    assert record is not None and record.state == "done", (record.stage, record.error)
    assert any((sd / "stripped").glob("*.wav"))  # strip stage output
    assert (sd / "session-transcript.json").is_file()  # transcribe stage output
    stored = read_session_summary("s")
    assert stored is not None and stored["summary"] == "decided to ship"  # summarize persisted
    assert recorder_under_test.jobs.get("s") is None


async def test_pipeline_transcribe_stage_honours_candidate_languages(recorder_under_test, monkeypatch):
    """ADR-0010: the end-of-meeting pipeline's transcribe stage resolves the
    operator's candidate-language default and applies it to the generalist —
    here a singleton default pins it, driving the model with source_lang=that
    code on every stripped region. Proves the pipeline path (not just
    transcribe_session) honours the set."""
    from conftest import TranscriberStub  # type: ignore[import-not-found]
    from wav_builders import seed_session  # type: ignore[import-not-found]

    from tapscribe.text import write_languages

    write_languages("da")  # the meeting is declared Danish-only → a pin
    stub = TranscriberStub(backend="fake-be", model="fake-m", text="hej med dig")
    monkeypatch.setattr(
        "tapscribe.transcribers.load_transcriber",
        lambda *a, **kw: stub,  # noqa: ARG005
    )
    monkeypatch.setattr(
        "tapscribe.batch_pipeline.load_summarizer",
        lambda **kw: _NoopSummarizer(),  # noqa: ARG005
    )

    seed_session(recorder_under_test.recordings_dir, "s", ["2026-01-01T01-00-00Z__alice__abc.wav"])
    task = await start_pipeline(recorder_under_test, PipelineRequest(session="s"))
    await task

    record = recorder_under_test.pipelines.get("s")
    assert record is not None and record.state == "done", (record.stage, record.error)
    # Every region the transcribe stage processed was pinned to Danish.
    assert stub.seen_source_lang, "transcribe stage never ran"
    assert set(stub.seen_source_lang) == {"da"}, stub.seen_source_lang


class _NoopSummarizer:
    @staticmethod
    def summarize(text, *, prompt, names=()):  # noqa: ARG004
        class _R:
            @staticmethod
            def to_mapping():
                return {"summary": "ok", "source": "local", "model": "fake-sum", "took_ms": 1}

        return _R()


async def test_pipeline_zero_speech_session_fails_at_transcribe_stage(recorder_under_test):
    """REAL strip + transcribe stages: a session whose WAVs contain no speech
    strips to nothing (no stripped/ dir at all), so the chain fails at the
    transcribe stage with NoUsableWavs — the correct verdict for a meeting
    with nothing usable in it, reported instead of half-swallowed."""
    sd = recorder_under_test.recordings_dir / "s"
    sd.mkdir(parents=True)
    seed_silent_wav(sd / "2026-01-01T01-00-00Z__alice__abc.wav")

    task = await start_pipeline(recorder_under_test, PipelineRequest(session="s"))
    await task

    record = recorder_under_test.pipelines.get("s")
    assert record is not None
    assert record.state == "failed"
    assert record.stage == "transcribe"
    assert record.error_kind == "NoUsableWavs"
    assert recorder_under_test.jobs.get("s") is None


async def test_run_transcribe_stage_selection_runs_off_the_event_loop(recorder_under_test, monkeypatch):
    """Issue #214: the transcribe stage's `select_session_wavs(...,
    source="stripped")` call is the same disk-heavy per-WAV RMS scan as the
    manual transcribe path (test_batch_transcribe.py's sibling test) — it
    must be offloaded via `asyncio.to_thread` so the pipeline's transcribe
    stage doesn't stall the loop before the model is even loaded."""
    import threading

    from conftest import TranscriberStub  # type: ignore[import-not-found]
    from wav_builders import seed_session, seed_wav  # type: ignore[import-not-found]

    from tapscribe.batch_pipeline import run_transcribe_stage
    from tapscribe.session_merge import select_session_wavs as real_select

    stub = TranscriberStub(backend="fake-be", model="fake-m", text="hi")
    monkeypatch.setattr(
        "tapscribe.transcribers.load_transcriber",
        lambda *a, **kw: stub,  # noqa: ARG005
    )

    wav_name = "2026-01-01T01-00-00Z__alice__abc.wav"
    sd = seed_session(recorder_under_test.recordings_dir, "s", [wav_name])
    stripped_dir = sd / "stripped"
    stripped_dir.mkdir()
    seed_wav(stripped_dir / wav_name)

    seen_is_main: list[bool] = []

    def _spy(*a, **kw):
        seen_is_main.append(threading.current_thread() is threading.main_thread())
        return real_select(*a, **kw)

    monkeypatch.setattr("tapscribe.batch_pipeline.select_session_wavs", _spy)

    await run_transcribe_stage(
        PipelineRequest(session="s"),
        job=recorder_under_test.jobs.handle("s"),
        model="fake-m",
        backend="cpu",
    )

    assert seen_is_main == [False], seen_is_main


# ---------------------------------------------------------------------------
# run_summarize_stage × effective_summarizer_config (#84) — the summarize
# stage resolves session override → global default → built-ins; the tap
# trigger still carries no summarizer fields (operator defaults only).
# ---------------------------------------------------------------------------


async def _run_summarize_stage_capturing(recorder, monkeypatch, session="s"):
    """Run the summarize stage directly over a seeded merged transcript with
    `load_summarizer` (in batch_pipeline's namespace, per convention) swapped
    for a capturing fake. Returns the captured factory kwargs + prompt."""
    from conftest import seed_merged_transcript  # type: ignore[import-not-found]

    from tapscribe.batch_pipeline import run_summarize_stage

    seen: dict = {}

    class _FakeResult:
        @staticmethod
        def to_mapping():
            return {"summary": "ok", "source": "x", "model": "", "command": "", "took_ms": 1}

    class _FakeSummarizer:
        @staticmethod
        def summarize(text, *, prompt, names=()):  # noqa: ARG004
            seen["prompt"] = prompt
            seen["names"] = list(names)
            return _FakeResult()

    def _fake_load(**kw):
        seen.update(kw)
        return _FakeSummarizer()

    monkeypatch.setattr("tapscribe.batch_pipeline.load_summarizer", _fake_load)
    seed_merged_transcript(recorder.recordings_dir, session)
    await run_summarize_stage(PipelineRequest(session=session), job=None)
    return seen


async def test_pipeline_summarize_stage_resolves_global_default(recorder_under_test, monkeypatch):
    from tapscribe import text

    text.write_summarizer_config(
        {"source": "command", "command": "claude -p", "prompt": "GLOBAL", "max_tokens": 512}
    )
    seen = await _run_summarize_stage_capturing(recorder_under_test, monkeypatch)
    assert seen["source"] == "command"
    assert seen["command"] == "claude -p"
    assert seen["max_tokens"] == 512
    assert seen["prompt"] == "GLOBAL"


async def test_pipeline_summarize_stage_session_override_beats_global(recorder_under_test, monkeypatch):
    from tapscribe import text
    from tapscribe.sessions import write_session_meta

    text.write_summarizer_config({"source": "command", "command": "claude -p", "prompt": "GLOBAL"})
    write_session_meta("s", {"summary_source": "local", "summary_prompt": "SESSION"})
    seen = await _run_summarize_stage_capturing(recorder_under_test, monkeypatch)
    assert seen["source"] == "local"
    assert seen["prompt"] == "SESSION"


async def test_pipeline_summarize_stage_built_ins_when_nothing_configured(recorder_under_test, monkeypatch):
    """No saved config at all → today's pre-#84 behaviour: the bundled local
    source with the catalog/env default model."""
    from tapscribe.summarizers import DEFAULT_SUMMARY_PROMPT

    seen = await _run_summarize_stage_capturing(recorder_under_test, monkeypatch)
    assert seen["source"] == "local"
    assert seen["model"] == ""
    assert seen["max_tokens"] is None
    assert seen["prompt"] == DEFAULT_SUMMARY_PROMPT


async def test_a_diarize_failure_does_not_cost_the_operator_their_transcript(
    recorder_under_test, monkeypatch
):
    """The pipeline aborts on any stage exception, so the diarize stage has to
    absorb its own: an unfetched model or one unreadable WAV would otherwise
    take the transcript and the summary with it. What is lost is attribution on
    this session, and a re-diarize recovers that."""
    from tapscribe.diarizers.base import DiarizerUnavailable

    calls: list[str] = []

    async def _strip(req, *, job):  # noqa: ARG001
        calls.append("strip")

    async def _transcribe(req, *, job, model, backend):  # noqa: ARG001
        calls.append("transcribe")

    async def _summarize(req, *, job):  # noqa: ARG001
        calls.append("summarize")

    async def _locked(req, *, job=None):  # noqa: ARG001
        raise DiarizerUnavailable("the speaker-embedding model is not at …")

    monkeypatch.setattr("tapscribe.batch_pipeline.run_strip_stage", _strip)
    monkeypatch.setattr("tapscribe.batch_pipeline.run_transcribe_stage", _transcribe)
    monkeypatch.setattr("tapscribe.batch_pipeline.run_summarize_stage", _summarize)
    monkeypatch.setattr("tapscribe.batch_pipeline.diarize_session_locked", _locked)

    task = await start_pipeline(recorder_under_test, PipelineRequest(session="s"))
    await task

    assert calls == ["strip", "transcribe", "summarize"]
    record = recorder_under_test.pipelines.get("s")
    assert record is not None and record.state == "done"


async def test_the_job_says_diarize_while_the_diarize_stage_runs(recorder_under_test, monkeypatch):
    """The Bridge's meeting card and the dashboard both read `stage` off the job
    to say what is happening; a stage that never sets it shows the previous one."""
    seen: list[str | None] = []

    async def _strip(req, *, job):  # noqa: ARG001
        pass

    async def _diarize(req, *, job):  # noqa: ARG001
        seen.append(recorder_under_test.jobs.snapshot()["s"].stage)

    async def _transcribe(req, *, job, model, backend):  # noqa: ARG001
        pass

    async def _summarize(req, *, job):  # noqa: ARG001
        pass

    monkeypatch.setattr("tapscribe.batch_pipeline.run_strip_stage", _strip)
    monkeypatch.setattr("tapscribe.batch_pipeline.run_diarize_stage", _diarize)
    monkeypatch.setattr("tapscribe.batch_pipeline.run_transcribe_stage", _transcribe)
    monkeypatch.setattr("tapscribe.batch_pipeline.run_summarize_stage", _summarize)

    task = await start_pipeline(recorder_under_test, PipelineRequest(session="s"))
    await task

    assert seen == ["diarize"]


async def test_the_pipeline_diarizes_the_same_audio_it_transcribes(recorder_under_test, monkeypatch):
    """Both stages read the strip's output. Diarizing the originals instead
    would pay again for the silence the strip just removed, and would cover
    audio no segment exists for."""
    seen: list[str] = []

    async def _strip(req, *, job):  # noqa: ARG001
        pass

    async def _transcribe(req, *, job, model, backend):  # noqa: ARG001
        pass

    async def _summarize(req, *, job):  # noqa: ARG001
        pass

    async def _locked(req, *, job=None):  # noqa: ARG001
        seen.append(req.source)
        return {"ok": True}

    monkeypatch.setattr("tapscribe.batch_pipeline.run_strip_stage", _strip)
    monkeypatch.setattr("tapscribe.batch_pipeline.run_transcribe_stage", _transcribe)
    monkeypatch.setattr("tapscribe.batch_pipeline.run_summarize_stage", _summarize)
    monkeypatch.setattr("tapscribe.batch_pipeline.diarize_session_locked", _locked)

    task = await start_pipeline(recorder_under_test, PipelineRequest(session="s"))
    await task

    assert seen == ["stripped"]
