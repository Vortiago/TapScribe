"""Coverage for the `python -m tapscribe` boundary (#236).

`tapscribe/__main__.py` parses ~20 argparse flags and wires them into
`Recorder`/`LiveConfig` construction, the auth/TLS banners, and
`uvicorn.run`. Nothing else in the suite imports or calls `main()` — the
e2e harness boots the FastAPI app in-thread and bypasses this module
entirely (see `tests/e2e/harness.py`) — so a dropped flag or a broken
flag-to-config mapping shipped with every other test green.

Seams under test:
  * `build_parser()` — the real argparse layer (already partially covered
    by `test_cli_flags.py` for `--auto-live`/`--no-auto-live`; this file
    covers argument *validation*, i.e. `choices=`/`type=` constraints).
  * `main()` — the full wiring seam, driven end-to-end with `uvicorn.run`
    monkeypatched to a recording spy (so no server actually binds a
    socket) and every boot-time path (config dir, recordings dir, auth
    secrets, TLS material) repointed at `tmp_path`. `available_backends()`
    is pinned via `set_available_backends_for_testing` so backend
    resolution is deterministic and doesn't probe the real machine.
  * A subprocess smoke test for `python -m tapscribe --help`, per the
    issue's suggestion — cheap and catches "the module doesn't even
    import" class failures the in-process tests can't.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from conftest import (
    repoint_config_files,  # type: ignore[import-not-found]  # noqa: E402  # tests/ is on sys.path
)

from tapscribe import config as _config
from tapscribe.transcribers import catalog


@pytest.fixture
def boot_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Repoint every boot-time path `main()` touches at `tmp_path`, so a
    real `Recorder`/TLS/auth boot never reads or writes the real repo."""
    monkeypatch.setattr(_config, "RECORDINGS_DIR", tmp_path / "recordings")
    cfg = tmp_path / "config"
    cfg.mkdir()
    repoint_config_files(monkeypatch, cfg)
    (tmp_path / "recordings").mkdir()
    monkeypatch.setattr(_config, "AUTH_PASSWORD_FILE", tmp_path / ".auth-password")
    monkeypatch.setattr(_config, "TAP_TOKEN_FILE", tmp_path / ".tap-token")
    monkeypatch.setattr(_config, "TLS_CERT_FILE", tmp_path / ".tapscribe-cert.pem")
    monkeypatch.setattr(_config, "TLS_KEY_FILE", tmp_path / ".tapscribe-key.pem")
    return tmp_path


@pytest.fixture
def fixed_backends(monkeypatch: pytest.MonkeyPatch):
    """Pin `available_backends()` to a deterministic set for the duration
    of the test so backend resolution doesn't depend on what's actually
    importable on the machine running the suite."""
    catalog.set_available_backends_for_testing(frozenset({"cpu"}))
    yield
    catalog.set_available_backends_for_testing(None)


@pytest.fixture
def uvicorn_spy(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Replace `uvicorn.run` with a spy that records its kwargs and
    returns instead of blocking on a real server. `main()`'s only call
    into uvicorn is by keyword (`uvicorn.run(app, host=..., port=...,
    ...)`), so we record kwargs only."""
    calls: list[dict] = []

    def _fake_run(app, **kwargs):
        calls.append(kwargs)

    monkeypatch.setattr("tapscribe.__main__.uvicorn.run", _fake_run)
    return calls


@pytest.fixture(autouse=True)
def _restore_app_recorder(monkeypatch: pytest.MonkeyPatch):
    """`main()` assigns directly to `app.state.recorder`/`app.state.log_json`
    (a real module-level FastAPI singleton), not through a fixture. Save and
    restore both attributes so a CLI-boundary test never leaks a Recorder
    into a test file collected after it."""
    from tapscribe.app import app

    had_recorder = hasattr(app.state, "recorder")
    prior_recorder = getattr(app.state, "recorder", None)
    had_log_json = hasattr(app.state, "log_json")
    prior_log_json = getattr(app.state, "log_json", None)
    yield
    if had_recorder:
        app.state.recorder = prior_recorder
    elif hasattr(app.state, "recorder"):
        del app.state.recorder
    if had_log_json:
        app.state.log_json = prior_log_json
    elif hasattr(app.state, "log_json"):
        del app.state.log_json


def _run_main(argv: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    from tapscribe.__main__ import main

    monkeypatch.setattr(sys, "argv", ["python -m tapscribe", *argv])
    main()


# ---------------------------------------------------------------------------
# build_parser() — argument validation (choices=/type=)
# ---------------------------------------------------------------------------


def test_backend_rejects_unknown_choice() -> None:
    """`--backend` is constrained to a fixed choice set — CodeQL treats
    argparse values as external input, so this boundary is validated,
    not passed through."""
    from tapscribe.__main__ import build_parser

    with pytest.raises(SystemExit):
        build_parser().parse_args(["--backend", "quantum"])


def test_live_buffer_trimming_rejects_unknown_choice() -> None:
    from tapscribe.__main__ import build_parser

    with pytest.raises(SystemExit):
        build_parser().parse_args(["--live-buffer-trimming", "paragraph"])


def test_port_rejects_non_integer() -> None:
    from tapscribe.__main__ import build_parser

    with pytest.raises(SystemExit):
        build_parser().parse_args(["--port", "not-a-number"])


def test_backend_default_is_auto() -> None:
    from tapscribe.__main__ import build_parser

    args = build_parser().parse_args([])
    assert args.backend == "auto"


# ---------------------------------------------------------------------------
# main() — flag -> Recorder/LiveConfig wiring
# ---------------------------------------------------------------------------


def test_main_default_boot_wires_recorder_and_uvicorn(
    boot_paths: Path,
    fixed_backends: None,
    uvicorn_spy: list[dict],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A default (no-flag) boot constructs a Recorder rooted at the
    configured dirs and hands uvicorn the default host/port with TLS off."""
    from tapscribe.app import app

    _run_main([], monkeypatch)

    assert app.state.recorder.recordings_dir == boot_paths / "recordings"
    assert app.state.recorder.config_dir == boot_paths / "config"
    assert app.state.log_json is False
    assert len(uvicorn_spy) == 1
    call = uvicorn_spy[0]
    assert call["host"] == "localhost"
    assert call["port"] == 8001
    assert call["ssl_certfile"] is None
    assert call["ssl_keyfile"] is None


def test_main_wires_live_config_flags(
    boot_paths: Path,
    fixed_backends: None,
    uvicorn_spy: list[dict],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every `--live-*` flag lands on the LiveChannel's LiveConfig
    unchanged — this is the flag<->config mapping the issue calls out as
    untested."""
    from tapscribe.app import app

    _run_main(
        [
            "--live-model",
            "small.en",
            "--live-language",
            "no",
            "--live-host",
            "0.0.0.0",
            "--live-port",
            "9123",
            "--live-min-chunk-size",
            "1.5",
            "--live-buffer-trimming",
            "segment",
            "--live-buffer-trimming-sec",
            "30",
            "--live-max-context-tokens",
            "100",
            "--live-gate-min-speech-ms",
            "50",
        ],
        monkeypatch,
    )

    live_cfg = app.state.recorder.live.config
    assert live_cfg.model == "small.en"
    assert live_cfg.language == "no"
    assert live_cfg.host == "0.0.0.0"
    assert live_cfg.port == 9123
    assert live_cfg.min_chunk_size == 1.5
    assert live_cfg.buffer_trimming == "segment"
    assert live_cfg.buffer_trimming_sec == 30
    assert live_cfg.max_context_tokens == 100
    assert live_cfg.gate_min_speech_ms == 50


def test_main_live_host_defaults_to_loopback(
    boot_paths: Path,
    fixed_backends: None,
    uvicorn_spy: list[dict],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No `--live-host` -> LiveConfig binds the live channel to loopback,
    never the dashboard's own `--host`."""
    from tapscribe.app import app

    _run_main(["--host", "0.0.0.0"], monkeypatch)

    assert app.state.recorder.live.config.host == "127.0.0.1"


def test_main_live_gate_min_speech_ms_unset_keeps_liveconfig_default(
    boot_paths: Path,
    fixed_backends: None,
    uvicorn_spy: list[dict],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--live-gate-min-speech-ms` is only forwarded when the operator
    passes it (`main()` special-cases `None` so LiveConfig's own default
    isn't clobbered by an explicit 0 vs "unset") — pin that branch."""
    from tapscribe.app import app
    from tapscribe.live import LiveConfig

    _run_main([], monkeypatch)

    assert app.state.recorder.live.config.gate_min_speech_ms == LiveConfig(
        model="x", language="x", host="x", port=0
    ).gate_min_speech_ms


def test_main_no_mlx_forces_cpu_when_backend_is_auto(
    boot_paths: Path,
    uvicorn_spy: list[dict],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--no-mlx` is the legacy alias: with the default `--backend=auto`
    it forces cpu even on a machine where mlx is available."""
    from tapscribe.app import app

    catalog.set_available_backends_for_testing(frozenset({"mlx", "cpu"}))
    try:
        _run_main(["--no-mlx"], monkeypatch)
    finally:
        catalog.set_available_backends_for_testing(None)

    assert app.state.recorder.backend == "cpu"


def test_main_no_mlx_does_not_override_explicit_backend(
    boot_paths: Path,
    uvicorn_spy: list[dict],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--no-mlx` only folds into `auto` — an explicit `--backend=cpu`
    (already cpu) stays cpu, and the legacy flag must not clobber a
    different explicit choice such as `cuda`."""
    from tapscribe.app import app

    catalog.set_available_backends_for_testing(frozenset({"mlx", "cuda", "cpu"}))
    try:
        _run_main(["--no-mlx", "--backend", "cuda"], monkeypatch)
    finally:
        catalog.set_available_backends_for_testing(None)

    assert app.state.recorder.backend == "cuda"


def test_main_backend_unavailable_exits_with_code_2(
    boot_paths: Path,
    uvicorn_spy: list[dict],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Picking a backend kind that isn't available on this machine fails
    loudly before uvicorn starts, instead of surfacing on the first
    transcribe call."""
    catalog.set_available_backends_for_testing(frozenset({"cpu"}))
    try:
        with pytest.raises(SystemExit) as exc_info:
            _run_main(["--backend", "cuda"], monkeypatch)
    finally:
        catalog.set_available_backends_for_testing(None)

    assert exc_info.value.code == 2
    assert uvicorn_spy == []  # never reached uvicorn.run
    assert "FATAL" in capsys.readouterr().out


def test_main_no_auth_disables_auth_and_prints_warning(
    boot_paths: Path,
    fixed_backends: None,
    uvicorn_spy: list[dict],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _run_main(["--no-auth"], monkeypatch)

    assert _config.AUTH_ENABLED is False
    assert "UNAUTHENTICATED" in capsys.readouterr().out


def test_main_default_auth_enabled_prints_credentials_banner(
    boot_paths: Path,
    fixed_backends: None,
    uvicorn_spy: list[dict],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from tapscribe.app import app

    _run_main([], monkeypatch)

    assert _config.AUTH_ENABLED is True
    out = capsys.readouterr().out
    assert "Dashboard auth: HTTP Basic" in out
    assert app.state.recorder.auth.value in out


def test_main_host_0000_prints_lan_warning(
    boot_paths: Path,
    fixed_backends: None,
    uvicorn_spy: list[dict],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _run_main(["--host", "0.0.0.0"], monkeypatch)

    assert "exposes the recorder to" in capsys.readouterr().out


def test_main_loopback_host_prints_no_lan_warning(
    boot_paths: Path,
    fixed_backends: None,
    uvicorn_spy: list[dict],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _run_main([], monkeypatch)

    assert "exposes the recorder to" not in capsys.readouterr().out


def test_main_rotate_password_changes_persisted_secret(
    boot_paths: Path,
    fixed_backends: None,
    uvicorn_spy: list[dict],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tapscribe.app import app

    auth_file = boot_paths / ".auth-password"
    auth_file.write_text("preexisting-password", encoding="utf-8")

    _run_main(["--rotate-password"], monkeypatch)

    assert app.state.recorder.auth.value != "preexisting-password"
    assert auth_file.read_text(encoding="utf-8").strip() == app.state.recorder.auth.value


def test_main_rotate_tap_token_changes_persisted_secret(
    boot_paths: Path,
    fixed_backends: None,
    uvicorn_spy: list[dict],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tapscribe.app import app

    tap_file = boot_paths / ".tap-token"
    tap_file.write_text("preexisting-token", encoding="utf-8")

    _run_main(["--rotate-tap-token"], monkeypatch)

    assert app.state.recorder.tap.value != "preexisting-token"
    assert tap_file.read_text(encoding="utf-8").strip() == app.state.recorder.tap.value


def test_main_no_rotate_flags_leave_secrets_untouched(
    boot_paths: Path,
    fixed_backends: None,
    uvicorn_spy: list[dict],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    auth_file = boot_paths / ".auth-password"
    auth_file.write_text("stays-the-same", encoding="utf-8")

    _run_main([], monkeypatch)

    assert auth_file.read_text(encoding="utf-8").strip() == "stays-the-same"


def test_main_log_json_flag_sets_app_state(
    boot_paths: Path,
    fixed_backends: None,
    uvicorn_spy: list[dict],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tapscribe.app import app

    _run_main(["--log-json"], monkeypatch)

    assert app.state.log_json is True


def test_main_tls_missing_cert_or_key_exits_with_code_2(
    boot_paths: Path,
    fixed_backends: None,
    uvicorn_spy: list[dict],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`--cert`/`--key` pointing at files that don't exist fails fast
    instead of handing uvicorn a bad ssl_certfile path."""
    missing_cert = boot_paths / "nope-cert.pem"
    missing_key = boot_paths / "nope-key.pem"

    with pytest.raises(SystemExit) as exc_info:
        _run_main(["--cert", str(missing_cert), "--key", str(missing_key)], monkeypatch)

    assert exc_info.value.code == 2
    assert uvicorn_spy == []
    assert "ERROR" in capsys.readouterr().out


def test_main_tls_generates_self_signed_pair_and_wires_uvicorn(
    boot_paths: Path,
    fixed_backends: None,
    uvicorn_spy: list[dict],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--tls` with no explicit --cert/--key generates a self-signed pair
    next to the auth secrets and passes both paths to uvicorn.run."""
    _run_main(["--tls"], monkeypatch)

    cert_path = boot_paths / ".tapscribe-cert.pem"
    key_path = boot_paths / ".tapscribe-key.pem"
    assert cert_path.is_file()
    assert key_path.is_file()
    assert len(uvicorn_spy) == 1
    assert uvicorn_spy[0]["ssl_certfile"] == str(cert_path)
    assert uvicorn_spy[0]["ssl_keyfile"] == str(key_path)


def test_main_cert_key_flags_imply_tls_without_explicit_tls_flag(
    boot_paths: Path,
    fixed_backends: None,
    uvicorn_spy: list[dict],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Passing --cert/--key alone (no --tls) still turns TLS on — the
    `use_tls = args.tls or bool(args.cert or args.key)` branch."""
    from tapscribe.tls import ensure_self_signed_cert

    cert_path = boot_paths / "my-cert.pem"
    key_path = boot_paths / "my-key.pem"
    # Seed a valid pair directly via the real helper so the boundary test
    # doesn't also depend on _looks_valid's internals.
    ensure_self_signed_cert(cert_path, key_path, host="localhost")

    _run_main(["--cert", str(cert_path), "--key", str(key_path)], monkeypatch)

    assert uvicorn_spy[0]["ssl_certfile"] == str(cert_path)
    assert uvicorn_spy[0]["ssl_keyfile"] == str(key_path)


# ---------------------------------------------------------------------------
# Subprocess smoke test — catches import-time breakage the in-process
# tests above can't (they already imported the module).
# ---------------------------------------------------------------------------


def test_help_smoke_via_subprocess() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "tapscribe", "--help"],
        cwd=Path(__file__).resolve().parent.parent,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0
    assert "--auto-live" in result.stdout
