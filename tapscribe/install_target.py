"""Where pip installs TapScribe from — the one place the topology is decided.

TapScribe is installed three ways, and until ADR-0015 the code only knew about
the first:

  * **checkout** — a dev clone; ``pip install -e ".[extras]"``.
  * **Bundle** — the Windows installer (CONTEXT.md). A wheel ships next to the
    embedded interpreter and extras resolve from ITS metadata, so PyPI supplies
    dependencies but never TapScribe itself and the shipped installer's version
    cannot drift from the running server's.
  * **PyPI** — a plain ``pip install tapscribe`` in someone's venv.

The topology is an explicit ``--install-spec`` **argument**, never a sniffed
environment: auto-detection would have to guess whether "no ``pyproject.toml``
nearby" means Bundle or PyPI-install, and would guess wrong on a plain
``pip install tapscribe``.

Stdlib only, and deliberately so — ``tapscribe/__init__.py`` is a docstring and
``__version__`` with no imports, so ``_install_picker`` (which runs *before*
TapScribe's extras exist) can import this without acquiring a dependency.

Security note: the spec is a CLI value that flows straight into a pip argv.
CodeQL treats argparse values as external input regardless of who launched the
process (CLAUDE.md), so ``resolve_install_spec`` is an allowlist of three
shapes checked at the boundary — the same defensive shape as
``setup_install.validate_selection``.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

#: The historical (and still default) target: an editable install of the
#: checkout the recorder is running from.
CHECKOUT_SPEC = "."

#: A PyPI install must be PINNED. An unpinned ``tapscribe`` would let a Bundle
#: silently float to a newer release than the one it shipped, which is the
#: exact drift the bundled-wheel decision exists to prevent (ADR-0015).
_PINNED_PYPI = re.compile(r"tapscribe==[0-9][0-9A-Za-z.+!-]*")


class InstallSpecError(ValueError):
    """An ``--install-spec`` value that isn't an installable TapScribe."""


def resolve_install_spec(raw: str | None) -> str:
    """Validate an untrusted ``--install-spec`` and return the pip target.

    ``None`` (the default, i.e. nobody passed the flag) means the checkout.
    A ``.whl`` path is absolutised here — the Launcher may pass one relative to
    the install directory while pip runs with a different cwd. A pinned
    ``tapscribe==X.Y.Z`` passes through.

    Anything else raises: an sdist (which would build from source), an
    unrelated distribution, an unpinned requirement, or a string carrying extra
    pip arguments.
    """
    if raw is None:
        return CHECKOUT_SPEC
    spec = raw.strip()
    if spec == CHECKOUT_SPEC:
        return CHECKOUT_SPEC
    if _PINNED_PYPI.fullmatch(spec):
        return spec
    if spec.endswith(".whl"):
        wheel = Path(spec).resolve()
        if not wheel.is_file():
            # Loud beats letting pip report this as an unfindable requirement:
            # a Bundle whose wheel didn't ship is a packaging bug, and the
            # operator needs the path we actually looked for.
            raise InstallSpecError(f"install spec {spec!r} does not exist (looked for {wheel})")
        return str(wheel)
    raise InstallSpecError(
        f"install spec {spec!r} is not recognised — expected '.', a path to a "
        "TapScribe .whl, or a pinned 'tapscribe==X.Y.Z'"
    )


def pip_install_argv(
    extras: list[str],
    *,
    install_spec: str | None = None,
    python: str = sys.executable,
) -> list[str]:
    """``pip install`` argv for this topology, with ``extras`` bracket-grouped.

    ``python`` defaults to the running interpreter so the install lands in
    whichever venv is executing — the bundled one in a Bundle, the dev's
    ``.venv`` in a checkout. ``-e`` is emitted only for the checkout; it is
    meaningless against a wheel or a PyPI pin.
    """
    spec = resolve_install_spec(install_spec)
    target = f"{spec}[{','.join(extras)}]" if extras else spec
    editable = ["-e"] if spec == CHECKOUT_SPEC else []
    return [python, "-m", "pip", "install", *editable, target]
