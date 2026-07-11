"""Seam tests for the --auto-live / --no-auto-live CLI flag mapping."""

from tapscribe.__main__ import apply_live_autostart_flag


def test_default_is_off() -> None:
    assert apply_live_autostart_flag(no_auto_live=False, auto_live=False) is False


def test_auto_live_opt_in() -> None:
    assert apply_live_autostart_flag(no_auto_live=False, auto_live=True) is True


def test_no_auto_live_is_noop() -> None:
    assert apply_live_autostart_flag(no_auto_live=True, auto_live=False) is False


def test_auto_live_wins_over_deprecated_flag() -> None:
    assert apply_live_autostart_flag(no_auto_live=True, auto_live=True) is True
