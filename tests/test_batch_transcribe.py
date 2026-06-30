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
    returns the supplied stub. Patches the binding in batch_transcribe
    (the consumer) rather than the source package — same reason
    test_routes.py double-patches."""

    def _install(stub):
        monkeypatch.setattr(
            "tapscribe.batch_transcribe.load_transcriber",
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
        target_lang=None,
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
        target_lang=None,
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
        target_lang=None,
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
        def transcribe(self, path, *, initial_prompt=None, hotwords=None, source_lang=None, target_lang=None):  # noqa: ARG002
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
        target_lang=None,
    )
    await transcribe_one(recorder_under_test, request)
    assert captured["initial_prompt"] == "SESSION OVERRIDE"
    assert captured["hotwords"] == "Patricia"


async def test_transcribe_one_falls_back_to_global_when_meta_empty(
    recorder_under_test, install_stub_transcriber
):
    captured: dict[str, str | None] = {}

    class _Spy(TranscriberStub):
        def transcribe(self, path, *, initial_prompt=None, hotwords=None, source_lang=None, target_lang=None):  # noqa: ARG002
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
        target_lang=None,
    )
    await transcribe_one(recorder_under_test, request)
    assert captured["initial_prompt"] == "GLOBAL DEFAULT"
    assert captured["hotwords"] == "Acme"


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
        target_lang=None,
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
        target_lang=None,
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
        target_lang=None,
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
        target_lang=None,
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
    install_stub_transcriber(TranscriberStub(backend="fake-be", model="fake-m", text="merged"))
    released: list = []
    monkeypatch.setattr("tapscribe.batch_transcribe.release_transcriber", released.append)
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
        target_lang=None,
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
        target_lang=None,
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
        target_lang=None,
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
        target_lang=None,
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
        target_lang=None,
    )
    await transcribe_session(recorder_under_test, request)

    assert len(seen_threads) == len(SESSION_WAVS), seen_threads
    # Every clip transcribed on the SAME thread, and that thread is the
    # dedicated model worker — never a default-pool thread.
    assert len(set(seen_threads)) == 1, f"model scattered across threads: {seen_threads}"
    assert seen_threads[0].startswith(MODEL_THREAD_PREFIX), seen_threads[0]


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
# Candidate-language resolution + apply (ADR-0009). The operator declares a
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
        target_lang=None,
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


async def test_manual_single_wav_transcribe_ignores_candidate_languages(
    recorder_under_test, install_stub_transcriber
):
    """ADR-0009 scope: the candidate-language policy applies to the batch SESSION
    path only. A manual single-WAV transcribe stays unchanged — even with a
    singleton global default set, the transcriber is driven with the request's
    own source_lang (None here), not the meeting's candidate set."""
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
            target_lang=None,
        ),
    )
    assert stub.seen_source_lang == [None]


async def test_multi_candidate_set_constrains_detection_to_the_set(
    recorder_under_test, install_stub_transcriber
):
    """A multi-language meeting defers to a per-region constrained auto-detect:
    the cache loop asks the adapter to snap the language to the candidate set,
    and drives transcribe with that winner — never a language outside the set
    (no drift to e.g. sv)."""
    from tapscribe.text import write_languages

    write_languages("da, no")
    stub = _DetectorStub(winner="no")
    await _run_session(recorder_under_test, stub, install_stub_transcriber)
    # The adapter was asked to choose WITHIN exactly the declared set…
    assert stub.seen_candidates == [("da", "no")]
    # …and its in-set winner became the pin handed to transcribe.
    assert stub.seen_source_lang == ["no"]


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
    {da, no} → {da, no, en} re-runs and re-pins, instead of serving the stale
    {da, no} pick. This is the "fix a mis-detection by adding the language"
    workflow (ADR-0009) — it would silently no-op if the cache served the old
    pick on a non-force re-run."""
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

    write_languages("da, no")  # global default {da, no} → last = "no"
    async with recorder_under_test.jobs.run("s", kind="transcribe", total=1) as handle:
        await transcribe_session_locked(_session_request(), selection=selection, job=handle)
    assert stub.seen_source_lang == ["no"]

    # Widen the meeting to {da, no, en}; a NON-force re-run must re-detect to "en".
    write_session_meta("s", {"languages": ["da", "no", "en"]})
    async with recorder_under_test.jobs.run("s", kind="transcribe", total=1) as handle:
        await transcribe_session_locked(_session_request(), selection=selection, job=handle)
    assert stub.seen_source_lang == ["no", "en"], (
        "widening the candidate set must re-detect on a non-force re-run, not serve the stale pick"
    )
