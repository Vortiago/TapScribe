"""RED contract for `tapscribe.bringup_defaults`, the one Python owner of the launch values.

The expected values are literals from the scripts' own header docs
(`start.sh`'s "Configurable via env vars" block), not imported from the
module: the `resolve` tests are the contract. The sharing tests pin that the
recorder's argparse defaults and both start scripts read that one source, and
the round-trip tests pin that each script variable reads its matching key.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tapscribe import bringup_defaults, config
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
    """Empty means unset: `SX_MODEL=` must not launch the recorder with no model."""
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
    patch the constants and the parser's defaults move with them."""
    from tapscribe.__main__ import build_parser

    monkeypatch.setattr(bringup_defaults, "HOST", "patched-host")
    monkeypatch.setattr(bringup_defaults, "MODEL", "patched-model")
    monkeypatch.setattr(bringup_defaults, "LANG", "patched-lang")
    monkeypatch.setattr(bringup_defaults, "PORT_WLK", "9100")

    args = build_parser().parse_args([])

    assert args.host == "patched-host"
    assert args.live_model == "patched-model"
    assert args.live_language == "patched-lang"
    assert args.live_port == 9100


def test_recorder_port_is_derived_from_config():
    """`config.PORT` owns the recorder port. `PORT_REC` is its str form, not
    a second literal that happens to agree."""
    assert bringup_defaults.PORT_REC == str(config.PORT)


# A read of an `SX_*` variable: bash `$SX_X` / `${SX_X:-…}`, PowerShell `$env:SX_X`.
# A bare `SX_MODEL=` in banner text names the override and reads nothing.
_SX_READ = re.compile(r"\$\{?SX_|\$env:SX_", re.IGNORECASE)


def _live_code(script: Path) -> str:
    """The script without its comment lines, which keep the `SX_*` usage docs."""
    return "\n".join(
        line for line in script.read_text(encoding="utf-8").splitlines() if not line.lstrip().startswith("#")
    )


@pytest.mark.parametrize("script", _START_SCRIPTS, ids=lambda p: p.name)
def test_start_scripts_never_restate_bring_up_values(script):
    """The drift gate: the scripts read the values, they do not restate them.
    Live code names no default value, reads no `SX_*` variable, and invokes
    the shared module."""
    code = _live_code(script)
    for token in ("8001", "tiny.en"):
        assert token not in code, f"{script.name} still restates {token!r} in live code"
    assert not _SX_READ.search(code), f"{script.name} still reads an SX_* variable itself"
    assert re.search(r"python -m tapscribe\.bringup_defaults", code), (
        f"{script.name} does not invoke the shared bring-up defaults"
    )


# --- the consumers: each script variable reads its matching key ---------------


def _start_sh_parse_bringup() -> str:
    body = re.search(
        r"^parse_bringup\(\) \{\n.*?^\}\n",
        (REPO_ROOT / "start.sh").read_text(encoding="utf-8"),
        re.MULTILINE | re.DOTALL,
    )
    assert body, "start.sh has no parse_bringup() function"
    return body.group(0)


def _run_start_sh_parse(env: dict[str, str], lan: bool) -> dict[str, str]:
    """Feed the real module output through start.sh's own parser and report
    the five variables it sets, plus LANG, which it must leave alone."""
    names = ("HOST", "PORT_REC", "PORT_WLK", "MODEL", "LANG_CODE", "LANG")
    script = (
        _start_sh_parse_bringup()
        + 'parse_bringup <<< "$("$PY" -m tapscribe.bringup_defaults $LAN_FLAG)"\n'
        + "".join(f'printf "{name}=%s\\n" "${name}"\n' for name in names)
    )
    # An inherited HOST or MODEL would mask a key the parser failed to set.
    base = {k: v for k, v in os.environ.items() if not k.startswith("SX_") and k not in names}
    run_env = base | env | {"PY": sys.executable, "LAN_FLAG": "--lan" if lan else ""}
    out = subprocess.run(
        ["bash", "-c", script],
        cwd=REPO_ROOT,
        env=run_env,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return dict(line.split("=", 1) for line in out.splitlines())


_needs_bash = pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("bash") is None,
    reason="start.sh runs under a POSIX bash",
)


@_needs_bash
@pytest.mark.parametrize(
    ("env", "lan", "expected"),
    [
        ({}, False, ("localhost", "8001", "", "tiny.en", "en")),
        ({}, True, ("0.0.0.0", "8001", "", "tiny.en", "en")),
        ({"SX_HOST": "10.0.0.5"}, True, ("10.0.0.5", "8001", "", "tiny.en", "en")),
        (
            {
                "SX_PORT_REC": "9001",
                "SX_PORT_WLK": "9100",
                "SX_MODEL": "small.en",
                "SX_LANG": "no",
            },
            False,
            ("localhost", "9001", "9100", "small.en", "no"),
        ),
    ],
    ids=["defaults", "lan", "sx-host-beats-lan", "every-override"],
)
def test_start_sh_parses_every_bring_up_value(env, lan, expected):
    parsed = _run_start_sh_parse(env | {"LANG": "C.UTF-8"}, lan)

    assert (
        parsed["HOST"],
        parsed["PORT_REC"],
        parsed["PORT_WLK"],
        parsed["MODEL"],
        parsed["LANG_CODE"],
    ) == expected
    assert parsed["LANG"] == "C.UTF-8", "start.sh overwrote the exported locale"


@pytest.mark.parametrize(
    ("variable", "key"),
    [
        ("BindHost", "HOST"),
        ("PortRec", "PORT_REC"),
        ("PortWlk", "PORT_WLK"),
        ("Model", "MODEL"),
        ("LangCode", "LANG"),
    ],
)
def test_start_ps1_reads_each_variable_from_its_key(variable, key):
    """A static pin, so the gate needs no PowerShell: the mapping is one line
    per variable."""
    code = _live_code(REPO_ROOT / "start.ps1")
    assert re.search(rf'^\${variable} = \$Bringup\["{key}"\]$', code, re.MULTILINE), (
        f'start.ps1 does not set ${variable} from $Bringup["{key}"]'
    )
