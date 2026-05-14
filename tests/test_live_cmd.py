"""Tests for build_live_cmd — pure argv construction for whisperlivekit-server.

These tests parametrize over the knob soup (model + use_mlx + vac +
confidence_validation + NB-Whisper) so any regression in the CLI surface
fails loudly.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tapscribe.live import LiveConfig, build_live_cmd

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


def test_no_vac_flag_only_appended_when_vac_disabled():
    cfg_vac_on = LiveConfig(model="tiny.en", language="en", host="h", port=8000, vac=True)
    cfg_vac_off = LiveConfig(model="tiny.en", language="en", host="h", port=8000, vac=False)
    assert "--no-vac" not in build_live_cmd(EXE, cfg_vac_on, use_mlx=False)
    assert "--no-vac" in build_live_cmd(EXE, cfg_vac_off, use_mlx=False)


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
