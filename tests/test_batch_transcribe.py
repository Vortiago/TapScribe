"""Direct tests for tapscribe.batch_transcribe.

The deepening behind this module's seam is that the orchestration
(load_transcriber + prompt/hotwords resolution + cached_transcribe loop
+ JobTracker + merge writes) is testable WITHOUT an HTTP TestClient or
a real Transcriber. Tests here call `transcribe_one` / `transcribe_session`
straight against a tmpdir-rooted Recorder and a TranscriberStub.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from conftest import TranscriberStub  # type: ignore[import-not-found]
from wav_builders import seed_session, seed_silent_wav  # type: ignore[import-not-found]

from tapscribe.batch_transcribe import (
    BatchOneRequest,
    BatchSessionRequest,
    BatchTranscribeError,
    WavTooQuiet,
    WavUnreadable,
    transcribe_one,
    transcribe_session,
    transcribe_session_locked,
)
from tapscribe.recorder import JobState, SessionBusy
from tapscribe.session_merge import InvalidRange, NoUsableWavs, select_session_wavs

# The `recorder_under_test` fixture (tmpdir-rooted Recorder, no auth, no
# live spawn) lives in conftest.py — shared with test_batch_strip.py.


@pytest.fixture
def install_stub_transcriber(monkeypatch: pytest.MonkeyPatch):
    """Returns a function that swaps `load_transcriber` for one that
    returns the supplied stub. Patches the binding in `transcribers`
    (where `lease_transcriber` fetches it), the consumer target."""

    def _install(stub):
        monkeypatch.setattr(
            "tapscribe.transcribers.load_transcriber",
            lambda *a, **kw: stub,  # noqa: ARG005
        )

    return _install


# ---------------------------------------------------------------------------
# transcribe_one — happy path + pre-checks + override chain
# ---------------------------------------------------------------------------


WAV_NAME = "2026-01-01T01-00-00Z__alice__abc.wav"


async def test_transcribe_one_writes_sidecar_and_returns_payload(
    recorder_under_test, install_stub_transcriber
):
    """Happy path: the orchestrator runs the cache layer, the sidecar
    lands on disk, and the returned dict is the freshly-written
    payload (not an in-memory shape that could drift from the file)."""
    install_stub_transcriber(TranscriberStub(backend="fake-be", model="fake-m", text="hello world"))
    seed_session(recorder_under_test.recordings_dir, "s", [WAV_NAME])

    request = BatchOneRequest(
        session="s",
        name=WAV_NAME,
        source="original",
        model="fake-m",
        backend="cpu",
        source_lang=None,
    )
    payload = await transcribe_one(recorder_under_test, request)

    assert payload["text"] == "hello world"
    assert payload["transcriber"] == "fake"
    # Sidecar exists in the new-layout directory next to the WAV.
    transcripts_dir = (
        recorder_under_test.recordings_dir / "s" / "2026-01-01T01-00-00Z__alice__abc.transcripts"
    )
    assert transcripts_dir.is_dir()
    assert any(transcripts_dir.glob("*.json"))


async def test_transcribe_one_raises_wav_unreadable_on_empty_file(
    recorder_under_test, install_stub_transcriber
):
    """Files under 64 bytes (or with unreadable headers) raise before
    the model is loaded — the operator gets fast feedback, the stub
    never sees a `transcribe()` call."""
    stub = TranscriberStub(backend="fake-be", model="fake-m")
    install_stub_transcriber(stub)
    sd = recorder_under_test.recordings_dir / "s"
    sd.mkdir(parents=True)
    (sd / WAV_NAME).write_bytes(b"")

    request = BatchOneRequest(
        session="s",
        name=WAV_NAME,
        source="original",
        model="fake-m",
        backend="cpu",
        source_lang=None,
    )
    with pytest.raises(WavUnreadable):
        await transcribe_one(recorder_under_test, request)
    assert stub.calls == []


async def test_transcribe_one_raises_wav_too_quiet_on_silent_audio(
    recorder_under_test, install_stub_transcriber
):
    """All-zeros PCM trips the silence floor. The Transcriber must NOT
    be invoked — Whisper would hallucinate."""
    stub = TranscriberStub(backend="fake-be", model="fake-m")
    install_stub_transcriber(stub)
    sd = recorder_under_test.recordings_dir / "s"
    sd.mkdir(parents=True)
    seed_silent_wav(sd / WAV_NAME)

    request = BatchOneRequest(
        session="s",
        name=WAV_NAME,
        source="original",
        model="fake-m",
        backend="cpu",
        source_lang=None,
    )
    with pytest.raises(WavTooQuiet):
        await transcribe_one(recorder_under_test, request)
    assert stub.calls == []


async def test_transcribe_one_uses_session_meta_prompt_when_set(
    recorder_under_test, install_stub_transcriber
):
    """Override chain: session-meta beats the global config file. This
    is the same invariant exercised by the route-level test, but here
    we drive the module directly and assert on the stub's captured
    kwargs — no HTTP needed."""
    captured: dict[str, str | None] = {}

    class _Spy(TranscriberStub):
        def transcribe(self, path, *, initial_prompt=None, hotwords=None, source_lang=None):  # noqa: ARG002
            captured["initial_prompt"] = initial_prompt
            captured["hotwords"] = hotwords
            return super().transcribe(path, initial_prompt=initial_prompt, hotwords=hotwords)

    install_stub_transcriber(_Spy(backend="fake-be", model="fake-m"))
    seed_session(recorder_under_test.recordings_dir, "s", [WAV_NAME])
    (recorder_under_test.config_dir / "prompt.txt").write_text("GLOBAL", encoding="utf-8")
    (recorder_under_test.config_dir / "hotwords.txt").write_text("Acme", encoding="utf-8")
    # session-meta sits in recordings/<session>/session-meta.json.
    (recorder_under_test.recordings_dir / "s" / "session-meta.json").write_text(
        json.dumps({"prompt": "SESSION OVERRIDE", "hotwords": "Patricia"}),
        encoding="utf-8",
    )

    request = BatchOneRequest(
        session="s",
        name=WAV_NAME,
        source="original",
        model="fake-m",
        backend="cpu",
        source_lang=None,
    )
    await transcribe_one(recorder_under_test, request)
    assert captured["initial_prompt"] == "SESSION OVERRIDE"
    assert captured["hotwords"] == "Patricia"


async def test_transcribe_one_falls_back_to_global_when_meta_empty(
    recorder_under_test, install_stub_transcriber
):
    captured: dict[str, str | None] = {}

    class _Spy(TranscriberStub):
        def transcribe(self, path, *, initial_prompt=None, hotwords=None, source_lang=None):  # noqa: ARG002
            captured["initial_prompt"] = initial_prompt
            captured["hotwords"] = hotwords
            return super().transcribe(path, initial_prompt=initial_prompt, hotwords=hotwords)

    install_stub_transcriber(_Spy(backend="fake-be", model="fake-m"))
    seed_session(recorder_under_test.recordings_dir, "s", [WAV_NAME])
    (recorder_under_test.config_dir / "prompt.txt").write_text("GLOBAL DEFAULT", encoding="utf-8")
    (recorder_under_test.config_dir / "hotwords.txt").write_text("Acme", encoding="utf-8")

    request = BatchOneRequest(
        session="s",
        name=WAV_NAME,
        source="original",
        model="fake-m",
        backend="cpu",
        source_lang=None,
    )
    await transcribe_one(recorder_under_test, request)
    assert captured["initial_prompt"] == "GLOBAL DEFAULT"
    assert captured["hotwords"] == "Acme"


async def test_transcribe_one_precheck_runs_off_the_event_loop(
    recorder_under_test, install_stub_transcriber, monkeypatch
):
    """Issue #214: the original WAV's duration + whole-file RMS pre-check
    reads the file from disk and runs a numpy pass over it — pure disk/CPU
    work that must run via `asyncio.to_thread`, not inline on the event
    loop, or a slow disk stalls every concurrent await (the /api/state poll
    included) for as long as the read takes. Thread-identity assertion
    (not a timing race) — same style as
    test_transcribe_session_runs_model_on_one_dedicated_thread below."""
    import threading

    from tapscribe.audio import wav_rms_dbfs as real_rms

    install_stub_transcriber(TranscriberStub(backend="fake-be", model="fake-m", text="hi"))
    seed_session(recorder_under_test.recordings_dir, "s", [WAV_NAME])

    seen_is_main: list[bool] = []

    def _spy_rms(path):
        seen_is_main.append(threading.current_thread() is threading.main_thread())
        return real_rms(path)

    monkeypatch.setattr("tapscribe.batch_transcribe.wav_rms_dbfs", _spy_rms)

    request = BatchOneRequest(
        session="s",
        name=WAV_NAME,
        source="original",
        model="fake-m",
        backend="cpu",
        source_lang=None,
    )
    await transcribe_one(recorder_under_test, request)

    assert seen_is_main == [False], seen_is_main


async def test_transcribe_one_raises_session_busy_when_slot_taken(
    recorder_under_test, install_stub_transcriber
):
    """The manual per-WAV re-transcribe is a heavy job like any other: while a
    session/pipeline job holds the slot it must get `SessionBusy`, not run
    concurrently. Two covers resident at once doubles peak memory, and both
    runs repoint the same WAV's `_primary` — a race over which transcript
    wins. The verdict must land BEFORE any work (the WAV need not even exist)."""
    install_stub_transcriber(TranscriberStub(backend="fake-be", model="fake-m"))
    seed_session(recorder_under_test.recordings_dir, "s", [WAV_NAME])
    await recorder_under_test.jobs.claim(
        JobState(
            session="s",
            kind="pipeline",
            current=0,
            total=1,
            started_at=datetime.now(UTC),
            status="transcribing",
        )
    )

    request = BatchOneRequest(
        session="s",
        name=WAV_NAME,
        source="original",
        model="fake-m",
        backend="cpu",
        source_lang=None,
    )
    with pytest.raises(SessionBusy):
        await transcribe_one(recorder_under_test, request)

    # The foreign claim is intact — a refused run never releases someone
    # else's slot.
    held = recorder_under_test.jobs.get("s")
    assert held is not None and held.kind == "pipeline"


async def test_transcribe_one_claims_and_releases_the_session_slot(
    recorder_under_test, install_stub_transcriber
):
    """The dual: a successful manual transcribe HOLDS the slot for its
    duration (so a concurrent trigger 409s) and releases it on the way out."""
    seen: list[str | None] = []

    class _SlotWatchingStub(TranscriberStub):
        def transcribe(self, path, **kw):
            held = recorder_under_test.jobs.get("s")
            seen.append(held.kind if held is not None else None)
            return super().transcribe(path, **kw)

    install_stub_transcriber(_SlotWatchingStub(backend="fake-be", model="fake-m", text="hi"))
    seed_session(recorder_under_test.recordings_dir, "s", [WAV_NAME])

    await transcribe_one(
        recorder_under_test,
        BatchOneRequest(
            session="s",
            name=WAV_NAME,
            source="original",
            model="fake-m",
            backend="cpu",
            source_lang=None,
        ),
    )

    assert seen and all(kind == "transcribe" for kind in seen), seen
    assert recorder_under_test.jobs.get("s") is None


# ---------------------------------------------------------------------------
# transcribe_session — happy path + JobTracker + range errors
# ---------------------------------------------------------------------------


SESSION_WAVS = [
    "2026-01-01T01-00-00Z__alice__a.wav",
    "2026-01-01T01-00-05Z__alice__b.wav",
]


async def test_transcribe_session_writes_outputs_and_returns_merged(
    recorder_under_test, install_stub_transcriber
):
    """Drives the loop, the merge, AND the file writes. Returns the
    same dict that landed in session-transcript.json on disk."""
    install_stub_transcriber(TranscriberStub(backend="fake-be", model="fake-m", text="merged"))
    sd = seed_session(recorder_under_test.recordings_dir, "s", SESSION_WAVS)

    request = BatchSessionRequest(
        session="s",
        source="original",
        model="fake-m",
        backend="cpu",
        from_iso=None,
        to_iso=None,
        force=False,
        source_lang=None,
    )
    merged = await transcribe_session(recorder_under_test, request)

    assert merged["model"] == "fake-m"
    assert (sd / "session-transcript.json").is_file()
    assert (sd / "session-transcript.txt").is_file()
    written = json.loads((sd / "session-transcript.json").read_text(encoding="utf-8"))
    assert written == merged


async def test_transcribe_session_releases_jobtracker_on_success(
    recorder_under_test, install_stub_transcriber
):
    """Whether the loop succeeds OR raises, the JobTracker slot must
    be released. Otherwise a single bad run would wedge the session
    forever (the next call would 409 even after the operator fixed
    the root cause)."""
    install_stub_transcriber(TranscriberStub(backend="fake-be", model="fake-m"))
    seed_session(recorder_under_test.recordings_dir, "s", SESSION_WAVS)

    request = BatchSessionRequest(
        session="s",
        source="original",
        model="fake-m",
        backend="cpu",
        from_iso=None,
        to_iso=None,
        force=False,
        source_lang=None,
    )
    await transcribe_session(recorder_under_test, request)
    assert recorder_under_test.jobs.get("s") is None


async def test_transcribe_session_releases_jobtracker_on_exception(
    recorder_under_test, install_stub_transcriber
):
    """A mid-loop transcriber crash must still release the slot."""

    class _Boom(TranscriberStub):
        def transcribe(self, *a, **kw):  # noqa: ARG002
            raise RuntimeError("model exploded")

    install_stub_transcriber(_Boom(backend="fake-be", model="fake-m"))
    seed_session(recorder_under_test.recordings_dir, "s", SESSION_WAVS)

    request = BatchSessionRequest(
        session="s",
        source="original",
        model="fake-m",
        backend="cpu",
        from_iso=None,
        to_iso=None,
        force=False,
        source_lang=None,
    )
    with pytest.raises(RuntimeError, match="model exploded"):
        await transcribe_session(recorder_under_test, request)
    assert recorder_under_test.jobs.get("s") is None


async def test_transcribe_session_raises_session_busy_when_slot_taken(
    recorder_under_test, install_stub_transcriber
):
    install_stub_transcriber(TranscriberStub(backend="fake-be", model="fake-m"))
    seed_session(recorder_under_test.recordings_dir, "s", SESSION_WAVS)
    # Pre-claim the slot — as if another transcribe were in flight.
    await recorder_under_test.jobs.claim(
        JobState(
            session="s",
            kind="transcribe",
            current=0,
            total=1,
            started_at=datetime.now(UTC),
            status="running",
        )
    )

    request = BatchSessionRequest(
        session="s",
        source="original",
        model="fake-m",
        backend="cpu",
        from_iso=None,
        to_iso=None,
        force=False,
        source_lang=None,
    )
    with pytest.raises(SessionBusy):
        await transcribe_session(recorder_under_test, request)


async def test_transcribe_session_locked_uses_caller_slot_and_releases_model(
    recorder_under_test, install_stub_transcriber, monkeypatch
):
    """The end-of-meeting pipeline drives the transcribe core directly under
    its own `kind="pipeline"` claim: the core must write the merged outputs,
    report per-WAV progress through the CALLER's job handle, release the
    model, and leave the caller's claim alone."""
    from tapscribe.text import write_languages

    # A specialist-free set keeps the cover single-model, so this stays a focused
    # test of the one-model release contract (the multi-model cover releases one
    # model per cover entry — exercised in the slice-2 tests).
    write_languages("en")
    install_stub_transcriber(TranscriberStub(backend="fake-be", model="fake-m", text="merged"))
    released: list = []
    monkeypatch.setattr("tapscribe.transcribers.release_transcriber", released.append)
    sd = seed_session(recorder_under_test.recordings_dir, "s", SESSION_WAVS)
    claimed = await recorder_under_test.jobs.claim(
        JobState(
            session="s", kind="pipeline", current=0, total=2, started_at=datetime.now(UTC), status="running"
        )
    )
    assert claimed

    selection = select_session_wavs(sd, from_iso=None, to_iso=None, source="original")
    request = BatchSessionRequest(
        session="s",
        source="original",
        model="fake-m",
        backend="cpu",
        from_iso=None,
        to_iso=None,
        force=False,
        source_lang=None,
    )
    merged = await transcribe_session_locked(
        request, selection=selection, job=recorder_under_test.jobs.handle("s")
    )

    assert merged["model"] == "fake-m"
    assert (sd / "session-transcript.json").is_file()
    held = recorder_under_test.jobs.get("s")
    assert held is not None and held.kind == "pipeline"  # claim untouched
    # Per-WAV progress flowed through the caller's slot, not a fresh claim.
    assert held.current_file == SESSION_WAVS[-1]
    assert len(released) == 1  # model released on the way out


async def test_transcribe_session_raises_no_usable_wavs_on_empty_range(
    recorder_under_test, install_stub_transcriber
):
    install_stub_transcriber(TranscriberStub(backend="fake-be", model="fake-m"))
    # Empty session — no WAVs match anything.
    (recorder_under_test.recordings_dir / "s").mkdir(parents=True)

    request = BatchSessionRequest(
        session="s",
        source="original",
        model="fake-m",
        backend="cpu",
        from_iso=None,
        to_iso=None,
        force=False,
        source_lang=None,
    )
    with pytest.raises(NoUsableWavs):
        await transcribe_session(recorder_under_test, request)


async def test_transcribe_session_raises_invalid_range_on_unparseable_iso(
    recorder_under_test, install_stub_transcriber
):
    """Unparseable from_iso surfaces as `InvalidRange` — the route maps
    it to 400. Distinct from `NoUsableWavs` (404) because the inputs
    were syntactically wrong, not just empty-result."""
    install_stub_transcriber(TranscriberStub(backend="fake-be", model="fake-m"))
    seed_session(recorder_under_test.recordings_dir, "s", SESSION_WAVS)

    request = BatchSessionRequest(
        session="s",
        source="original",
        model="fake-m",
        backend="cpu",
        from_iso="not-a-timestamp",
        to_iso=None,
        force=False,
        source_lang=None,
    )
    with pytest.raises(InvalidRange):
        await transcribe_session(recorder_under_test, request)


async def test_transcribe_session_progress_updates_per_wav(recorder_under_test, install_stub_transcriber):
    """Per-WAV progress lands in the JobTracker BEFORE each transcribe
    call, so the dashboard's once-per-second poll sees `current` advance
    in real time. We capture the observed progress sequence inside the
    stub itself, since the slot is released by the time the call
    returns."""
    observed_currents: list[int] = []
    observed_files: list[str | None] = []

    class _ProgressSpy(TranscriberStub):
        def __init__(self, recorder, **kw):
            super().__init__(**kw)
            self._recorder = recorder

        def transcribe(self, path, **kw):  # noqa: ARG002
            job = self._recorder.jobs.get("s")
            if job is not None:
                observed_currents.append(job.current)
                observed_files.append(job.current_file)
            return super().transcribe(path, **kw)

    install_stub_transcriber(_ProgressSpy(recorder_under_test, backend="fake-be", model="fake-m"))
    seed_session(recorder_under_test.recordings_dir, "s", SESSION_WAVS)

    request = BatchSessionRequest(
        session="s",
        source="original",
        model="fake-m",
        backend="cpu",
        from_iso=None,
        to_iso=None,
        force=False,
        source_lang=None,
    )
    await transcribe_session(recorder_under_test, request)

    assert observed_currents == [0, 1]
    assert observed_files == list(SESSION_WAVS)


async def test_transcribe_session_runs_model_on_one_dedicated_thread(
    recorder_under_test, install_stub_transcriber
):
    """MLX's Metal GPU stream is thread-local: every model op for a job must
    run on ONE thread or `mx.eval` raises "There is no Stream(gpu, 0) in
    current thread". The loop offloaded each `cached_transcribe` via
    `asyncio.to_thread` (the shared default executor), so under concurrent
    offloaded work (the ~2 Hz /api/state poll, the per-clip loop) a later
    `generate` could land on a worker without the model's stream. Stripped
    sessions surface it reliably — every freshly-cut clip is a cache MISS, so
    the model actually runs for each. The fix pins all model work to the
    dedicated `tapscribe-model` worker. The assertion is MLX-free (a stub
    records its thread) so it guards the contract on every CI host, not just
    Metal."""
    import threading

    from tapscribe.transcribers import MODEL_THREAD_PREFIX

    seen_threads: list[str] = []

    class _ThreadSpy(TranscriberStub):
        def transcribe(self, path, **kw):  # noqa: ARG002
            seen_threads.append(threading.current_thread().name)
            return super().transcribe(path, **kw)

    install_stub_transcriber(_ThreadSpy(backend="fake-be", model="fake-m"))
    seed_session(recorder_under_test.recordings_dir, "s", SESSION_WAVS)

    request = BatchSessionRequest(
        session="s",
        source="original",
        model="fake-m",
        backend="cpu",
        from_iso=None,
        to_iso=None,
        force=False,
        source_lang=None,
    )
    await transcribe_session(recorder_under_test, request)

    assert len(seen_threads) == len(SESSION_WAVS), seen_threads
    # Every clip transcribed on the SAME thread, and that thread is the
    # dedicated model worker — never a default-pool thread.
    assert len(set(seen_threads)) == 1, f"model scattered across threads: {seen_threads}"
    assert seen_threads[0].startswith(MODEL_THREAD_PREFIX), seen_threads[0]


async def test_transcribe_session_selection_runs_off_the_event_loop(
    recorder_under_test, install_stub_transcriber, monkeypatch
):
    """Issue #214: `select_session_wavs` walks every WAV in the session and
    reads a whole-file RMS pass for each — pure disk/CPU work that must be
    offloaded via `asyncio.to_thread` (before the job slot is even claimed,
    per the issue) rather than run inline on the event loop."""
    import threading

    from tapscribe.session_merge import select_session_wavs as real_select

    install_stub_transcriber(TranscriberStub(backend="fake-be", model="fake-m", text="merged"))
    seed_session(recorder_under_test.recordings_dir, "s", SESSION_WAVS)

    seen_is_main: list[bool] = []

    def _spy(*a, **kw):
        seen_is_main.append(threading.current_thread() is threading.main_thread())
        return real_select(*a, **kw)

    monkeypatch.setattr("tapscribe.batch_transcribe.select_session_wavs", _spy)

    request = BatchSessionRequest(
        session="s",
        source="original",
        model="fake-m",
        backend="cpu",
        from_iso=None,
        to_iso=None,
        force=False,
        source_lang=None,
    )
    await transcribe_session(recorder_under_test, request)

    assert seen_is_main == [False], seen_is_main


async def test_transcribe_session_locked_merge_and_write_run_off_the_event_loop(
    recorder_under_test, install_stub_transcriber, monkeypatch
):
    """Issue #214: `merge_session` re-parses every WAV's cached sidecar JSON,
    and the merged transcript is then `json.dumps`'d and written twice — all
    pure disk/CPU work that must be offloaded so the merge-and-write tail
    doesn't stall the loop right after the model loop finishes."""
    import threading

    from tapscribe.session_merge import merge_session as real_merge

    install_stub_transcriber(TranscriberStub(backend="fake-be", model="fake-m", text="merged"))
    seed_session(recorder_under_test.recordings_dir, "s", SESSION_WAVS)

    seen_is_main: list[bool] = []

    def _spy(*a, **kw):
        seen_is_main.append(threading.current_thread() is threading.main_thread())
        return real_merge(*a, **kw)

    monkeypatch.setattr("tapscribe.batch_transcribe.merge_session", _spy)

    request = BatchSessionRequest(
        session="s",
        source="original",
        model="fake-m",
        backend="cpu",
        from_iso=None,
        to_iso=None,
        force=False,
        source_lang=None,
    )
    await transcribe_session(recorder_under_test, request)

    assert seen_is_main == [False], seen_is_main


# ---------------------------------------------------------------------------
# Exception taxonomy — transcribe-specific errors share BatchTranscribeError;
# the cross-cutting ones live with their concept and are deliberately NOT
# BatchTranscribeError (SessionBusy is a JobTracker concept; NoUsableWavs /
# InvalidRange are selection verdicts), so strip/summarize don't import a
# transcribe base just to raise them.
# ---------------------------------------------------------------------------


def test_transcribe_specific_errors_inherit_base():
    for cls in (WavUnreadable, WavTooQuiet):
        assert issubclass(cls, BatchTranscribeError)


def test_relocated_errors_are_decoupled_from_transcribe_base():
    for cls in (SessionBusy, NoUsableWavs, InvalidRange):
        assert not issubclass(cls, BatchTranscribeError)


# ---------------------------------------------------------------------------
# Candidate-language resolution + apply (ADR-0010). The operator declares a
# candidate-language *set* per meeting; the batch path turns it into a per-
# region language for the generalist: a singleton set pins (source_lang); a
# multi-language set defers to a per-region constrained auto-detect that snaps
# the detected language to the set. Both transcribe_session and the pipeline's
# transcribe stage go through transcribe_session_locked, so testing it here
# covers both entry points.
# ---------------------------------------------------------------------------


# The shared `TranscriberStub` (conftest) already records `seen_source_lang`
# and echoes it into the result, so the plain / pinned tests use it directly.
# Only the constrained-detect capability is new — add it by subclassing, the
# file's established `_Spy(TranscriberStub)` pattern.
class _DetectorStub(TranscriberStub):
    """A `TranscriberStub` that also advertises the constrained-detection
    capability: records the candidate set it was asked to snap to, and returns a
    fixed winner (so a test can assert the constrained pick flows through to
    transcribe as the pin)."""

    def __init__(self, *, winner, **kw):
        super().__init__(**kw)
        self.winner = winner
        self.seen_candidates: list[tuple[str, ...]] = []

    def detect_constrained_language(self, path, candidate_languages):  # noqa: ARG002
        self.seen_candidates.append(tuple(candidate_languages))
        return self.winner


def _session_request(model="fake-m"):
    return BatchSessionRequest(
        session="s",
        source="original",
        model=model,
        backend="cpu",
        from_iso=None,
        to_iso=None,
        force=False,
        source_lang=None,
    )


async def _run_session(recorder, stub, install_stub_transcriber):
    install_stub_transcriber(stub)
    seed_session(recorder.recordings_dir, "s", [WAV_NAME])
    selection = select_session_wavs(
        recorder.recordings_dir / "s", from_iso=None, to_iso=None, source="original"
    )
    async with recorder.jobs.run("s", kind="transcribe", total=1) as handle:
        await transcribe_session_locked(_session_request(), selection=selection, job=handle)


async def test_singleton_candidate_set_pins_that_language(recorder_under_test, install_stub_transcriber):
    """A meeting whose candidate set is a single language pins it directly —
    the transcriber is driven with source_lang=that code, on every backend."""
    from tapscribe.text import write_languages

    write_languages("da")
    stub = TranscriberStub(backend="fake-be", model="fake-m")
    await _run_session(recorder_under_test, stub, install_stub_transcriber)
    assert stub.seen_source_lang == ["da"]


async def test_manual_single_wav_transcribe_applies_candidate_languages(
    recorder_under_test, install_stub_transcriber
):
    """ADR-0011: the manual single-WAV path is now language-driven too — it runs
    the meeting's cover as a one-WAV slice. A singleton global default pins that
    language (the specialist-free `da` keeps the cover single-model), so the
    transcriber is driven with source_lang="da", not the request's own None."""
    from tapscribe.text import write_languages

    write_languages("da")
    stub = TranscriberStub(backend="fake-be", model="fake-m")
    install_stub_transcriber(stub)
    seed_session(recorder_under_test.recordings_dir, "s", [WAV_NAME])
    await transcribe_one(
        recorder_under_test,
        BatchOneRequest(
            session="s",
            name=WAV_NAME,
            source="original",
            model="fake-m",
            backend="cpu",
            source_lang=None,
        ),
    )
    assert stub.seen_source_lang == ["da"]


async def test_manual_single_wav_transcribe_runs_cover_and_picks_winner(recorder_under_test, monkeypatch):
    """ADR-0011 headline: re-transcribing ONE WAV runs the same cover as the range
    — the generalist AND the Norwegian specialist both transcribe the WAV, two
    sidecars land, and _primary points at the selector's winner (here the
    generalist keeps the English region). The single-WAV counterpart to
    test_cover_runs_both_models_and_selector_routes_primary_per_region."""
    from tapscribe.text import write_languages
    from tapscribe.wav_cache import read_all_cached, read_cached

    write_languages("no, en")  # cover = {generalist, nb-whisper-large}
    generalist = _ConfidenceStub(
        backend="faster-whisper",
        model="fake-generalist",
        logprob_by_marker={"armstrong": -0.10},
        language_by_marker={"armstrong": "en"},  # English region → generalist wins
    )
    specialist = _ConfidenceStub(
        backend="faster-whisper", model="nb-whisper-large", logprob_by_marker={"armstrong": -0.80}
    )
    _install_by_model(monkeypatch, {"nb-whisper-large": specialist}, default=generalist)

    sd = seed_session(recorder_under_test.recordings_dir, "s", [COVER_WAVS[1]])
    payload = await transcribe_one(
        recorder_under_test,
        BatchOneRequest(
            session="s",
            name=COVER_WAVS[1],
            source="original",
            model="fake-generalist",
            backend="cpu",
            source_lang=None,
        ),
    )
    armstrong = sd / COVER_WAVS[1]
    # Both cover models ran on the one WAV — two sidecars.
    assert {c.result.model for c in read_all_cached(armstrong)} == {"fake-generalist", "nb-whisper-large"}
    # _primary points at the winner, and that's what the returned payload is.
    assert read_cached(armstrong).result.model == "fake-generalist"
    assert payload["model"] == "fake-generalist"


async def test_multi_candidate_set_constrains_detection_to_the_set(
    recorder_under_test, install_stub_transcriber
):
    """A multi-language meeting defers to a per-region constrained auto-detect:
    the cache loop asks the adapter to snap the language to the candidate set,
    and drives transcribe with that winner — never a language outside the set
    (no drift to e.g. sv). Uses the specialist-free pair {da, en} so the cover
    stays single-model; the Norwegian specialist's cover behaviour is exercised
    in the slice-2 tests below."""
    from tapscribe.text import write_languages

    write_languages("da, en")
    stub = _DetectorStub(winner="da")
    await _run_session(recorder_under_test, stub, install_stub_transcriber)
    # The adapter was asked to choose WITHIN exactly the declared set…
    assert stub.seen_candidates == [("da", "en")]
    # …and its in-set winner became the pin handed to transcribe.
    assert stub.seen_source_lang == ["da"]


async def test_session_meta_languages_override_beats_global_default(
    recorder_under_test, install_stub_transcriber
):
    """A per-meeting `languages` override takes precedence over the global
    default — here it narrows the meeting to a single language, which pins."""
    from tapscribe.sessions import write_session_meta
    from tapscribe.text import write_languages

    write_languages("da, no")  # global default is multi
    seed_session(recorder_under_test.recordings_dir, "s", [WAV_NAME])
    write_session_meta("s", {"languages": ["en"]})  # this meeting is English-only
    stub = TranscriberStub(backend="fake-be", model="fake-m")
    install_stub_transcriber(stub)
    selection = select_session_wavs(
        recorder_under_test.recordings_dir / "s", from_iso=None, to_iso=None, source="original"
    )
    async with recorder_under_test.jobs.run("s", kind="transcribe", total=1) as handle:
        await transcribe_session_locked(_session_request(), selection=selection, job=handle)
    assert stub.seen_source_lang == ["en"]


async def test_changing_candidate_set_re_detects_on_non_force_rerun(
    recorder_under_test, install_stub_transcriber
):
    """Changing the meeting's candidate set must re-detect even WITHOUT force: a
    constrained entry is keyed on the language it resolved to, so widening
    {da, en} → {da, en, sv} re-runs and re-pins, instead of serving the stale
    {da, en} pick. This is the "fix a mis-detection by adding the language"
    workflow (ADR-0010) — it would silently no-op if the cache served the old
    pick on a non-force re-run. Specialist-free sets keep the cover single-model
    so this stays a focused test of the re-detect cache key."""
    from tapscribe.sessions import write_session_meta
    from tapscribe.text import write_languages

    # A detector that picks the LAST declared language, so a different set
    # yields a different pin (stands in for the real argmax shifting once a
    # better-matching candidate is added).
    class _LastWins(TranscriberStub):
        def detect_constrained_language(self, path, candidate_languages):  # noqa: ARG002
            return candidate_languages[-1]

    stub = _LastWins(backend="fake-be", model="fake-m")
    install_stub_transcriber(stub)
    seed_session(recorder_under_test.recordings_dir, "s", [WAV_NAME])
    selection = select_session_wavs(
        recorder_under_test.recordings_dir / "s", from_iso=None, to_iso=None, source="original"
    )

    write_languages("da, en")  # global default {da, en} → last = "en"
    async with recorder_under_test.jobs.run("s", kind="transcribe", total=1) as handle:
        await transcribe_session_locked(_session_request(), selection=selection, job=handle)
    assert stub.seen_source_lang == ["en"]

    # Widen the meeting to {da, en, sv}; a NON-force re-run must re-detect to "sv".
    write_session_meta("s", {"languages": ["da", "en", "sv"]})
    async with recorder_under_test.jobs.run("s", kind="transcribe", total=1) as handle:
        await transcribe_session_locked(_session_request(), selection=selection, job=handle)
    assert stub.seen_source_lang == ["en", "sv"], (
        "widening the candidate set must re-detect on a non-force re-run, not serve the stale pick"
    )


# ---------------------------------------------------------------------------
# Cover + select (ADR-0010 slice 2). A meeting whose candidate set contains a
# language with a specialist (v1: no → nb-whisper) runs the generalist AND the
# specialist on every region, and a pluggable selector picks the winner per
# region into _primary. merge_session then stitches a mixed-language transcript.
# The slice-1 tests above pin a specialist-free set so they stay single-model;
# these opt INTO cover by declaring Norwegian.
# ---------------------------------------------------------------------------


class _ConfidenceStub(TranscriberStub):
    """A `TranscriberStub` whose per-segment avg_logprob is chosen by which
    marker substring appears in the WAV name, so the acoustic-confidence
    selector's per-region winner is deterministic. Distinct (backend, model)
    per instance → distinct sidecars, the way two real cover models land side
    by side in the cache."""

    def __init__(
        self, *, logprob_by_marker: dict[str, float], language_by_marker: dict[str, str] | None = None, **kw
    ):
        super().__init__(**kw)
        self.logprob_by_marker = logprob_by_marker
        # The per-region detected language the generalist reports (the
        # SpecialistRoutingSelector's routing key); defaults to "no".
        self.language_by_marker = language_by_marker or {}

    def transcribe(self, path, *, initial_prompt=None, hotwords=None, source_lang=None):  # noqa: ARG002
        from tapscribe.transcribers.base import TranscriptionSegment, build_transcription_result

        self.calls.append(path)
        self.seen_source_lang.append(source_lang)
        marker = next((m for m in self.logprob_by_marker if m in path.name), None)
        logprob = self.logprob_by_marker.get(marker, -1.0)
        language = self.language_by_marker.get(marker) or source_lang or "no"
        text = f"{self.model_name}:{marker or 'unknown'}"
        return build_transcription_result(
            self,
            text=text,
            segments=(TranscriptionSegment(start=0.0, end=1.0, text=text, avg_logprob=logprob),),
            duration=1.0,
            language=language,
            language_probability=1.0,
            source_lang=source_lang,
        )


def _install_by_model(monkeypatch, by_model: dict, *, default):
    """Patch `load_transcriber` to dispatch on model_id — the cover loads each
    model in turn, so the fakes must differ per id (unlike the single-stub
    `install_stub_transcriber`)."""
    monkeypatch.setattr(
        "tapscribe.transcribers.load_transcriber",
        lambda model_id, **kw: by_model.get(model_id, default),  # noqa: ARG005
    )


COVER_WAVS = [
    "2026-01-01T01-00-00Z__marlene__a.wav",  # Norwegian-leaning region
    "2026-01-01T01-00-05Z__armstrong__b.wav",  # English-leaning region
]


async def test_cover_runs_both_models_and_selector_routes_primary_per_region(
    recorder_under_test, monkeypatch
):
    """The slice-2 headline: a da/no/en meeting transcribes EACH region with
    both the generalist and the Norwegian specialist, and the selector points
    each region's _primary at the higher-confidence transcript INDEPENDENTLY —
    the Norwegian clip routes to nb-whisper, the English clip to the generalist,
    in one run."""
    from tapscribe.text import write_languages
    from tapscribe.wav_cache import read_all_cached, read_cached

    write_languages("no, en")  # cover = {generalist, nb-whisper-large}
    # The generalist detects each region's language; that drives routing. The
    # avg_logprob is set so acoustic would mis-route armstrong (gen -0.10 wins)
    # but route marlene to the specialist anyway — the point is that routing
    # follows the DETECTED language, not the confidence.
    generalist = _ConfidenceStub(
        backend="faster-whisper",
        model="fake-generalist",
        logprob_by_marker={"marlene": -0.90, "armstrong": -0.10},
        language_by_marker={"marlene": "no", "armstrong": "en"},
    )
    specialist = _ConfidenceStub(
        backend="faster-whisper",
        model="nb-whisper-large",
        logprob_by_marker={"marlene": -0.20, "armstrong": -0.80},
    )
    _install_by_model(monkeypatch, {"nb-whisper-large": specialist}, default=generalist)

    sd = seed_session(recorder_under_test.recordings_dir, "s", COVER_WAVS)
    selection = select_session_wavs(sd, from_iso=None, to_iso=None, source="original")
    async with recorder_under_test.jobs.run("s", kind="transcribe", total=len(COVER_WAVS)) as handle:
        merged = await transcribe_session_locked(
            _session_request(model="fake-generalist"), selection=selection, job=handle
        )

    marlene, armstrong = sd / COVER_WAVS[0], sd / COVER_WAVS[1]
    # Both models ran on EVERY region — two sidecars apiece.
    assert {c.result.model for c in read_all_cached(marlene)} == {"fake-generalist", "nb-whisper-large"}
    assert {c.result.model for c in read_all_cached(armstrong)} == {"fake-generalist", "nb-whisper-large"}
    # …and _primary points at the per-region winner.
    assert read_cached(marlene).result.model == "nb-whisper-large"
    assert read_cached(armstrong).result.model == "fake-generalist"
    # merge_session stitched the WINNERS, not whichever model ran last.
    assert "nb-whisper-large:marlene" in merged["plain_text"]
    assert "fake-generalist:armstrong" in merged["plain_text"]
    assert "fake-generalist:marlene" not in merged["plain_text"]


async def test_specialist_free_meeting_runs_generalist_only(recorder_under_test, monkeypatch):
    """A candidate set with no specialist language collapses to the slice-1
    generalist-only path: one model, one sidecar, the selector is never
    consulted (so a future selector regression can't affect monolingual runs)."""
    from tapscribe.text import write_languages
    from tapscribe.wav_cache import read_all_cached

    write_languages("da, en")  # neither has a specialist
    generalist = _ConfidenceStub(backend="faster-whisper", model="fake-generalist", logprob_by_marker={})
    nb_loaded: list[str] = []

    def _factory(model_id, **kw):  # noqa: ARG001
        if model_id != "fake-generalist":
            nb_loaded.append(model_id)
        return generalist

    def _no_selector():
        raise AssertionError("selector must not run for a single-model cover")

    monkeypatch.setattr("tapscribe.transcribers.load_transcriber", _factory)
    monkeypatch.setattr("tapscribe.batch_transcribe.default_language_selector", _no_selector)

    sd = seed_session(recorder_under_test.recordings_dir, "s", COVER_WAVS)
    selection = select_session_wavs(sd, from_iso=None, to_iso=None, source="original")
    async with recorder_under_test.jobs.run("s", kind="transcribe", total=len(COVER_WAVS)) as handle:
        await transcribe_session_locked(
            _session_request(model="fake-generalist"), selection=selection, job=handle
        )

    assert nb_loaded == [], f"no specialist should load for a specialist-free set, loaded: {nb_loaded}"
    assert len(read_all_cached(sd / COVER_WAVS[0])) == 1


async def test_selector_is_pluggable_swapping_it_needs_no_pipeline_change(recorder_under_test, monkeypatch):
    """The selector is a seam: swapping the strategy is a one-function change
    with NO edit to transcribe_session_locked. Here a selector that always
    keeps the first (generalist) candidate overrides the acoustic default, so
    the generalist wins even on the Norwegian clip where nb-whisper is more
    confident — and it receives the meeting's declared set (what a constrained
    text-LID selector would need), proving the seam is wide enough."""
    from tapscribe.text import write_languages
    from tapscribe.wav_cache import read_cached

    write_languages("no, en")
    generalist = _ConfidenceStub(
        backend="faster-whisper", model="fake-generalist", logprob_by_marker={"marlene": -0.90}
    )
    specialist = _ConfidenceStub(
        backend="faster-whisper", model="nb-whisper-large", logprob_by_marker={"marlene": -0.10}
    )
    _install_by_model(monkeypatch, {"nb-whisper-large": specialist}, default=generalist)

    seen_langs: list[tuple[str, ...]] = []

    class _AlwaysFirst:
        def select(self, candidates, *, candidate_languages=()):
            seen_langs.append(tuple(candidate_languages))
            return candidates[0]

    monkeypatch.setattr("tapscribe.batch_transcribe.default_language_selector", _AlwaysFirst)

    sd = seed_session(recorder_under_test.recordings_dir, "s", [COVER_WAVS[0]])
    selection = select_session_wavs(sd, from_iso=None, to_iso=None, source="original")
    async with recorder_under_test.jobs.run("s", kind="transcribe", total=1) as handle:
        await transcribe_session_locked(
            _session_request(model="fake-generalist"), selection=selection, job=handle
        )

    # The custom selector kept the generalist despite nb-whisper's higher score…
    assert read_cached(sd / COVER_WAVS[0]).result.model == "fake-generalist"
    # …and the pipeline handed it the declared candidate set.
    assert seen_langs == [("no", "en")]


async def test_cover_rerun_without_force_keeps_primary_and_skips_retranscribe(
    recorder_under_test, monkeypatch
):
    """A non-force re-run of a covered meeting hits the per-WAV cache for both
    models (no re-transcribe) and the selector re-affirms the same _primary —
    idempotent, so re-opening a meeting doesn't churn the model or flip the
    winner."""
    from tapscribe.text import write_languages
    from tapscribe.wav_cache import read_cached

    write_languages("no, en")
    generalist = _ConfidenceStub(
        backend="faster-whisper", model="fake-generalist", logprob_by_marker={"marlene": -0.90}
    )
    specialist = _ConfidenceStub(
        backend="faster-whisper", model="nb-whisper-large", logprob_by_marker={"marlene": -0.20}
    )
    _install_by_model(monkeypatch, {"nb-whisper-large": specialist}, default=generalist)

    sd = seed_session(recorder_under_test.recordings_dir, "s", [COVER_WAVS[0]])
    selection = select_session_wavs(sd, from_iso=None, to_iso=None, source="original")
    async with recorder_under_test.jobs.run("s", kind="transcribe", total=1) as handle:
        await transcribe_session_locked(
            _session_request(model="fake-generalist"), selection=selection, job=handle
        )
    calls_after_first = len(generalist.calls) + len(specialist.calls)
    assert read_cached(sd / COVER_WAVS[0]).result.model == "nb-whisper-large"

    # Second pass, force=False: every (backend, model) sidecar is a cache hit.
    async with recorder_under_test.jobs.run("s", kind="transcribe", total=1) as handle:
        await transcribe_session_locked(
            _session_request(model="fake-generalist"), selection=selection, job=handle
        )
    assert len(generalist.calls) + len(specialist.calls) == calls_after_first, (
        "a non-force re-run must not re-transcribe either cover model"
    )
    assert read_cached(sd / COVER_WAVS[0]).result.model == "nb-whisper-large"


async def test_narrowing_languages_repoints_primary_off_dropped_specialist(recorder_under_test, monkeypatch):
    """ADR-0011 stale-primary guard: after a {no,en} cover routes the Norwegian
    region to nb-whisper, NARROWING the meeting to English (a specialist-free,
    single-model cover) and re-transcribing must repoint _primary to the
    generalist — not leave it aimed at the now-unused specialist. The single-model
    path used to skip set_primary; `_select_primaries` now always repoints."""
    from tapscribe.text import write_languages
    from tapscribe.wav_cache import read_all_cached, read_cached

    write_languages("no, en")
    generalist = _ConfidenceStub(
        backend="faster-whisper", model="fake-generalist", logprob_by_marker={"marlene": -0.90}
    )
    specialist = _ConfidenceStub(
        backend="faster-whisper", model="nb-whisper-large", logprob_by_marker={"marlene": -0.20}
    )
    _install_by_model(monkeypatch, {"nb-whisper-large": specialist}, default=generalist)

    sd = seed_session(recorder_under_test.recordings_dir, "s", [COVER_WAVS[0]])
    selection = select_session_wavs(sd, from_iso=None, to_iso=None, source="original")
    async with recorder_under_test.jobs.run("s", kind="transcribe", total=1) as handle:
        await transcribe_session_locked(
            _session_request(model="fake-generalist"), selection=selection, job=handle
        )
    assert read_cached(sd / COVER_WAVS[0]).result.model == "nb-whisper-large"  # specialist won pass 1

    # Narrow to English only → specialist-free, single-model cover, and re-run.
    write_languages("en")
    selection = select_session_wavs(sd, from_iso=None, to_iso=None, source="original")
    async with recorder_under_test.jobs.run("s", kind="transcribe", total=1) as handle:
        await transcribe_session_locked(
            _session_request(model="fake-generalist"), selection=selection, job=handle
        )
    # _primary flips to the generalist even though only ONE model ran this pass…
    assert read_cached(sd / COVER_WAVS[0]).result.model == "fake-generalist"
    # …and the specialist sidecar is still on disk — a pointer flip, not a delete.
    assert {c.result.model for c in read_all_cached(sd / COVER_WAVS[0])} == {
        "fake-generalist",
        "nb-whisper-large",
    }


async def test_explicit_source_lang_pin_runs_generalist_only(recorder_under_test, monkeypatch):
    """An explicit per-job `source_lang` pin (the manual transcribe route)
    BYPASSES the candidate-set cover and honours the operator's chosen model:
    even a Norwegian pin runs ONLY the generalist — nb-whisper never loads, so
    the selector can't override `req.model`. (The candidate-set machinery is the
    languages.txt / session-meta path, not the legacy source_lang body param.)"""
    from tapscribe.text import write_languages
    from tapscribe.wav_cache import read_all_cached, read_cached

    write_languages("no, en")  # a DECLARED {no, en} would cover nb-whisper…
    generalist = _ConfidenceStub(backend="faster-whisper", model="fake-generalist", logprob_by_marker={})
    loaded: list[str] = []

    def _factory(model_id, **kw):  # noqa: ARG001
        loaded.append(model_id)
        return generalist

    monkeypatch.setattr("tapscribe.transcribers.load_transcriber", _factory)

    sd = seed_session(recorder_under_test.recordings_dir, "s", [COVER_WAVS[0]])
    selection = select_session_wavs(sd, from_iso=None, to_iso=None, source="original")
    req = BatchSessionRequest(
        session="s",
        source="original",
        model="fake-generalist",
        backend="cpu",
        from_iso=None,
        to_iso=None,
        force=False,
        source_lang="no",  # …but an explicit PIN bypasses it
    )
    async with recorder_under_test.jobs.run("s", kind="transcribe", total=1) as handle:
        await transcribe_session_locked(req, selection=selection, job=handle)

    assert loaded == ["fake-generalist"], f"an explicit pin must run only the generalist, loaded: {loaded}"
    assert len(read_all_cached(sd / COVER_WAVS[0])) == 1
    assert read_cached(sd / COVER_WAVS[0]).result.model == "fake-generalist"


async def test_specialist_loads_with_auto_backend_not_the_generalists(recorder_under_test, monkeypatch):
    """The operator's backend preference is for THEIR chosen generalist; the
    system-routed specialist self-resolves ("auto"). So an MLX-preference
    generalist doesn't drag nb-whisper (cpu/cuda only, no MLX binding) into an
    unsupported-backend crash on Apple Silicon — the cover survives there."""
    from tapscribe.text import write_languages

    write_languages("no, en")
    seen_backend: dict[str, str] = {}
    generalist = _ConfidenceStub(
        backend="faster-whisper", model="fake-generalist", logprob_by_marker={"marlene": -0.5}
    )
    specialist = _ConfidenceStub(
        backend="faster-whisper", model="nb-whisper-large", logprob_by_marker={"marlene": -0.2}
    )

    def _factory(model_id, *, backend="auto", **kw):  # noqa: ARG001
        seen_backend[model_id] = backend
        return specialist if model_id == "nb-whisper-large" else generalist

    monkeypatch.setattr("tapscribe.transcribers.load_transcriber", _factory)

    sd = seed_session(recorder_under_test.recordings_dir, "s", [COVER_WAVS[0]])
    selection = select_session_wavs(sd, from_iso=None, to_iso=None, source="original")
    req = BatchSessionRequest(
        session="s",
        source="original",
        model="fake-generalist",
        backend="mlx",  # operator forced MLX (Apple Silicon)
        from_iso=None,
        to_iso=None,
        force=False,
        source_lang=None,
    )
    async with recorder_under_test.jobs.run("s", kind="transcribe", total=1) as handle:
        await transcribe_session_locked(req, selection=selection, job=handle)

    assert seen_backend["fake-generalist"] == "mlx"  # operator's choice honoured for the generalist
    assert seen_backend["nb-whisper-large"] == "auto"  # specialist self-routes — no MLX crash


def test_resolve_batch_model_warn_flag_gates_the_invalid_config_log(recorder_under_test, capsys):
    """An out-of-band invalid batch-model.txt warns once by default (on a real
    transcribe), but resolve_batch_model(warn=False) — used by the ~2 Hz /api/state
    poll to DISPLAY the generalist — stays silent, so it can't spam stdout twice a
    second. Both still fall back to the bundled default."""
    from tapscribe.batch_transcribe import resolve_batch_model
    from tapscribe.transcribers.catalog import DEFAULT_BATCH_MODEL

    # Write the file DIRECTLY (bypassing write_config's write-time validation) to
    # simulate a since-removed model id / out-of-band edit.
    (recorder_under_test.config_dir / "batch-model.txt").write_text("not-a-real-model", encoding="utf-8")

    assert resolve_batch_model(warn=False) == DEFAULT_BATCH_MODEL
    assert "not a usable batch model" not in capsys.readouterr().out  # poll path: silent

    assert resolve_batch_model(warn=True) == DEFAULT_BATCH_MODEL
    assert "not a usable batch model" in capsys.readouterr().out  # on-demand: warns once
