"""RED contract for #210 — the model idle-TTL knob becomes dashboard-tunable.

`TAPSCRIBE_MODEL_IDLE_TTL_S` (when batch models unload) is env-only today, so an
operator can only change it by restarting the server with an env var — the one
config channel the product philosophy rejects. #210 makes it read from a
dashboard-writable config FILE when the env var is unset, applied at USE-time so a
change takes effect without a restart.

Resolution precedence (pinned here):

  env var (if set + valid)  >  config file (if set + valid)  >  default (0.0)

The config file is `CONFIG_DIR/model-idle-ttl.txt` (the dashboard writes it via the
config-store, key ``model-idle-ttl``); `_idle_ttl_s()` reads it at call time. The
existing env-var bounds (`_IDLE_TTL_BOUNDS`) still apply to the config-file value —
an out-of-range file value falls back to the default, exactly like a bad env var.

Out of this gate (named in the plan-spec, verified by code-review): the config-store
key registration + a route/dashboard Settings field to write the file. This file
pins the correctness-bearing core: the resolver's precedence, use-time read, and
bounds handling. The tests write the config file directly (the location the resolver
must read), so RED shows as clean assertion failures, not a missing-key error.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import repoint_config_files  # type: ignore[import-not-found]  # tests/ on sys.path

from tapscribe.transcribers import _idle_ttl_s

_ENV = "TAPSCRIBE_MODEL_IDLE_TTL_S"
_TTL_FILE = "model-idle-ttl.txt"  # under CONFIG_DIR


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A tmp CONFIG_DIR (all config-file constants repointed under it) with the
    idle-TTL env var cleared, so each test starts from a known baseline."""
    d = tmp_path / "config"
    d.mkdir()
    repoint_config_files(monkeypatch, d)
    monkeypatch.delenv(_ENV, raising=False)
    return d


def _set_ttl_file(cfg: Path, value) -> None:
    (cfg / _TTL_FILE).write_text(str(value), encoding="utf-8")


def test_config_file_used_when_env_unset(cfg: Path) -> None:
    # DISCRIMINATOR (RED at base): with no env var, the dashboard-written config
    # file drives the idle-TTL. Base returns the 0.0 default (env-only resolver).
    _set_ttl_file(cfg, 300)
    assert _idle_ttl_s() == 300.0, (
        "with the env var unset, idle-TTL must come from the config file the dashboard writes"
    )


def test_env_var_wins_over_config_file(cfg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Guardrail: the env var stays the override — it must win even when the config
    # file is also set (the fix must not let the file shadow an explicit env var).
    _set_ttl_file(cfg, 300)
    monkeypatch.setenv(_ENV, "600")
    assert _idle_ttl_s() == 600.0, "an explicit env var must win over the config file"


def test_default_when_neither_set(cfg: Path) -> None:
    # Guardrail: no env, no config file → the module default, unchanged.
    assert _idle_ttl_s() == 0.0


def test_config_file_is_read_at_use_time(cfg: Path) -> None:
    # DISCRIMINATOR (RED at base): a change to the config file is reflected on the
    # next resolve — no restart, no cached snapshot.
    _set_ttl_file(cfg, 100)
    assert _idle_ttl_s() == 100.0
    _set_ttl_file(cfg, 200)
    assert _idle_ttl_s() == 200.0, "a config-file change must apply at use-time, without a restart"


def test_out_of_bounds_config_falls_back_to_default(cfg: Path) -> None:
    # Guardrail: the existing knob bounds (_IDLE_TTL_BOUNDS, upper = 86400) apply to
    # the config-file value too — an out-of-range file value degrades to the default,
    # just like a bad env var (never lands unbounded at the consumer).
    _set_ttl_file(cfg, 999_999_999)
    assert _idle_ttl_s() == 0.0, "an out-of-bounds config value must fall back to the default"
