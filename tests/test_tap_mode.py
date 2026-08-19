"""Single-person vs multi-person tap: the precedence ladder and its durable
per-identity override (ADR-0021).

Precedence mirrors name resolution: operator override › bridge declaration ›
default single. The wire is lenient (an absent or junk declaration means
single); the operator PUT is strict, and lives in the route tests.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tapscribe import tap_mode


def test_a_tap_that_declares_nothing_is_single() -> None:
    """Every bridge predating this feature sends no declaration."""
    assert tap_mode.resolve(declared=None, override=None) == tap_mode.TAP_MODE_SINGLE


def test_a_tap_declaring_multi_is_multi() -> None:
    assert tap_mode.resolve(declared="multi", override=None) == tap_mode.TAP_MODE_MULTI


def test_an_unrecognised_declaration_is_single() -> None:
    """Never let junk mean multi — that manufactures Voices out of one human."""
    assert tap_mode.resolve(declared="MULTI-ish", override=None) == tap_mode.TAP_MODE_SINGLE


def test_the_operator_override_beats_the_declaration() -> None:
    """An NDI bridge cannot tell a room mic from a per-participant feed, so a
    declaration is only ever a default."""
    assert tap_mode.resolve(declared="single", override="multi") == tap_mode.TAP_MODE_MULTI
    assert tap_mode.resolve(declared="multi", override="single") == tap_mode.TAP_MODE_SINGLE


def test_a_junk_override_falls_through_to_the_declaration() -> None:
    assert tap_mode.resolve(declared="multi", override="sideways") == tap_mode.TAP_MODE_MULTI


# ---- the durable store -----------------------------------------------------


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "tap-modes.json"
    monkeypatch.setattr(tap_mode, "_store_path", lambda: path)
    return path


def test_an_override_survives_a_restart(store: Path) -> None:
    """`TapSettings` is in-memory by design and ADR-0021 keeps it that way, so
    this needs its own durable home."""
    tap_mode.set_override("tray-sysaudio-001", "multi")

    assert tap_mode.overrides() == {"tray-sysaudio-001": "multi"}
    assert store.exists()


def test_clearing_an_override_removes_it(store: Path) -> None:
    tap_mode.set_override("tray-sysaudio-001", "multi")

    tap_mode.set_override("tray-sysaudio-001", None)

    assert tap_mode.overrides() == {}


def test_a_missing_or_torn_store_reads_as_no_overrides(store: Path) -> None:
    assert tap_mode.overrides() == {}
    store.write_text("{not json", encoding="utf-8")
    assert tap_mode.overrides() == {}


def test_the_store_rejects_a_value_that_is_not_a_mode(store: Path) -> None:
    with pytest.raises(ValueError):
        tap_mode.set_override("tray-sysaudio-001", "sideways")
