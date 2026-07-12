"""Seam tests for the --auto-live / --no-auto-live CLI wiring.

These drive the real argparse layer (`build_parser`), so a dropped or
renamed flag string fails here — the flag wiring lives outside every
other fast gate.
"""

from tapscribe.__main__ import build_parser


def test_default_is_off() -> None:
    """No flag → the parser default is off."""
    args = build_parser().parse_args([])
    assert args.auto_live is False


def test_auto_live_opt_in() -> None:
    """`--auto-live` parses to True."""
    args = build_parser().parse_args(["--auto-live"])
    assert args.auto_live is True


def test_no_auto_live_still_parses() -> None:
    """Backward-compat: the deprecated `--no-auto-live` must still PARSE
    (a direct `python -m tapscribe --no-auto-live` from the e2e + C#
    consumers must not error with 'unrecognized arguments'), and it stays
    a no-op — off is the default."""
    args = build_parser().parse_args(["--no-auto-live"])
    assert args.auto_live is False


def test_auto_live_wins_over_deprecated_flag() -> None:
    """`--auto-live` opts in even when the deprecated `--no-auto-live` is
    also present — the retained flag never overrides the opt-in."""
    args = build_parser().parse_args(["--no-auto-live", "--auto-live"])
    assert args.auto_live is True
