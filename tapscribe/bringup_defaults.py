"""Bring-up defaults: the one Python owner of the values the start scripts launch with.

`start.sh` and `start.ps1` run `python -m tapscribe.bringup_defaults` and parse
the `KEY=value` lines it prints. `__main__.build_parser` imports the same
constants, so a bare `python -m tapscribe` launch gets the same defaults.
`resolve` is pure: an env mapping and the `--lan` flag go in, one
`BringupConfig` comes out.

Stdlib-only at import (`config` is stdlib-only too), so `-m` works against a
venv that holds nothing but pip. The scripts' line parsers truncate a value
that contains a newline, which no model id, port or host does.
"""

from __future__ import annotations

import argparse
import os
from collections.abc import Mapping
from dataclasses import asdict, dataclass

from tapscribe import config

#: Initial live Whisper model; changeable from the dashboard.
MODEL = "tiny.en"
#: Live language hint.
LANG = "en"
#: Bind host: loopback by default, all interfaces under `--lan`.
HOST = "localhost"
HOST_LAN = "0.0.0.0"
#: Empty means ephemeral: the recorder picks a free live port at spawn.
PORT_WLK = ""
#: `config.py` owns the one declaration of the recorder port; this is its str form.
PORT_REC = str(config.PORT)


@dataclass(frozen=True)
class BringupConfig:
    """The five launch values, resolved. All `str`, because they cross a shell boundary.

    The field order is the wire order, and a field's upper-cased name is its key.
    """

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
        help=f"Bind host: {HOST_LAN} instead of {HOST}.",
    )
    args = p.parse_args(argv)

    cfg = resolve(os.environ, lan=args.lan)
    for name, value in asdict(cfg).items():
        print(f"{name.upper()}={value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
