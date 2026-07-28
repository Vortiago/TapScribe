"""RED contract for #101 — live transcription is OFF by default at boot.

A fresh Recorder boot must spawn NO WhisperLiveKit child and load no live
model, so memory is only spent when an operator actually wants captions. The
lifespan already gates the spawn on `config.AUTO_START_LIVE`
(`tapscribe/lifespan.py`) — the
change is that this flag DEFAULTS to off, and a fresh boot with no flags no
longer auto-starts the channel.

This pins the SOUND, in-process-assertable harm: the boot spawn decision.

  * the default is off:            config.AUTO_START_LIVE is False
  * a default boot spawns nothing: the lifespan does NOT call live.start()
  * the capability is intact:      an explicit opt-in (AUTO_START_LIVE True,
                                   which the new `--auto-live` flag sets) still
                                   spawns — so the flip changes the DEFAULT, it
                                   does not delete auto-start.

The CLI-flag taxonomy — the new `--auto-live` opt-in and the OLD `--no-auto-live`
retained as a deprecated NO-OP (accepted so existing launch scripts don't break)
— is pinned in `tests/test_cli_flags.py`: `__main__` now exposes a `build_parser()`
seam, so a fast in-process test drives the real argparse layer (that `--auto-live`
parses to True — main() then assigns it to config.AUTO_START_LIVE — and that the
deprecated `--no-auto-live` still PARSES for the out-of-gate e2e / C# consumers).
The operator-doc / start-script help text
stays out-of-gate, verified by inspection / the /code-review pass. Graceful
degradation (/tap records with the channel down) is already covered by the tap
endpoint suite.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from conftest import (
    repoint_config_files,  # type: ignore[import-not-found]  # noqa: E402  # tests/ is on sys.path
)
from fastapi.testclient import TestClient

from tapscribe import config as _config
from tapscribe.app import app, get_recorder
from tapscribe.live import LiveConfig
from tapscribe.recorder import Recorder


def _build_recorder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Recorder:
    """A Recorder rooted at tmp. Note: does NOT touch config.AUTO_START_LIVE —
    these tests read/drive that flag deliberately."""
    monkeypatch.setattr(_config, "AUTH_ENABLED", False)
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


def _spy_channel(recorder: Recorder, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Intercept the live channel's start/stop so the lifespan never spawns a
    real whisperlivekit-server. Returns a call log we assert on."""
    calls: list[str] = []
    monkeypatch.setattr(recorder.live, "start", lambda **kw: (calls.append("start"), (True, ""))[1])
    monkeypatch.setattr(recorder.live, "stop", lambda **kw: (True, "not running"))
    return calls


@pytest.fixture
def recorder_under_test(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Recorder:
    return _build_recorder(tmp_path, monkeypatch)


def _boot(recorder: Recorder) -> Iterator[TestClient]:
    app.dependency_overrides[get_recorder] = lambda: recorder
    app.state.recorder = recorder
    with TestClient(app) as c:  # entering the context runs the lifespan
        yield c
    app.dependency_overrides.clear()


def test_autostart_default_is_off() -> None:
    """The flip: the module-level default is OFF, so a boot that sets no flag
    leaves the live channel down."""
    assert _config.AUTO_START_LIVE is False


def test_default_boot_spawns_no_live_child(
    recorder_under_test: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """HARM: with the DEFAULT config (no AUTO_START_LIVE override), the lifespan
    must NOT start the live channel — no WhisperLiveKit child, no live model."""
    calls = _spy_channel(recorder_under_test, monkeypatch)
    for _ in _boot(recorder_under_test):
        pass
    assert calls == []  # nothing spawned on a default boot


def test_explicit_optin_still_spawns(recorder_under_test: Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    """Capability intact: an explicit opt-in (what the new `--auto-live` flag
    sets) still auto-starts at boot — the flip changes the DEFAULT, it does not
    remove auto-start. A build that 'fixes' the harm by deleting the lifespan
    branch fails here."""
    monkeypatch.setattr(_config, "AUTO_START_LIVE", True)
    calls = _spy_channel(recorder_under_test, monkeypatch)
    for _ in _boot(recorder_under_test):
        pass
    assert calls == ["start"]
