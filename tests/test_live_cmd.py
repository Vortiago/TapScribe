"""Tests for build_live_cmd — pure argv construction for whisperlivekit-server.

These tests parametrize over the knob soup (model + use_mlx + vac +
confidence_validation + NB-Whisper) so any regression in the CLI surface
fails loudly.
"""

from __future__ import annotations

import socket
from pathlib import Path
from unittest.mock import patch

import pytest

from tapscribe.live import (
    LiveConfig,
    WhisperLiveKitChannel,
    _is_console_worthy,
    _probe_port_free,
    build_live_cmd,
)

# Back-compat alias so the pre-refactor test names still read naturally.
LiveChannel = WhisperLiveKitChannel

EXE = "/path/to/whisperlivekit-server"
DEFAULT_CFG = LiveConfig(model="tiny.en", language="en", host="localhost", port=8000)


def test_argv_starts_with_exe():
    cmd = build_live_cmd(EXE, DEFAULT_CFG, use_mlx=False)
    assert cmd[0] == EXE


def test_argv_always_passes_pcm_input():
    cmd = build_live_cmd(EXE, DEFAULT_CFG, use_mlx=False)
    assert "--pcm-input" in cmd


def test_argv_for_standard_whisper_model_includes_model_flag():
    cmd = build_live_cmd(EXE, DEFAULT_CFG, use_mlx=False)
    assert "--model" in cmd
    assert cmd[cmd.index("--model") + 1] == "tiny.en"


def test_mlx_backend_only_appended_when_use_mlx_true():
    cpu_cmd = build_live_cmd(EXE, DEFAULT_CFG, use_mlx=False)
    mlx_cmd = build_live_cmd(EXE, DEFAULT_CFG, use_mlx=True)
    assert "--backend" not in cpu_cmd
    assert "--backend" in mlx_cmd
    assert mlx_cmd[mlx_cmd.index("--backend") + 1] == "mlx-whisper"


def test_no_vac_flag_appended_when_gate_kind_is_tapscribe():
    """gate_kind="tapscribe" means TapScribe runs its own SpeechGate
    and WlK's native VAC must be off — translated to the --no-vac
    flag here at argv construction time."""
    cfg_backend = LiveConfig(model="tiny.en", language="en", host="h", port=8000, gate_kind="backend")
    cfg_tapscribe = LiveConfig(model="tiny.en", language="en", host="h", port=8000, gate_kind="tapscribe")
    assert "--no-vac" not in build_live_cmd(EXE, cfg_backend, use_mlx=False)
    assert "--no-vac" in build_live_cmd(EXE, cfg_tapscribe, use_mlx=False)


def test_gate_kind_default_is_tapscribe():
    """Operators who don't override get the TapScribe-side gate by default."""
    cfg = LiveConfig(model="tiny.en", language="en", host="h", port=8000)
    assert cfg.gate_kind == "tapscribe"
    assert "--no-vac" in build_live_cmd(EXE, cfg, use_mlx=False)


def test_whisper_live_kit_channel_supports_native_vad():
    """The dashboard reads `supports_native_vad` to decide whether to
    surface the "backend" gate_kind option. WhisperLiveKit has --vac
    / --no-vac, so True. (Future ParakeetLiveChannel without a native
    VAD will be False; that test lives with its own implementation.)
    """
    cfg = LiveConfig(model="tiny.en", language="en", host="h", port=8000)
    chan = LiveChannel(config=cfg, use_mlx=False)
    assert chan.supports_native_vad is True


def test_gate_knobs_are_independent_of_build_live_cmd():
    """gate_speech_threshold / gate_hangover_ms / gate_pre_roll_ms are
    consumed by the TapScribe-side SpeechGate, NOT by WlK. They must
    not appear in the WlK argv regardless of their values."""
    cfg = LiveConfig(
        model="tiny.en",
        language="en",
        host="h",
        port=8000,
        gate_speech_threshold=0.7,
        gate_hangover_ms=600,
        gate_pre_roll_ms=500,
    )
    cmd = build_live_cmd(EXE, cfg, use_mlx=False)
    # No leakage of TapScribe-side knobs into the WlK child's argv.
    assert "--threshold" not in cmd
    assert "--gate-threshold" not in cmd
    assert "--hangover-ms" not in cmd
    assert "--pre-roll-ms" not in cmd
    assert "0.7" not in cmd
    assert "600" not in cmd
    assert "500" not in cmd


def test_confidence_validation_flag_only_appended_when_enabled():
    cfg_off = LiveConfig(model="tiny.en", language="en", host="h", port=8000, confidence_validation=False)
    cfg_on = LiveConfig(model="tiny.en", language="en", host="h", port=8000, confidence_validation=True)
    assert "--confidence-validation" not in build_live_cmd(EXE, cfg_off, use_mlx=False)
    assert "--confidence-validation" in build_live_cmd(EXE, cfg_on, use_mlx=False)


def test_init_prompt_only_passed_when_supplied():
    cmd_none = build_live_cmd(EXE, DEFAULT_CFG, use_mlx=False, init_prompt=None)
    cmd_with = build_live_cmd(EXE, DEFAULT_CFG, use_mlx=False, init_prompt="weekly planning")
    assert "--init-prompt" not in cmd_none
    assert "--init-prompt" in cmd_with
    assert cmd_with[cmd_with.index("--init-prompt") + 1] == "weekly planning"


def test_min_chunk_size_only_passed_when_set():
    """Operators dial latency vs. accuracy by raising min chunk size — bigger
    chunk = more future context per decode = better accuracy but more lag.
    Default (None) means WLK uses its own default; we don't emit the flag."""
    cfg_default = LiveConfig(model="tiny.en", language="en", host="h", port=8000)
    cfg_tuned = LiveConfig(model="tiny.en", language="en", host="h", port=8000, min_chunk_size=2.5)
    assert "--min-chunk-size" not in build_live_cmd(EXE, cfg_default, use_mlx=False)
    cmd = build_live_cmd(EXE, cfg_tuned, use_mlx=False)
    assert "--min-chunk-size" in cmd
    assert cmd[cmd.index("--min-chunk-size") + 1] == "2.5"


def test_buffer_trimming_only_passed_when_set():
    """`--buffer_trimming` controls how the rolling buffer is reset (sentence
    vs segment boundaries). `--buffer_trimming_sec` is the length threshold
    that triggers the reset. Both default to WLK's behavior (flag absent)."""
    cfg_default = LiveConfig(model="tiny.en", language="en", host="h", port=8000)
    cfg_tuned = LiveConfig(
        model="tiny.en",
        language="en",
        host="h",
        port=8000,
        buffer_trimming="sentence",
        buffer_trimming_sec=15.0,
    )
    assert "--buffer_trimming" not in build_live_cmd(EXE, cfg_default, use_mlx=False)
    assert "--buffer_trimming_sec" not in build_live_cmd(EXE, cfg_default, use_mlx=False)
    cmd = build_live_cmd(EXE, cfg_tuned, use_mlx=False)
    assert cmd[cmd.index("--buffer_trimming") + 1] == "sentence"
    assert cmd[cmd.index("--buffer_trimming_sec") + 1] == "15.0"


def test_buffer_trimming_strategy_alone_emits_only_the_strategy_flag():
    """Setting only the strategy (not the seconds threshold) emits just the
    strategy flag — WLK keeps its own default for the threshold."""
    cfg = LiveConfig(model="tiny.en", language="en", host="h", port=8000, buffer_trimming="segment")
    cmd = build_live_cmd(EXE, cfg, use_mlx=False)
    assert "--buffer_trimming" in cmd
    assert cmd[cmd.index("--buffer_trimming") + 1] == "segment"
    assert "--buffer_trimming_sec" not in cmd


def test_max_context_tokens_only_passed_when_set():
    """Raising max context tokens lets the decoder condition on more prior
    text — usually better continuations, more compute per tick. Default
    None (== don't emit the flag) means WLK's own default applies."""
    cfg_default = LiveConfig(model="tiny.en", language="en", host="h", port=8000)
    cfg_tuned = LiveConfig(model="tiny.en", language="en", host="h", port=8000, max_context_tokens=128)
    assert "--max-context-tokens" not in build_live_cmd(EXE, cfg_default, use_mlx=False)
    cmd = build_live_cmd(EXE, cfg_tuned, use_mlx=False)
    assert cmd[cmd.index("--max-context-tokens") + 1] == "128"


def test_nb_whisper_uses_model_path_and_backend_policy_not_model_flag(tmp_path: Path):
    cfg = LiveConfig(model="nb-whisper-medium", language="no", host="h", port=8000)
    ct2_dir = tmp_path / "nb-whisper-medium-ct2"
    ct2_dir.mkdir()
    cmd = build_live_cmd(EXE, cfg, use_mlx=False, nb_whisper_ct2_dir=ct2_dir)
    assert "--model-path" in cmd
    assert cmd[cmd.index("--model-path") + 1] == str(ct2_dir)
    assert "--backend-policy" in cmd
    assert cmd[cmd.index("--backend-policy") + 1] == "localagreement"
    # NB-Whisper must NOT use the --model name flag (WhisperLiveKit's table
    # rejects it) and must NOT force --backend mlx-whisper.
    assert "--model" not in cmd
    assert "--backend" not in cmd


def test_nb_whisper_ignores_use_mlx_true_in_argv(tmp_path: Path):
    """NB-Whisper routing forces faster-whisper regardless of operator
    MLX preference. The argv reflects that — no --backend mlx-whisper."""
    cfg = LiveConfig(model="nb-whisper-medium", language="no", host="h", port=8000)
    ct2_dir = tmp_path / "nb-whisper-medium-ct2"
    ct2_dir.mkdir()
    cmd = build_live_cmd(EXE, cfg, use_mlx=True, nb_whisper_ct2_dir=ct2_dir)
    assert "--backend" not in cmd


def test_nb_whisper_raises_without_ct2_dir():
    cfg = LiveConfig(model="nb-whisper-medium", language="no", host="h", port=8000)
    with pytest.raises(ValueError):
        build_live_cmd(EXE, cfg, use_mlx=False, nb_whisper_ct2_dir=None)


def test_argv_contains_host_port_and_language():
    cfg = LiveConfig(model="small.en", language="en", host="0.0.0.0", port=8123)
    cmd = build_live_cmd(EXE, cfg, use_mlx=False)
    assert "--host" in cmd
    assert cmd[cmd.index("--host") + 1] == "0.0.0.0"
    assert "--port" in cmd
    assert cmd[cmd.index("--port") + 1] == "8123"
    # WhisperLiveKit's `--lan <language>` flag carries the language code.
    assert "--lan" in cmd
    assert cmd[cmd.index("--lan") + 1] == "en"


# ---------------------------------------------------------------------------
# _is_console_worthy — _pump_logs classifier
# ---------------------------------------------------------------------------
#
# The dashboard log dialog reads the full WlK output from
# /api/live/log; only warnings / errors / tracebacks reach the
# recorder's stdout. Keeping this predicate honest matters because the
# point of the change is that the console stops being unreadable but
# real problems still surface there immediately.


@pytest.mark.parametrize(
    "line",
    [
        "WARNING:whisperlivekit:audio dropouts on tap-3",
        "ERROR:whisperlivekit.diarizer:cuda OOM",
        "CRITICAL:whisperlivekit:unrecoverable",
        "Traceback (most recent call last):",
        "  ERROR:whisperlivekit:fell back to cpu",  # leading whitespace ok
        "ERROR:    Application startup failed.",  # uvicorn-style
    ],
)
def test_console_worthy_lines_pass_through(line: str):
    assert _is_console_worthy(line) is True


@pytest.mark.parametrize(
    "line",
    [
        "INFO:whisperlivekit.audio_processor:internal_buffer=0.00s | lag=0.00s |",
        "INFO:whisperlivekit:loading model",
        "INFO:     Started server process [1234]",
        "DEBUG:whisperlivekit:tick",
        "",
        "some unrelated line without a level",
    ],
)
def test_non_warning_lines_are_filtered(line: str):
    assert _is_console_worthy(line) is False


# ---------------------------------------------------------------------------
# _probe_port_free — preflight before spawning the WLK child
# ---------------------------------------------------------------------------


def test_probe_returns_none_for_free_port():
    # Bind ephemerally to discover a port, release it, then probe.
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    assert _probe_port_free("127.0.0.1", port) is None


def test_probe_returns_diagnostic_when_port_taken():
    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    holder.bind(("127.0.0.1", 0))
    holder.listen(1)
    port = holder.getsockname()[1]
    try:
        msg = _probe_port_free("127.0.0.1", port)
        assert msg is not None
        # Diagnostic must name the port AND give the operator an actionable
        # hint — without that, the user is back to the cryptic [Errno 48].
        assert str(port) in msg
        assert "already in use" in msg
    finally:
        holder.close()


def test_start_with_port_zero_picks_ephemeral_port():
    """Default --live-port is 0 because WLK is internal; LiveChannel
    must allocate a real free port at spawn time, not pass 0 through to
    whisperlivekit-server (which would either fail or pick its own,
    leaving config.port stale for `live_relay` to use)."""
    cfg = LiveConfig(model="tiny.en", language="en", host="127.0.0.1", port=0)
    chan = LiveChannel(config=cfg, use_mlx=False)

    captured: dict = {}

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd

        class _Stdout:
            def __iter__(self):
                return iter(())

        class _P:
            pid = 12345
            stdout = _Stdout()

            def poll(self):
                return None

            def wait(self, timeout=None):
                return 0

        return _P()

    with (
        patch.object(LiveChannel, "_find_exe", return_value="/fake/whisperlivekit-server"),
        patch("tapscribe.live.subprocess.Popen", side_effect=fake_popen),
    ):
        ok, _ = chan.start()
    assert ok is True
    # config.port was rewritten to a real port the kernel handed us, and
    # the argv reflects the same number.
    assert chan.config.port != 0
    assert chan.config.port > 0
    assert "--port" in captured["cmd"]
    assert captured["cmd"][captured["cmd"].index("--port") + 1] == str(chan.config.port)


def test_restart_in_ephemeral_mode_picks_a_fresh_port():
    """A LiveChannel constructed with port=0 must re-allocate on every
    start(), not just the first. Otherwise the dashboard's stop→start
    "Apply model" path reuses the port whose listening socket is now in
    TIME_WAIT, which is the exact bug ephemeral defaults were meant to
    avoid."""
    cfg = LiveConfig(model="tiny.en", language="en", host="127.0.0.1", port=0)
    chan = LiveChannel(config=cfg, use_mlx=False)
    ports_seen: list[int] = []

    def fake_popen(cmd, **kwargs):
        ports_seen.append(int(cmd[cmd.index("--port") + 1]))

        class _Stdout:
            def __iter__(self):
                return iter(())

        class _P:
            pid = 0
            stdout = _Stdout()
            _alive = True

            def poll(self):
                return None if self._alive else 0

            def wait(self, timeout=None):
                self._alive = False
                return 0

        return _P()

    with (
        patch.object(LiveChannel, "_find_exe", return_value="/fake/whisperlivekit-server"),
        patch("tapscribe.live.subprocess.Popen", side_effect=fake_popen),
    ):
        chan.start()
        # Mark the proc as dead so the next start() proceeds; we don't
        # actually call stop() because it would try to SIGTERM a fake.
        if chan._proc is not None:
            chan._proc._alive = False  # type: ignore[attr-defined]
        chan.start()

    assert len(ports_seen) == 2
    assert ports_seen[0] != ports_seen[1], (
        f"second start reused port {ports_seen[0]} instead of allocating fresh"
    )


def test_start_fails_fast_when_port_in_use():
    """LiveChannel.start must NOT Popen if the WLK port is occupied —
    otherwise the child crashes 10-30s later with the same cryptic
    errno after bridges have already opened /tap WS connections."""
    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    holder.bind(("127.0.0.1", 0))
    holder.listen(1)
    port = holder.getsockname()[1]
    try:
        cfg = LiveConfig(model="tiny.en", language="en", host="127.0.0.1", port=port)
        chan = LiveChannel(config=cfg, use_mlx=False)
        with (
            patch.object(LiveChannel, "_find_exe", return_value="/fake/whisperlivekit-server"),
            patch("tapscribe.live.subprocess.Popen") as popen,
        ):
            ok, msg = chan.start()
        assert ok is False
        assert "already in use" in msg
        assert chan.info["state"] == "error"
        assert chan.info["last_error"] == msg
        popen.assert_not_called()
    finally:
        holder.close()
