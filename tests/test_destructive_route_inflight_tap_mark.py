"""RED contract for #405 item 2: the destructive preflight must see the in-flight mark.

`refuse_current_or_busy` (routes/guards.py) is the pre-flight for all four
destructive whole-folder routes: session delete, session-audio delete, absorb,
WAV delete. Its only in-flight-tap signal is `recorder.streams.snapshot()`, and
`streams.register` runs about 40 lines AFTER `open_recorder_wav` in
`TapFanOut._open`. #257 added a mark taken BEFORE the mkdir, so through the whole
mkdir to WAV-open window a session has an in-flight mark and no `ActiveStream`,
and the preflight waves the delete through.

THE HARM, at the harm layer. Every test below drives the real HTTP route and
asserts the BYTES survive, not merely that a 409 came back. A guard that returns
409 and deletes anyway, or one wired into only one of the four routes, passes a
status-code-only contract. Base deletes the directory.

WHY THIS IS ONLY A NARROWING, AND WHY THAT IS STILL THE ISSUE'S ASK. The routes
finish in `await asyncio.to_thread(shutil.rmtree, ...)`, which runs off the loop
thread and can therefore interleave inside `_open`'s await-free segment even with
a correct preflight: a tap that takes its mark one instruction after the guard
read still loses its audio. No check-then-act preflight can close that; #405 says
so and defers the decision, which is tracked as #408. This file pins the
narrowing #405 actually asks for and does not pretend to pin exclusion.

SCOPE IS `current`, NOT EVERY SESSION. The existing active-tap branch is scoped
to `current` (the session actually being emptied) rather than all of `sessions`,
because absorb only moves source's files and never rewrites target's, so a tap on
a live TARGET is safe. The mark check must use that SAME scope.
`test_absorb_allows_a_marked_target` fails a build that refuses on any marked
session in the argument list, and `test_a_mark_on_an_unrelated_session_does_not_block`
fails one that refuses whenever the registry is non-empty.

Interface note: mark and release through `session_maintenance`'s public
`mark_session_in_flight` / `release_session_mark`, the same seam #257's contract
uses. That keeps this file agnostic about where #405 moves the registry (pinned
separately in `test_tap_registry_layering.py`).
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime
from pathlib import Path

import anyio.from_thread
import pytest
from conftest import repoint_config_files  # type: ignore[import-not-found]
from fastapi.testclient import TestClient
from wav_builders import seed_session  # type: ignore[import-not-found]

from tapscribe import config as _config
from tapscribe import session_maintenance
from tapscribe.app import app, get_recorder
from tapscribe.live import LiveConfig
from tapscribe.recorder import ActiveStream, Recorder, SessionBusy

_WAV = "20260101T000000Z__alice__abc.wav"
_PKG = Path(__file__).resolve().parent.parent / "tapscribe"


@pytest.fixture
def recorder_under_test(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Recorder:
    """Mirrors test_session_active_tap_guard.py's fixture of the same name:
    tmp_path-rooted, auth and auto-start off so the lifespan does not try to
    spawn whisperlivekit-server."""
    monkeypatch.setattr(_config, "AUTH_ENABLED", False)
    monkeypatch.setattr(_config, "AUTO_START_LIVE", False)
    monkeypatch.setattr(_config, "RECORDINGS_DIR", tmp_path / "recordings")
    cfg = tmp_path / "config"
    cfg.mkdir()
    repoint_config_files(monkeypatch, cfg)
    (tmp_path / "recordings").mkdir()

    return Recorder(
        recordings_dir=tmp_path / "recordings",
        config_dir=tmp_path / "config",
        live_config=LiveConfig(model="tiny.en", language="en", host="localhost", port=8000),
        use_mlx=False,
        auth_password_file=tmp_path / ".auth-password",
    )


@pytest.fixture
def client(recorder_under_test):
    app.dependency_overrides[get_recorder] = lambda: recorder_under_test
    app.state.recorder = recorder_under_test
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def _clean_registry():
    """The registry is a process-global dict that nothing resets, and once the
    destructive preflight consults it a mark left behind here makes unrelated
    route tests elsewhere in the suite 409. Clear it both ways.

    Deliberately NOT test_prune_vs_tap_race.py's asserting leak detector: the
    tests below mark sessions by hand rather than driving `TapFanOut._open`, so a
    mark still set at teardown is this file's own setup, not a product defect.
    Release-pairing in the tap's real exit paths is #257's contract to pin.
    """
    session_maintenance._tap_open_sessions.clear()
    yield
    session_maintenance._tap_open_sessions.clear()


def _register_active_tap(recorder: Recorder, session: str, conn_id: str) -> None:
    with anyio.from_thread.start_blocking_portal() as portal:
        portal.call(
            recorder.streams.register,
            ActiveStream(
                conn_id=conn_id,
                identity="alice",
                name="Alice",
                filename=_WAV,
                started_at=datetime.now(UTC),
                session=session,
            ),
        )


# ---------------------------------------------------------------------------
# The harm: all four destructive routes, bytes-level assertions.
# ---------------------------------------------------------------------------


def test_session_delete_refuses_a_marked_session(client, recorder_under_test):
    """The window #257 opened the mark for: marked, no ActiveStream yet. Base
    rmtree's the directory the tap is about to write into."""
    root = recorder_under_test.recordings_dir
    sd = seed_session(root, "detached", [_WAV])
    # No ActiveStream is registered anywhere in this test: the mark is the only signal.
    session_maintenance.mark_session_in_flight("detached")

    r = client.delete("/api/sessions/detached")

    assert r.status_code == 409
    assert sd.is_dir()
    assert (sd / _WAV).is_file()


def test_delete_session_audio_refuses_a_marked_session(client, recorder_under_test):
    root = recorder_under_test.recordings_dir
    sd = seed_session(root, "detached", [_WAV])
    session_maintenance.mark_session_in_flight("detached")

    r = client.delete("/api/sessions/detached/audio")

    assert r.status_code == 409
    assert (sd / _WAV).is_file()


def test_delete_wav_refuses_a_marked_session(client, recorder_under_test):
    root = recorder_under_test.recordings_dir
    sd = seed_session(root, "detached", [_WAV])
    session_maintenance.mark_session_in_flight("detached")

    r = client.delete(f"/api/wav/detached/{_WAV}")

    assert r.status_code == 409
    assert (sd / _WAV).is_file()


def test_absorb_refuses_a_marked_source(client, recorder_under_test):
    root = recorder_under_test.recordings_dir
    src = seed_session(root, "src", [_WAV])
    seed_session(root, "target", [])
    session_maintenance.mark_session_in_flight("src")

    r = client.post("/api/sessions/target/absorb", json={"source": "src"})

    assert r.status_code == 409
    assert src.is_dir()
    assert (src / _WAV).is_file()


async def test_the_guard_itself_refuses_a_marked_session(recorder_under_test):
    """The same refusal at the guard's own boundary, so a build that fixes the
    four routes one by one instead of the shared preflight is visible. Raises
    `SessionBusy`, the domain error the other two busy branches raise, not a bare
    `HTTPException`."""
    session_maintenance.mark_session_in_flight("detached")
    with pytest.raises(SessionBusy):
        from tapscribe.routes.guards import refuse_current_or_busy

        await refuse_current_or_busy(recorder_under_test, "detached", current="detached", action="delete")
    session_maintenance.release_session_mark("detached")


# ---------------------------------------------------------------------------
# Distinguishing guardrails: what must still be ALLOWED.
# ---------------------------------------------------------------------------


def test_a_mark_on_an_unrelated_session_does_not_block(client, recorder_under_test):
    """Fails a build that refuses whenever the registry is non-empty. In
    production a mark is almost always present somewhere, so that build bricks
    every destructive route."""
    root = recorder_under_test.recordings_dir
    sd = seed_session(root, "detached", [_WAV])
    session_maintenance.mark_session_in_flight("some-other-session")

    r = client.delete("/api/sessions/detached")

    assert r.status_code == 200
    assert not sd.exists()


def test_absorb_allows_a_marked_target(client, recorder_under_test):
    """The mark check must reuse the existing `current` scope, not all of
    `sessions`. Absorb's target MAY be the live session and never has its own
    files rewritten, so a tap into the target is safe; only a tap into the
    session being emptied (`current`, the source) is not. Mirrors
    test_absorb_allows_active_tap_on_target_when_target_is_the_live_session."""
    root = recorder_under_test.recordings_dir
    target = recorder_under_test.session_start
    seed_session(root, target, [])
    src = seed_session(root, "src", [_WAV])
    session_maintenance.mark_session_in_flight(target)

    r = client.post(f"/api/sessions/{target}/absorb", json={"source": "src"})

    assert r.status_code == 200
    assert not src.exists()


def test_releasing_the_mark_reopens_the_route(client, recorder_under_test):
    """The guard reads live state, it does not latch. Fails a build that refuses
    forever once a session has ever been marked, which would make any session
    that hosted a tap permanently undeletable."""
    root = recorder_under_test.recordings_dir
    sd = seed_session(root, "detached", [_WAV])
    session_maintenance.mark_session_in_flight("detached")
    assert client.delete("/api/sessions/detached").status_code == 409
    session_maintenance.release_session_mark("detached")

    r = client.delete("/api/sessions/detached")

    assert r.status_code == 200
    assert not sd.exists()


def test_the_existing_active_stream_branch_still_refuses(client, recorder_under_test):
    """Regression rail: the mark is an ADDITIONAL signal. A tap past the WAV open
    has an ActiveStream and (after `_close`) may hold no mark, so the stream
    branch #195 added must survive the edit."""
    root = recorder_under_test.recordings_dir
    sd = seed_session(root, "detached", [_WAV])
    _register_active_tap(recorder_under_test, "detached", "conn-1")

    r = client.delete("/api/sessions/detached")

    assert r.status_code == 409
    assert sd.is_dir()


def test_the_current_session_branch_still_wins(client, recorder_under_test):
    """The current-session refusal is an `HTTPException` with its own message and
    must stay ahead of the busy branches: the live session's directory may not
    exist on disk yet, so ordering is load-bearing."""
    current = recorder_under_test.session_start

    r = client.delete(f"/api/sessions/{current}")

    assert r.status_code == 409
    assert "rotate to a new one first" in r.json()["detail"]


# ---------------------------------------------------------------------------
# Layering: guards may not buy the maintenance module to get the mark.
# ---------------------------------------------------------------------------


def test_the_guard_module_does_not_import_the_maintenance_module():
    """#405's ask is that the preflight can consult the registry "without adding
    a second routes -> session_maintenance edge". The obvious fix,
    `from ..session_maintenance import session_has_open_tap`, is exactly the edge
    the issue rules out; the neutral leaf is what makes the check payable.

    Source-level, not a runtime `sys.modules` check: importing
    `tapscribe.routes.guards` runs `tapscribe.routes.__init__`, which pulls all
    69 route modules including `session_maintenance`, so a runtime assertion here
    would be unsatisfiable regardless of how guards.py is written.
    """
    path = _PKG / "routes" / "guards.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    named: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            named.add(node.module.lstrip("."))
        elif isinstance(node, ast.Import):
            named.update(a.name for a in node.names)
    assert not {n for n in named if n.split(".")[-1] == "session_maintenance"}, (
        "routes/guards.py must reach the in-flight registry through its neutral home, "
        "not by importing session_maintenance (#405)"
    )
