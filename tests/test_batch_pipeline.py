"""Direct tests for tapscribe.batch_pipeline — the end-of-meeting pipeline.

Same deepening as the sibling orchestrator suites: the chain (one
`kind="pipeline"` claim, strip → transcribe → summarize stage ordering,
mid-chain failure verdicts, the poll record lifecycle) is testable WITHOUT
HTTP or a real model. Tests fake the three STAGES (`run_*_stage`, the
pipeline's per-stage seam) in `tapscribe.batch_pipeline`'s namespace — the
same patch-the-consumer convention the transcribe suite uses for
`load_transcriber`."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from wav_builders import seed_silent_wav  # type: ignore[import-not-found]

from tapscribe.batch_pipeline import PipelineRequest, start_pipeline
from tapscribe.recorder import JobState, SessionBusy

pytestmark = pytest.mark.asyncio


@pytest.fixture
def fake_stages(monkeypatch: pytest.MonkeyPatch):
    """Swap the three stage functions for fakes that log their order.
    Returns the call log."""
    calls: list[str] = []

    async def _strip(req, *, job):  # noqa: ARG001
        calls.append("strip")

    async def _transcribe(req, *, job, model, backend):  # noqa: ARG001
        calls.append("transcribe")

    async def _summarize(req, *, job):  # noqa: ARG001
        calls.append("summarize")

    monkeypatch.setattr("tapscribe.batch_pipeline.run_strip_stage", _strip)
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

    assert fake_stages == ["strip", "transcribe", "summarize"]
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

    async def _transcribe(req, *, job, model, backend):  # noqa: ARG001
        raise NoUsableWavs("no usable WAVs after stripping — no speech detected in this session")

    async def _summarize(req, *, job):  # noqa: ARG001
        calls.append("summarize")

    monkeypatch.setattr("tapscribe.batch_pipeline.run_strip_stage", _strip)
    monkeypatch.setattr("tapscribe.batch_pipeline.run_transcribe_stage", _transcribe)
    monkeypatch.setattr("tapscribe.batch_pipeline.run_summarize_stage", _summarize)

    task = await start_pipeline(recorder_under_test, PipelineRequest(session="s"))
    await task

    assert calls == ["strip"]  # summarize never ran
    record = recorder_under_test.pipelines.get("s")
    assert record is not None
    assert record.state == "failed"
    assert record.stage == "transcribe"
    assert "no usable WAVs" in (record.error or "")
    assert record.error_kind == "NoUsableWavs"
    assert recorder_under_test.jobs.get("s") is None  # released despite failure


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
    ],
)
async def test_pipeline_resolves_model_from_batch_model_config_else_default(
    recorder_under_test, monkeypatch, configured, expected
):
    """The transcribe stage's model comes from the operator's batch-model
    config — validated against the catalog — never from the request."""
    monkeypatch.setattr("tapscribe.batch_pipeline.read_config", lambda key: configured)
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
        "tapscribe.batch_transcribe.load_transcriber",
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
        "tapscribe.batch_transcribe.load_transcriber",
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
