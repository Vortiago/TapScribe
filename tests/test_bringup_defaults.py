"""RED contract for `tapscribe.bringup_defaults` — one Python owner of the launch values (#357).

`start.sh` and `start.ps1` each re-derived the same five bring-up values —
bind host, recorder port, ephemeral live port, initial live model, language
hint — plus the `SX_*` precedence chain above them, once per language. A bash
`:-` and a PowerShell truthiness test are two owners of one rule, which is
how they drift. Both scripts now read the values from `python -m
tapscribe.bringup_defaults`, and `__main__.build_parser` imports the same
constants, so a launch that skips the scripts cannot disagree either.

The expected values are literals from the scripts' own header docs
(`start.sh`'s "Configurable via env vars" block), not imported from the
module: test 1 is the contract, and the sharing tests pin that everything
else reads the same source.
"""

from __future__ import annotations

import re
from pathlib import Path

from tapscribe import bringup_defaults
from tapscribe.bringup_defaults import BringupConfig, resolve

REPO_ROOT = Path(__file__).resolve().parent.parent
_START_SCRIPTS = [REPO_ROOT / "start.sh", REPO_ROOT / "start.ps1"]
_SX_VARS = ("SX_HOST", "SX_PORT_REC", "SX_PORT_WLK", "SX_MODEL", "SX_LANG")


def _clear_sx_env(monkeypatch) -> None:
    for name in _SX_VARS:
        monkeypatch.delenv(name, raising=False)


# --- resolve(): the precedence rule ------------------------------------------


def test_defaults_are_the_documented_bring_up_values():
    """The documented bring-up defaults: localhost, 8001, an ephemeral live
    port, tiny.en, en. Literals, not module constants — this is the contract
    the module must keep."""
    assert resolve({}, lan=False) == BringupConfig(
        host="localhost", port_rec="8001", port_wlk="", model="tiny.en", lang="en"
    )


def test_lan_makes_the_bind_host_all_interfaces():
    assert resolve({}, lan=True).host == "0.0.0.0"


def test_env_vars_beat_every_default():
    env = {
        "SX_HOST": "10.0.0.5",
        "SX_PORT_REC": "9001",
        "SX_PORT_WLK": "9100",
        "SX_MODEL": "small.en",
        "SX_LANG": "no",
    }
    assert resolve(env, lan=True) == BringupConfig(
        host="10.0.0.5", port_rec="9001", port_wlk="9100", model="small.en", lang="no"
    )


def test_sx_host_beats_lan():
    """Both scripts' current rule: an explicit host wins over the LAN flag."""
    assert resolve({"SX_HOST": "10.0.0.5"}, lan=True).host == "10.0.0.5"


def test_blank_env_vars_fall_back_to_defaults():
    """Empty means unset in both scripts today (bash `:-`, PowerShell
    truthiness) — `SX_MODEL=` must not launch the recorder with no model."""
    assert resolve(dict.fromkeys(_SX_VARS, ""), lan=True) == BringupConfig(
        host="0.0.0.0", port_rec="8001", port_wlk="", model="tiny.en", lang="en"
    )


# --- main(): the wire format the scripts parse --------------------------------


def test_main_prints_key_value_lines(monkeypatch, capsys):
    """Five KEY=value lines in the fixed order both scripts' parsers read."""
    _clear_sx_env(monkeypatch)

    assert bringup_defaults.main([]) == 0

    out = capsys.readouterr().out
    parsed = dict(line.split("=", 1) for line in out.strip().splitlines())
    assert list(parsed) == ["HOST", "PORT_REC", "PORT_WLK", "MODEL", "LANG"]
    assert parsed["HOST"] == "localhost"


def test_main_lan_flag_switches_the_host_default(monkeypatch, capsys):
    _clear_sx_env(monkeypatch)

    bringup_defaults.main(["--lan"])

    assert "HOST=0.0.0.0" in capsys.readouterr().out.splitlines()


# --- the sharing: no second copy of the values anywhere -----------------------


def test_recorder_cli_defaults_share_the_source(monkeypatch):
    """`build_parser` must read the module's constants, not restate them:
    patch the constants and the parser's defaults move with them. (The port
    pairs through `config.PORT`, which `PORT_REC` is derived from.)"""
    from tapscribe.__main__ import build_parser

    monkeypatch.setattr(bringup_defaults, "HOST", "patched-host")
    monkeypatch.setattr(bringup_defaults, "MODEL", "patched-model")
    monkeypatch.setattr(bringup_defaults, "LANG", "patched-lang")

    args = build_parser().parse_args([])

    assert args.host == "patched-host"
    assert args.live_model == "patched-model"
    assert args.live_language == "patched-lang"
    assert str(args.port) == bringup_defaults.PORT_REC


def test_start_scripts_never_restate_bring_up_values():
    """The drift gate: the scripts read the values, they do not restate them.

    Header prose keeps its `SX_*` usage docs, so comment lines are dropped
    before the scan; live code must name neither the values nor the variables,
    and must invoke the shared module."""
    for script in _START_SCRIPTS:
        code = "\n".join(
            line
            for line in script.read_text(encoding="utf-8").splitlines()
            if not line.lstrip().startswith("#")
        )
        for token in ("8001", "tiny.en", "SX_"):
            assert token not in code, f"{script.name} still restates {token!r} in live code"
        assert re.search(r"python -m tapscribe\.bringup_defaults", code), (
            f"{script.name} does not invoke the shared bring-up defaults"
        )
