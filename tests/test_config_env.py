"""Tests for tapscribe.config.env_float / env_int — the shared env
parsers operators use to tune numeric knobs without a code change."""

from __future__ import annotations

import pytest

from tapscribe.config import env_float, env_int


def test_env_float_returns_default_when_unset(monkeypatch):
    monkeypatch.delenv("TAPSCRIBE_TEST_X", raising=False)
    assert env_float("TAPSCRIBE_TEST_X", 1.5) == 1.5


def test_env_float_parses_set_value(monkeypatch):
    monkeypatch.setenv("TAPSCRIBE_TEST_X", "2.5")
    assert env_float("TAPSCRIBE_TEST_X", 1.5) == 2.5


def test_env_float_unparseable_falls_back_to_default(monkeypatch, capsys):
    """A typo doesn't take down the recorder — we log once and use the
    default so the operator sees their mistake on next boot."""
    monkeypatch.setenv("TAPSCRIBE_TEST_X", "not-a-number")
    assert env_float("TAPSCRIBE_TEST_X", 1.5) == 1.5
    captured = capsys.readouterr()
    assert "ignoring unparseable" in captured.out


def test_env_float_empty_string_uses_default(monkeypatch):
    """An empty value is "unset" semantics — no warning, no parsing."""
    monkeypatch.setenv("TAPSCRIBE_TEST_X", "")
    assert env_float("TAPSCRIBE_TEST_X", 1.5) == 1.5


def test_env_int_returns_default_when_unset(monkeypatch):
    monkeypatch.delenv("TAPSCRIBE_TEST_X", raising=False)
    assert env_int("TAPSCRIBE_TEST_X", 42) == 42


def test_env_int_parses_set_value(monkeypatch):
    monkeypatch.setenv("TAPSCRIBE_TEST_X", "100")
    assert env_int("TAPSCRIBE_TEST_X", 42) == 100


def test_env_int_unparseable_falls_back_to_default(monkeypatch, capsys):
    monkeypatch.setenv("TAPSCRIBE_TEST_X", "3.14")
    assert env_int("TAPSCRIBE_TEST_X", 42) == 42
    assert "ignoring unparseable" in capsys.readouterr().out


def test_env_int_negative_value_parsed(monkeypatch):
    """Negative ints are valid — no built-in bounds checking; callers
    that want positive-only validate at the use site."""
    monkeypatch.setenv("TAPSCRIBE_TEST_X", "-5")
    assert env_int("TAPSCRIBE_TEST_X", 42) == -5


@pytest.mark.parametrize("value", ["0", "0.0", "-0"])
def test_env_zero_returned_as_set_value(monkeypatch, value):
    """Zero is a valid set value — don't conflate with unset."""
    monkeypatch.setenv("TAPSCRIBE_TEST_X", value)
    assert env_float("TAPSCRIBE_TEST_X", 1.5) == 0.0


def test_env_float_below_min_falls_back_to_default(monkeypatch, capsys):
    """A `TAPSCRIBE_CANARY_CHUNK_S=-5` would otherwise reach the
    consumer where `int(-5 * 16000)` followed by `max(1, ...)` clamps
    to 1, producing a pathological one-sample-per-window loop. The
    bound rejects negative values at the boundary instead."""
    monkeypatch.setenv("TAPSCRIBE_TEST_X", "-5")
    assert env_float("TAPSCRIBE_TEST_X", 30.0, min_value=1.0, max_value=600.0) == 30.0
    assert "< min" in capsys.readouterr().out


def test_env_float_above_max_falls_back_to_default(monkeypatch, capsys):
    monkeypatch.setenv("TAPSCRIBE_TEST_X", "9999")
    assert env_float("TAPSCRIBE_TEST_X", 30.0, min_value=1.0, max_value=600.0) == 30.0
    assert "> max" in capsys.readouterr().out


def test_env_float_at_bounds_inclusive(monkeypatch):
    """Boundary values are accepted — the bounds are inclusive."""
    monkeypatch.setenv("TAPSCRIBE_TEST_X", "1.0")
    assert env_float("TAPSCRIBE_TEST_X", 30.0, min_value=1.0, max_value=600.0) == 1.0
    monkeypatch.setenv("TAPSCRIBE_TEST_X", "600")
    assert env_float("TAPSCRIBE_TEST_X", 30.0, min_value=1.0, max_value=600.0) == 600.0


def test_env_int_below_min_falls_back(monkeypatch, capsys):
    monkeypatch.setenv("TAPSCRIBE_TEST_X", "0")
    assert env_int("TAPSCRIBE_TEST_X", 256, min_value=16, max_value=4096) == 256
    assert "< min" in capsys.readouterr().out


def test_env_int_above_max_falls_back(monkeypatch, capsys):
    monkeypatch.setenv("TAPSCRIBE_TEST_X", "100000")
    assert env_int("TAPSCRIBE_TEST_X", 256, min_value=16, max_value=4096) == 256
    assert "> max" in capsys.readouterr().out
