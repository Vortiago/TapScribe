"""Direct tests for tapscribe.batch_transcribe.

The deepening behind this module's seam is that the orchestration
(load_transcriber + prompt/hotwords resolution + cached_transcribe loop
+ JobTracker + merge writes) is testable WITHOUT an HTTP TestClient or
a real Transcriber. Tests here call `transcribe_one` / `transcribe_session`
straight against a tmpdir-rooted Recorder and a TranscriberStub.
"""

from __future__ import annotations

import json
import wave
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest
from conftest import TranscriberStub  # type: ignore[import-not-found]

from tapscribe import config as _config
from tapscribe.batch_transcribe import (
    BatchOneRequest,
    BatchSessionRequest,
    BatchTranscribeError,
    InvalidRange,
    NoUsableWavs,
    SessionBusy,
    WavTooQuiet,
    WavUnreadable,
    transcribe_one,
    transcribe_session,
)
from tapscribe.live import LiveConfig
from tapscribe.recorder import JobState, Recorder

# ---------------------------------------------------------------------------
# WAV helpers
# ---------------------------------------------------------------------------


def _seed_wav(path: Path, *, amplitude: int = 8000, seconds: float = 1.0) -> Path:
    """Write a small audible-tone WAV. Default amplitude is comfortably
    above SILENT_RMS_DBFS_FLOOR so the silence pre-check passes."""
    n = int(16000 * seconds)
    samples = np.tile(np.array([amplitude, -amplitude], dtype=np.int16), n // 2 + 1)[:n]
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(samples.tobytes())
    return path


def _seed_silent_wav(path: Path) -> Path:
    """All-zeros PCM → RMS is -inf dBFS, deep below SILENT_RMS_DBFS_FLOOR."""
    return _seed_wav(path, amplitude=0)


def _seed_session(root: Path, name: str, wavs: list[str]) -> Path:
    sd = root / name
    sd.mkdir(parents=True)
    for w in wavs:
        _seed_wav(sd / w)
    return sd


# ---------------------------------------------------------------------------
# Recorder fixture — tmpdir, no auth, no live spawn
# ---------------------------------------------------------------------------


@pytest.fixture
def recorder_under_test(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Recorder:
    monkeypatch.setattr(_config, "AUTH_ENABLED", False)
    monkeypatch.setattr(_config, "AUTO_START_LIVE", False)
    monkeypatch.setattr(_config, "RECORDINGS_DIR", tmp_path / "recordings")
    cfg = tmp_path / "config"
    cfg.mkdir()
    monkeypatch.setattr(_config, "CONFIG_DIR", cfg)
    monkeypatch.setattr(_config, "PROMPT_FILE", cfg / "prompt.txt")
    monkeypatch.setattr(_config, "LIVE_PROMPT_FILE", cfg / "live-prompt.txt")
    monkeypatch.setattr(_config, "HOTWORDS_FILE", cfg / "hotwords.txt")
    monkeypatch.setattr(_config, "HALLUCINATIONS_FILE", cfg / "hallucinations.txt")
    (tmp_path / "recordings").mkdir()
    return Recorder(
        recordings_dir=tmp_path / "recordings",
        config_dir=cfg,
        live_config=LiveConfig(model="tiny.en", language="en", host="localhost", port=8000),
        use_mlx=False,
        auth_password_file=tmp_path / ".auth-password",
    )


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
    _seed_session(recorder_under_test.recordings_dir, "s", [WAV_NAME])

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
    _seed_silent_wav(sd / WAV_NAME)

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
    _seed_session(recorder_under_test.recordings_dir, "s", [WAV_NAME])
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
    _seed_session(recorder_under_test.recordings_dir, "s", [WAV_NAME])
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
    sd = _seed_session(recorder_under_test.recordings_dir, "s", SESSION_WAVS)

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
    _seed_session(recorder_under_test.recordings_dir, "s", SESSION_WAVS)

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
    _seed_session(recorder_under_test.recordings_dir, "s", SESSION_WAVS)

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
    _seed_session(recorder_under_test.recordings_dir, "s", SESSION_WAVS)
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
    _seed_session(recorder_under_test.recordings_dir, "s", SESSION_WAVS)

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
    _seed_session(recorder_under_test.recordings_dir, "s", SESSION_WAVS)

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


# ---------------------------------------------------------------------------
# Exception hierarchy — every domain error inherits the base so callers
# that don't care to discriminate (e.g. a CLI batch wanting one catch
# for "anything from this module went wrong") can write `except
# BatchTranscribeError`.
# ---------------------------------------------------------------------------


def test_every_domain_error_inherits_base():
    for cls in (WavUnreadable, WavTooQuiet, SessionBusy, NoUsableWavs, InvalidRange):
        assert issubclass(cls, BatchTranscribeError)
