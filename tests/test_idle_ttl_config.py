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

import math
from pathlib import Path

import pytest
from conftest import repoint_config_files  # type: ignore[import-not-found]  # tests/ on sys.path

from tapscribe.text import read_config, write_config
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


# ---------------------------------------------------------------------------
# Precedence fall-through: a set-but-INVALID env var must NOT shadow a valid
# config file. The env branch keys on presence, but an empty / non-numeric /
# out-of-bounds env value is not a real override — it falls through to the file
# (the systemd EnvironmentFile leaves `TAPSCRIBE_MODEL_IDLE_TTL_S=`, the operator
# set the dashboard knob). Without this, the empty env wins the env branch and
# resolves to 0.0 (evict-now), silently discarding the keep-warm value.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_env", ["", "abc", "999999999", "-2", "nan", "inf"])
def test_invalid_env_falls_through_to_config_file(
    cfg: Path, monkeypatch: pytest.MonkeyPatch, bad_env: str
) -> None:
    # DISCRIMINATOR: env set-but-invalid + a valid config file → the FILE value,
    # not the default. Base (env-branch keyed on `in os.environ`) returns 0.0.
    _set_ttl_file(cfg, 300)
    monkeypatch.setenv(_ENV, bad_env)
    assert _idle_ttl_s() == 300.0, (
        f"a set-but-invalid env var ({bad_env!r}) must fall through to a valid config file, "
        "not shadow it with the evict-now default"
    )


@pytest.mark.parametrize("bad_env", ["", "abc", "999999999", "-2", "nan", "inf"])
def test_invalid_env_without_config_falls_back_to_default(
    cfg: Path, monkeypatch: pytest.MonkeyPatch, bad_env: str
) -> None:
    # Guardrail: env invalid AND no config file → the module default, finite.
    monkeypatch.setenv(_ENV, bad_env)
    v = _idle_ttl_s()
    assert v == 0.0
    # NaN pitfall (a NaN env would make `== 0` False AND `> 0` False → the model
    # is never evicted, a silent leak). `float("nan")` parses without error and
    # slips past a naive range check (`lo <= nan <= hi` is False), so an
    # unguarded resolver would return NaN and disable eviction entirely — the
    # `bad_env="nan"` case above pins it to the finite default. The resolver
    # must never return NaN.
    assert math.isfinite(v)


# ---------------------------------------------------------------------------
# File-value round-trip at the consumer boundary.
# ---------------------------------------------------------------------------


def test_config_file_zero_is_evict_now(cfg: Path) -> None:
    # File "0" → evict-now (the release path compares `== 0.0`); a valid in-bounds
    # value, not a fall-back to the default that merely happens to also be 0.0.
    _set_ttl_file(cfg, 0)
    assert _idle_ttl_s() == 0.0


def test_config_file_whitespace_value_resolves(cfg: Path) -> None:
    # A trailing-newline / surrounding-whitespace file value must still resolve —
    # the read strips, and the parser strips again defensively.
    (cfg / _TTL_FILE).write_text("  300\n", encoding="utf-8")
    assert _idle_ttl_s() == 300.0


def test_config_file_negative_sentinel_keeps_warm(cfg: Path) -> None:
    # -1 is the in-bounds "never evict" sentinel — it must resolve to -1.0, not
    # degrade to the default (which would flip never-evict into evict-now).
    _set_ttl_file(cfg, -1)
    assert _idle_ttl_s() == -1.0


# ---------------------------------------------------------------------------
# The write-time validator, exercised DIRECTLY through write_config (the path
# the dashboard PUT takes). Also the only coverage of config_store._check_idle_ttl's
# use of `config._parse_bounded_ttl` / `config._IDLE_TTL_BOUNDS` — a rename there
# would break config writes at runtime, and this catches it.
# ---------------------------------------------------------------------------


def test_write_config_rejects_non_numeric(cfg: Path) -> None:
    with pytest.raises(ValueError):
        write_config("model-idle-ttl", "abc")


@pytest.mark.parametrize("bad", ["999999999", "-2", "nan", "inf"])
def test_write_config_rejects_out_of_bounds_or_nonfinite(cfg: Path, bad: str) -> None:
    # Hits the reject branch, which is what executes `lo, hi = config._IDLE_TTL_BOUNDS`
    # in config_store._check_idle_ttl.
    with pytest.raises(ValueError):
        write_config("model-idle-ttl", bad)


def test_write_config_empty_clears_override(cfg: Path) -> None:
    write_config("model-idle-ttl", "600")
    assert read_config("model-idle-ttl") == "600"
    write_config("model-idle-ttl", "")  # empty clears the override
    assert read_config("model-idle-ttl") == ""


def test_write_config_accepts_valid_and_resolver_reads_it(cfg: Path) -> None:
    # End-to-end: a dashboard write lands on disk AND the use-time resolver picks
    # it up (env unset in the `cfg` fixture), closing the write→read round-trip.
    write_config("model-idle-ttl", "600")
    assert read_config("model-idle-ttl") == "600"
    assert _idle_ttl_s() == 600.0
