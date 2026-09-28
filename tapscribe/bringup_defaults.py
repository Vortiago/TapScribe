"""Bring-up defaults — the one Python owner of the values the start scripts launch with.

`start.sh` and `start.ps1` each re-derived the same five launch values — bind
host, recorder port, ephemeral live port, initial live model, language hint —
and the `SX_*` precedence chain above them, once per language. Two owners of
one rule drift silently, so both scripts now run `python -m
tapscribe.bringup_defaults` and parse the five `KEY=value` lines it prints,
and `__main__.build_parser` imports the same constants so the recorder's own
argparse defaults cannot disagree with a scripted launch (#357).

`resolve` is pure — an env mapping and the `--lan` flag go in, one
`BringupConfig` comes out — following `preflight.plan_steps`: the whole
precedence rule lives here and is testable without a shell.

Stdlib-only at import, like its bring-up siblings (the one intra-package
import, `config`, is itself stdlib-only), so `-m` works against a venv that
holds nothing but pip, from the repo-root cwd the scripts cd to. A value
containing a newline is truncated by the scripts' line parsers — pathological
for model ids, ports and hosts, and accepted.
"""

from __future__ import annotations

import argparse
import os
from collections.abc import Mapping
from dataclasses import dataclass

from tapscribe import config

#: Initial live Whisper model; changeable from the dashboard.
MODEL = "tiny.en"
#: Live language hint.
LANG = "en"
#: Bind host: loopback by default, all interfaces under `--lan`.
HOST = "localhost"
HOST_LAN = "0.0.0.0"
#: Empty = ephemeral — the recorder picks a free live port at spawn.
PORT_WLK = ""
#: `config.py` owns the one declaration of the recorder port; this is its str form.
PORT_REC = str(config.PORT)


@dataclass(frozen=True)
class BringupConfig:
    """The five launch values, resolved. All `str` — they cross a shell boundary."""

    host: str
    port_rec: str
    port_wlk: str
    model: str
    lang: str


def resolve(env: Mapping[str, str], *, lan: bool = False) -> BringupConfig:
    """Resolve the bring-up values: a set, non-empty `SX_*` var beats the
    default, and `SX_HOST` beats `--lan`."""
    return BringupConfig(
        host=env.get("SX_HOST") or (HOST_LAN if lan else HOST),
        port_rec=env.get("SX_PORT_REC") or PORT_REC,
        port_wlk=env.get("SX_PORT_WLK") or PORT_WLK,
        model=env.get("SX_MODEL") or MODEL,
        lang=env.get("SX_LANG") or LANG,
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m tapscribe.bringup_defaults",
        description="Print TapScribe's bring-up launch values as KEY=value lines.",
    )
    p.add_argument(
        "--lan",
        action="store_true",
        help="Bind host: 0.0.0.0 instead of localhost.",
    )
    args = p.parse_args(argv)

    cfg = resolve(os.environ, lan=args.lan)
    for key, value in (
        ("HOST", cfg.host),
        ("PORT_REC", cfg.port_rec),
        ("PORT_WLK", cfg.port_wlk),
        ("MODEL", cfg.model),
        ("LANG", cfg.lang),
    ):
        print(f"{key}={value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
