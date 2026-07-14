"""Shared pytest fixtures.

The package is imported only after we redirect the recording / config dirs
to a per-session tmpdir — otherwise importing tapscribe.config from a CI
runner would create `recordings/` next to the repo and pollute the
worktree.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import shlex
import sys
import threading
import tomllib
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
import websockets

# Make the in-tree package importable when pytest is invoked from the repo
# root without an editable install (CI's most common shape).
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


# ---------------------------------------------------------------------------
# Silero stub for strip-silence tests
# ---------------------------------------------------------------------------
#
# tapscribe.strip_silence.detect_speech_silero loads the real silero-vad
# model, which pulls torch into the import graph — too heavy for CI. The
# fixture below monkeypatches it with a deterministic RMS-windowed detector
# that produces the same region boundaries silero would on the synthetic
# square-wave-and-silence fixtures the tests use.
#
# Keep this strictly a test-side helper: production must always run the real
# silero (any TapScribe install that has the live channel already does).


def _stub_detect_speech_silero(samples_int16, min_silence_ms: int, pad_ms: int):
    """Deterministic stand-in for the real silero detector — same signature.

    Computes RMS over 30 ms windows, calls anything > -45 dBFS "speech",
    merges gaps shorter than min_silence_ms, then pads each region by
    pad_ms. Matches silero's region semantics closely enough for the
    synthetic fixtures (square-wave bursts above the floor, zeros between).
    """
    import numpy as np

    from tapscribe.audio import RECORDER_SAMPLE_RATE as SAMPLE_RATE

    window_ms = 30
    window_samples = SAMPLE_RATE * window_ms // 1000
    audio = samples_int16.astype(np.float32) / 32768.0
    n_windows = len(audio) // window_samples
    if n_windows == 0:
        return []
    audio = audio[: n_windows * window_samples].reshape(n_windows, window_samples)
    rms = np.sqrt((audio**2).mean(axis=1) + 1e-12)
    db = 20.0 * np.log10(rms + 1e-12)
    is_speech = db > -45.0

    raw: list[tuple[int, int]] = []
    in_speech = False
    start = 0
    for i, v in enumerate(is_speech):
        if v and not in_speech:
            start = i
            in_speech = True
        elif not v and in_speech:
            raw.append((start, i))
            in_speech = False
    if in_speech:
        raw.append((start, n_windows))

    min_silence_windows = max(1, min_silence_ms // window_ms)
    merged: list[tuple[int, int]] = []
    for s, e in raw:
        if merged and s - merged[-1][1] < min_silence_windows:
            merged[-1] = (merged[-1][0], e)
        else:
            merged.append((s, e))

    pad_samples = (SAMPLE_RATE * pad_ms) // 1000
    total = len(samples_int16)
    out: list[tuple[int, int]] = []
    for s, e in merged:
        s2 = max(0, s * window_samples - pad_samples)
        e2 = min(total, e * window_samples + pad_samples)
        if out and s2 <= out[-1][1]:
            out[-1] = (out[-1][0], e2)
        else:
            out.append((s2, e2))
    return out


@pytest.fixture(autouse=True)
def _stub_silero(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch):
    """Replace strip_silence.detect_speech_silero with the cheap RMS-based
    stand-in on every test by default. Tests that need the real silero
    (or want to assert what happens when it's missing) opt out with
    `@pytest.mark.real_silero`.

    Autouse + opt-out is robust against future strip-silence tests being
    added and silently pulling torch into CI; the previous filename
    allowlist required maintenance every time a new test file landed."""
    if request.node.get_closest_marker("real_silero") is not None:
        return
    from tapscribe import strip_silence

    monkeypatch.setattr(strip_silence, "detect_speech_silero", _stub_detect_speech_silero)


def repoint_config_files(monkeypatch: pytest.MonkeyPatch, cfg: Path) -> None:
    """Point `tapscribe.config.CONFIG_DIR` AND every config-file constant
    under it at `cfg`. The per-file `*_FILE` constants are computed from
    CONFIG_DIR at import time, so repointing the dir alone leaves them aimed
    at the repo's `config/` — a test writing global config would then pollute
    the working tree and leak state into the next run. ONE shared helper
    (used by every recorder fixture, unit and e2e) instead of per-fixture
    copies of the list; introspecting for `*_FILE` Paths under CONFIG_DIR
    makes a new config file self-registering — zero fixture edits.

    Deliberately scoped to CONFIG_DIR children: BASE_DIR-rooted `*_FILE`
    constants (auth password, tap token, TLS material) keep their own
    per-fixture handling."""
    from tapscribe import config

    original_dir = config.CONFIG_DIR
    monkeypatch.setattr(config, "CONFIG_DIR", cfg)
    for name in dir(config):
        val = getattr(config, name)
        if name.endswith("_FILE") and isinstance(val, Path) and val.parent == original_dir:
            monkeypatch.setattr(config, name, cfg / val.name)


@pytest.fixture
def tmp_config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point the package's CONFIG_DIR + every config file at a tmpdir.

    Tests that want to exercise prompt/hotwords/hallucinations reads write
    files into this dir directly.
    """
    cfg = tmp_path / "config"
    cfg.mkdir()
    repoint_config_files(monkeypatch, cfg)
    return cfg


# ---------------------------------------------------------------------------
# Fake whisperlivekit-server (used by /tap and TapFanOut relay tests)
# ---------------------------------------------------------------------------


class FakeWlkThread:
    """Minimal whisperlivekit-server stand-in. Runs its own event loop in
    a daemon thread so synchronous and asyncio callers alike can drive a
    relay against it.

    Shutdown contract: `stop()` signals an in-loop `asyncio.Event` rather
    than calling `loop.stop()`. The serve coroutine then awaits the event,
    closes the websocket server + open connections, and returns — at which
    point `run_until_complete` returns naturally and the loop drains its
    own pending tasks. Stopping via `loop.stop()` mid-`await` (the prior
    shape) left the websockets server's close path scheduling tasks onto
    an already-stopped loop on Python 3.13, which surfaced as
    PytestUnraisableExceptionWarning noise on every test that touched
    /tap or the relay.

    Lives in conftest.py because both the end-to-end /tap tests and the
    TapFanOut unit tests need to construct a real WlKRelay against
    something that speaks the WlK wire shape."""

    def __init__(self) -> None:
        # `received` is the flat aggregate across all connections — kept
        # so existing single-conn callers (`b"".join(fake_wlk.received)`)
        # keep working unchanged. Per-connection state lives in the
        # parallel lists `connections` / `received_by_connection` /
        # `_lines_acc_by_connection`, indexed in connect order.
        self.received: list[bytes] = []
        self.connections: list = []
        self.received_by_connection: list[list[bytes]] = []
        self._lines_acc_by_connection: list[list[dict]] = []
        # Assigned by `_serve()` when the kernel binds port 0 — a
        # pick-then-bind via `_free_port()` here leaves a window where
        # another process (or a parallel test run) grabs the picked port
        # before `_serve()` binds it, failing `start()` with "address
        # already in use". Every consumer reads `.port` after `start()`
        # returns, which waits on `_ready` — set only after the bind.
        self._port = 0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._server = None
        self._stop_event: asyncio.Event | None = None
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    @property
    def port(self) -> int:
        return self._port

    def start(self) -> None:
        self._thread.start()
        assert self._ready.wait(timeout=2.0), "fake WlK didn't start"

    def stop(self) -> None:
        # Idempotent: tests that simulate a WlK restart call stop() to
        # tear down the original fake, then the fixture's teardown
        # calls it again on exit. Without this guard the second call
        # crashes with "Event loop is closed" because serve() already
        # returned and the loop is shut down.
        if not self._thread.is_alive():
            return
        loop, stop_event = self._loop, self._stop_event
        if loop is not None and stop_event is not None:
            try:
                loop.call_soon_threadsafe(stop_event.set)
            except RuntimeError:
                # Loop closed between the is_alive check and here —
                # extremely narrow race, but the thread is on its way
                # out either way.
                pass
        self._thread.join(timeout=2.0)

    def terminate(self) -> None:
        """Simulate a WhisperLiveKit child crash: yank the server out
        from under any open relay without a graceful drain.

        `stop()` triggers the stop_event and lets the serve coroutine
        close connections cooperatively. `terminate()` short-circuits
        that: it forces sockets shut from the WlK loop's thread before
        signalling stop, so any open relay sees an abrupt close rather
        than a polite goodbye. Crash/restart tests need that shape.
        Idempotent and safe to follow with a teardown `stop()`."""
        if not self._thread.is_alive():
            return
        loop = self._loop

        async def _kill():
            # Force-close every open connection without awaiting their
            # close handshake, then close the listening server.
            for c in list(self.connections):
                with contextlib.suppress(Exception):
                    transport = getattr(c, "transport", None)
                    if transport is not None:
                        transport.abort()
                    else:
                        await c.close(code=1011)
            if self._server is not None:
                self._server.close()
            if self._stop_event is not None:
                self._stop_event.set()

        if loop is not None:
            try:
                fut = asyncio.run_coroutine_threadsafe(_kill(), loop)
                with contextlib.suppress(Exception):
                    fut.result(timeout=2.0)
            except RuntimeError:
                # Loop already closed; nothing to do.
                pass
        self._thread.join(timeout=2.0)

    def push_committed(self, text: str, *, connection_index: int | None = None) -> None:
        """Schedule a settled-line broadcast on the WlK loop's thread.

        Mirrors the real WlK wire format: each call appends a new line to
        the cumulative list and broadcasts the FrontData-shaped snapshot.
        See whisperlivekit/timed_objects.py FrontData.to_dict for the
        source of truth on the keys.

        With `connection_index=None` (default) the line is broadcast to
        every connected relay, with each relay's own cumulative line
        list advanced independently — the legacy single-conn behaviour
        is the N=1 case of this. Pass an int to push to a single
        specific connection (in connect order) for tests that need to
        diverge the two relays.

        Waits for the broadcast to complete on the WlK loop so callers can
        assert on relay-side state right after — no implicit sleep needed."""
        if self._loop is None:
            return

        if connection_index is None:
            targets = list(range(len(self.connections)))
        else:
            targets = [connection_index]

        async def _push():
            for i in targets:
                if i < 0 or i >= len(self.connections):
                    continue
                acc = self._lines_acc_by_connection[i]
                idx = len(acc)
                acc.append(
                    {
                        "text": text,
                        "speaker": 1,
                        "start": float(idx),
                        "end": float(idx) + 1.0,
                    }
                )
                msg = json.dumps(
                    {
                        "status": "active_transcription",
                        "lines": list(acc),
                        "buffer_transcription": "",
                        "buffer_diarization": "",
                        "buffer_translation": "",
                        "remaining_time_transcription": 0,
                        "remaining_time_diarization": 0,
                    }
                )
                with contextlib.suppress(Exception):
                    await self.connections[i].send(msg)

        fut = asyncio.run_coroutine_threadsafe(_push(), self._loop)
        with contextlib.suppress(Exception):
            fut.result(timeout=2.0)

    def push_buffer(
        self,
        text: str,
        *,
        connection_index: int | None = None,
    ) -> None:
        """Push a FrontData-shaped snapshot whose `buffer_transcription`
        is `text` and whose `lines` is the current cumulative list (no
        new commit). Mirrors the wire-level moment where WlK reports
        in-flight hypothesis without yet promoting it to a settled
        line. Used by gate / buffer-display tests."""
        if self._loop is None:
            return

        if connection_index is None:
            targets = list(range(len(self.connections)))
        else:
            targets = [connection_index]

        async def _push():
            for i in targets:
                if i < 0 or i >= len(self.connections):
                    continue
                acc = self._lines_acc_by_connection[i]
                msg = json.dumps(
                    {
                        "status": "active_transcription",
                        "lines": list(acc),
                        "buffer_transcription": text,
                        "buffer_diarization": "",
                        "buffer_translation": "",
                        "remaining_time_transcription": 0,
                        "remaining_time_diarization": 0,
                    }
                )
                with contextlib.suppress(Exception):
                    await self.connections[i].send(msg)

        fut = asyncio.run_coroutine_threadsafe(_push(), self._loop)
        with contextlib.suppress(Exception):
            fut.result(timeout=2.0)

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._serve())
        finally:
            loop.close()

    async def _serve(self) -> None:
        self._server = await websockets.serve(self._handler, "localhost", 0)
        self._port = self._server.sockets[0].getsockname()[1]
        self._stop_event = asyncio.Event()
        self._ready.set()
        try:
            await self._stop_event.wait()
        finally:
            for c in list(self.connections):
                with contextlib.suppress(Exception):
                    await c.close()
            self._server.close()
            await self._server.wait_closed()

    async def _handler(self, ws) -> None:
        self.connections.append(ws)
        per_conn_received: list[bytes] = []
        self.received_by_connection.append(per_conn_received)
        self._lines_acc_by_connection.append([])
        try:
            async for msg in ws:
                if isinstance(msg, bytes):
                    self.received.append(msg)
                    per_conn_received.append(msg)
        finally:
            if ws in self.connections:
                idx = self.connections.index(ws)
                # Keep parallel lists aligned: drop this connection's
                # per-conn state alongside its slot in `connections`.
                self.connections.pop(idx)
                self.received_by_connection.pop(idx)
                self._lines_acc_by_connection.pop(idx)


@pytest.fixture
def fake_wlk() -> Iterator[FakeWlkThread]:
    wlk = FakeWlkThread()
    wlk.start()
    try:
        yield wlk
    finally:
        wlk.stop()


# ---------------------------------------------------------------------------
# Lightweight transcriber stub — shared across route + cache tests
# ---------------------------------------------------------------------------


class TranscriberStub:
    """A minimal Transcriber-protocol stub. Returns one canned segment per
    `transcribe()` call. Parameterise `backend`, `model`, and `text` to
    distinguish multiple stubs in the same test."""

    name = "fake"
    device = "test-device"

    def __init__(
        self,
        *,
        backend: str = "fake-backend",
        model: str = "fake-model",
        text: str | None = None,
    ) -> None:
        self.backend = backend
        self.model_name = model
        self._text = text if text is not None else f"text from {backend} {model}"
        self.calls: list[Path] = []
        # Every source_lang the stub was driven with, in call order — lets a test
        # assert the candidate-language resolution (ADR-0010) reached the model.
        self.seen_source_lang: list[str | None] = []

    def transcribe(self, path, *, initial_prompt=None, hotwords=None, source_lang=None):  # noqa: ARG002
        from tapscribe.transcribers.base import TranscriptionSegment, build_transcription_result

        self.calls.append(path)
        self.seen_source_lang.append(source_lang)
        # Echo source_lang into the result like the real adapters do, so the
        # cache's source_language match key behaves realistically.
        return build_transcription_result(
            self,
            text=self._text,
            segments=(TranscriptionSegment(start=0.0, end=1.0, text=self._text),),
            duration=1.0,
            language=source_lang or "en",
            language_probability=1.0,
            initial_prompt=initial_prompt,
            hotwords=hotwords,
            source_lang=source_lang,
        )


# ---------------------------------------------------------------------------
# Summarizer Command-source test helpers (shared by the summarizer adapter,
# batch-summarize orchestrator, and /summarize route tests)
# ---------------------------------------------------------------------------


def py_cmd(script: str) -> str:
    """A cross-platform Command-source template that runs `script` under the
    current interpreter. The summarizer tests use this instead of `cat`/`echo`
    (not PATH executables on Windows) so the suite is identical on the whole
    Linux/macOS/Windows CI matrix. `shlex.quote` + the adapter's `shlex.split`
    round-trip keeps a Windows `C:\\...\\python.exe` path intact (it's single-
    quoted, so the backslashes survive)."""
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"


def make_api_post_stub(rec: list[tuple]) -> Callable[[str, dict, dict], dict]:
    """An `ApiSummarizer` `post_fn` stub: records each `(url, headers, body)`
    call in `rec` and returns a canned chat-completions response. Shared by
    the direct `ApiSummarizer` request-shape tests and the local/api
    no-drift regression test — both need to capture what `ApiSummarizer`
    actually sent without a real network call."""

    def stub(url: str, headers: dict[str, str], body: dict) -> dict:
        rec.append((url, headers, body))
        return {"choices": [{"message": {"content": "SUMMARY TEXT"}}]}

    return stub


def seed_merged_transcript(
    recordings_dir: Path, session: str, *, plain_text: str = "Alice: hi. We shipped."
) -> Path:
    """Write a minimal session-transcript.json into `<recordings_dir>/<session>/`
    so the session has a merged transcript to summarize — the slim shape the
    summarize orchestrator + `read_session_transcript` consume. Returns the
    session dir."""
    sd = recordings_dir / session
    sd.mkdir(parents=True, exist_ok=True)
    (sd / "session-transcript.json").write_text(
        json.dumps(
            {
                "session": session,
                "model": "test",
                "transcribed_at": "2026-01-01T00:00:00+00:00",
                "speakers": ["Alice"],
                "segments": [],
                "plain_text": plain_text,
            }
        ),
        encoding="utf-8",
    )
    return sd


# ---------------------------------------------------------------------------
# Recorder fixture — tmpdir, no auth, no live spawn
# ---------------------------------------------------------------------------


@pytest.fixture
def recorder_under_test(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A tmpdir-rooted Recorder for direct batch-orchestrator tests
    (test_batch_transcribe, test_batch_strip) — no HTTP, no auth, no live
    spawn. tapscribe imports happen inside the fixture so config-dir
    redirection lands before module import (see module docstring)."""
    from tapscribe import config as _config
    from tapscribe.live import LiveConfig
    from tapscribe.recorder import Recorder

    monkeypatch.setattr(_config, "AUTH_ENABLED", False)
    monkeypatch.setattr(_config, "AUTO_START_LIVE", False)
    monkeypatch.setattr(_config, "RECORDINGS_DIR", tmp_path / "recordings")
    cfg = tmp_path / "config"
    cfg.mkdir()
    repoint_config_files(monkeypatch, cfg)
    (tmp_path / "recordings").mkdir()
    return Recorder(
        recordings_dir=tmp_path / "recordings",
        config_dir=cfg,
        live_config=LiveConfig(model="tiny.en", language="en", host="localhost", port=8000),
        use_mlx=False,
        auth_password_file=tmp_path / ".auth-password",
    )


# ---------------------------------------------------------------------------
# Tap-path test helpers — shared by test_tap_fan_out / test_tap_endpoint /
# test_tap_fan_out_chaos / test_concurrency_races (and, via
# tests/e2e/conftest, the e2e running-recorder fixtures)
# ---------------------------------------------------------------------------


class FakeAliveProc:
    """LiveChannel.running() returns True iff `_proc.poll() is None`, so any
    object whose poll() returns None marks the channel "alive". Inject into
    `recorder.live._proc` to open the tap relay path without spawning a real
    whisperlivekit child."""

    def poll(self):
        return None


async def wait_for(predicate: Callable[[], object], *, timeout: float = 2.0, interval: float = 0.005) -> None:
    """Event-style wait for state mutated outside the test coroutine (a fake
    WlK on its own thread loop; a relay reconnect in a background task).
    Tight inner poll wrapped in `asyncio.wait_for` so a stuck condition
    fails loudly instead of hanging until the suite timeout."""

    async def _poll() -> None:
        while not predicate():
            await asyncio.sleep(interval)

    await asyncio.wait_for(_poll(), timeout=timeout)


def build_tap_recorder(
    tmp_path: Path,
    *,
    port: int = 9999,
    gate_kind: str | None = None,
    live_running: bool = False,
):
    """One tmpdir-rooted Recorder construction for the tap-path test files,
    so the block isn't rebuilt per file. `gate_kind=None` keeps LiveConfig's
    default (the TapScribe gate); relay-focused tests pin "backend" because
    they feed near-silent synthetic PCM that real Silero would block.
    `live_running=True` injects a FakeAliveProc so TapFanOut opens its
    relay. tapscribe imports happen inside the helper so config-dir
    redirection lands before module import (see module docstring)."""
    import dataclasses

    from tapscribe.live import LiveConfig
    from tapscribe.recorder import Recorder

    recordings = tmp_path / "recordings"
    config_dir = tmp_path / "config"
    recordings.mkdir(parents=True, exist_ok=True)
    config_dir.mkdir(parents=True, exist_ok=True)
    live_config = LiveConfig(model="tiny.en", language="en", host="localhost", port=port)
    if gate_kind is not None:
        live_config = dataclasses.replace(live_config, gate_kind=gate_kind)
    recorder = Recorder(
        recordings_dir=recordings,
        config_dir=config_dir,
        live_config=live_config,
        use_mlx=False,
        auth_password_file=tmp_path / ".auth-password",
    )
    if live_running:
        recorder.live._proc = FakeAliveProc()
    return recorder


# ── Setup-feature test helpers (shared so the probe + fake-subprocess machinery
#    isn't copy-pasted across test_routes / test_setup_install / e2e conftest) ──


def all_probe_modules() -> frozenset[str]:
    """Every backend probe module the registry declares — for tests that
    simulate a fully-installed machine via
    `runtime_probe.set_installed_modules_for_testing(all_probe_modules())`."""
    from tapscribe.transcribers.catalog import REGISTRY

    return frozenset(b.probe_module for e in REGISTRY.entries() for b in e.backends if b.probe_module)


def fake_install_spawn(lines: list[bytes], returncode: int, *, on_wait=None):
    """A fake `setup_install._create_subprocess`: returns a coroutine yielding a
    process whose `.stdout` async-iterates `lines` and whose `wait()` returns
    `returncode`. Used by the install-streaming tests. `on_wait`, if given, is
    called inside `wait()` before the returncode is set — e.g. to simulate the
    install's effect (a backend becoming importable)."""

    async def _stdout():
        for line in lines:
            yield line

    class _Proc:
        def __init__(self) -> None:
            self.stdout = _stdout()
            self.returncode: int | None = None

        async def wait(self) -> int:
            if on_wait is not None:
                on_wait()
            self.returncode = returncode
            return returncode

    async def spawn(_argv):
        return _Proc()

    return spawn


# ---------------------------------------------------------------------------
# pyproject extras lookup (shared by test_install_picker.py + test_install_matrix.py)
# ---------------------------------------------------------------------------


def atomic_extras(extra_name: str) -> list[str]:
    """Pull one `[project.optional-dependencies]` entry out of pyproject,
    asserting it exists. Shared by the install-picker's per-backend pip
    resolution tests and install-matrix.yml's family-axis meta-test — both
    need "does this extra exist in pyproject.toml", so it lives here rather
    than in either test file alone."""
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    extras = data["project"]["optional-dependencies"]
    assert extra_name in extras, f"no `{extra_name}` extra in pyproject.toml"
    return list(extras[extra_name])
