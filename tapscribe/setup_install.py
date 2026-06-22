"""Install resolver for the browser setup surface (the "D" pattern).

The app does NOT resolve pip extras or run pip itself — it delegates to the
dependency-free `tools/install_picker.py`, which already encapsulates the messy
parts (shared extras like `whisper-live`, the auto-appended `cuda-libs`,
per-backend extras, the skip-if-unchanged stamp). The app's job is the
translation seam: turn the catalog-family selection the UI speaks into the
picker's selection (`.tapscribe-install.json` v2), then run the picker
`--non-interactive`, which loads that selection and installs.

Catalog families → picker (install) families
---------------------------------------------
The UI shows catalog families (whisper, nb-whisper, voxtral, parakeet). The
picker installs by BACKEND extra, so several catalog families fold onto one
picker family: Whisper and NB-Whisper both map to the picker's `whisper` family
(NB-Whisper rides its CPU/CUDA faster-whisper backend — it has no MLX). When the
operator picks Whisper-via-MLX *and* NB-Whisper, the picker family resolves to
backend "both" (MLX + faster-whisper), and `resolve_extras` dedups the shared
`whisper-live`. This dedup is exactly what folding here buys.

The picker family/backend key strings below are duplicated as constants (the app
must not import the dependency-free picker at runtime). `tests/test_setup_install.py`
validates every produced selection against the picker's real `resolve_extras`,
so a drift in the picker's model fails the suite rather than shipping.
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_PICKER_SCRIPT = _REPO_ROOT / "tools" / "install_picker.py"
_STATE_FILE = _REPO_ROOT / ".tapscribe-install.json"

# Mirror of tools/install_picker.py's keys (see module docstring on why these
# are duplicated rather than imported; the test pins them to the real picker).
_STATE_VERSION = 2
_BK_CPU, _BK_MLX, _BK_BOTH = "cpu", "mlx", "both"
_PICKER_FAMILIES: tuple[str, ...] = ("whisper", "voxtral", "parakeet")
# catalog family -> picker (install) family
_CATALOG_TO_PICKER: dict[str, str] = {
    "whisper": "whisper",
    "nb-whisper": "whisper",  # rides the whisper family's faster-whisper backend
    "voxtral": "voxtral",
    "parakeet": "parakeet",
}
_KNOWN_FAMILIES = frozenset(_CATALOG_TO_PICKER)
_KNOWN_KINDS = frozenset({"mlx", "cuda", "cpu"})


class InstallSelectionError(ValueError):
    """A setup-install request named an unknown family or backend kind."""


def validate_selection(families: object) -> dict[str, str]:
    """Validate an untrusted request body's family→backend map against the
    allowlists. Family + kind are the only external input that flows toward the
    installer; constraining them here keeps a bogus value from reaching the
    picker / pip (the picker installs from the curated pyproject extras only)."""
    if not isinstance(families, dict):
        raise InstallSelectionError("families must be an object of {family: backend}")
    out: dict[str, str] = {}
    for family, kind in families.items():
        if family not in _KNOWN_FAMILIES:
            raise InstallSelectionError(f"unknown family {family!r}")
        if kind not in _KNOWN_KINDS:
            raise InstallSelectionError(f"unknown backend {kind!r} for family {family!r}")
        out[family] = kind
    return out


def to_picker_state(selection: dict[str, str]) -> dict:
    """Translate a catalog-family selection into the picker's v2 state dict.

    `selection` maps a catalog family to the chosen backend kind
    (``"mlx" | "cuda" | "cpu"`` — the host-valid kind the UI resolved). "cuda"
    and "cpu" both ride the picker's CPU/CUDA (faster-whisper / transformers)
    backend; "mlx" rides the MLX backend. Unknown families are ignored.

    Assumes `selection` has passed `validate_selection`: an unrecognised kind
    falls through to the CPU backend rather than raising, so callers must
    validate first (the HTTP endpoint does).
    """
    # Collect the picker backends each picker family needs, then merge.
    per_family: dict[str, set[str]] = {}
    for catalog_family, kind in selection.items():
        picker_family = _CATALOG_TO_PICKER.get(catalog_family)
        if picker_family is None:
            continue  # unknown family — ignore rather than fabricate an install
        picker_backend = _BK_MLX if kind == _BK_MLX else _BK_CPU
        per_family.setdefault(picker_family, set()).add(picker_backend)

    choices: dict[str, dict] = {}
    for family in _PICKER_FAMILIES:
        backends = per_family.get(family)
        if not backends:
            choices[family] = {"enabled": False, "backend": _BK_CPU}
        elif _BK_MLX in backends and _BK_CPU in backends:
            choices[family] = {"enabled": True, "backend": _BK_BOTH}
        elif _BK_MLX in backends:
            choices[family] = {"enabled": True, "backend": _BK_MLX}
        else:
            choices[family] = {"enabled": True, "backend": _BK_CPU}
    return {"version": _STATE_VERSION, "choices": choices}


def write_picker_state(state: dict, *, path: Path = _STATE_FILE) -> None:
    """Persist the picker state where `install_picker --non-interactive` reads
    it. Matches the picker's own `Selection.save` formatting."""
    path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


def picker_install_argv(*, python: str = sys.executable, no_mlx: bool = False) -> list[str]:
    """argv to run the install picker non-interactively against the written
    selection. The picker resolves extras, runs pip, and writes the stamp."""
    argv = [python, str(_PICKER_SCRIPT), "--non-interactive"]
    if no_mlx:
        argv.append("--no-mlx")
    return argv


def sse(event: dict) -> str:
    """Frame an event dict as a Server-Sent Events `data:` block."""
    return f"data: {json.dumps(event)}\n\n"


async def _create_subprocess(argv: list[str]):
    """Default spawn: run argv with stdout+stderr merged into one line stream.
    Returns an asyncio subprocess whose `.stdout` is an async line iterator and
    whose `.wait()` yields the return code. Patched out in tests."""
    return await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )


async def run_install(
    selection: dict[str, str],
    *,
    no_mlx: bool = False,
    spawn: Callable[[list[str]], Awaitable] | None = None,
    write_state: Callable[..., None] | None = None,
    on_success: Callable[[], None] | None = None,
) -> AsyncIterator[dict]:
    """Write the resolved picker selection, run the picker `--non-interactive`,
    and yield progress events: one ``{"phase":"start"}``, a ``{"phase":"log"}``
    per output line, then ``{"phase":"done"}`` (returncode 0) or
    ``{"phase":"error"}``. `on_success` (hot-reload) fires only on success.

    `spawn` / `write_state` are injectable for tests; they default to the real
    asyncio subprocess and the on-disk picker-state writer at call time.
    """
    spawn = spawn or _create_subprocess
    write_state = write_state or write_picker_state

    yield {"phase": "start"}
    try:
        write_state(to_picker_state(selection))
        proc = await spawn(picker_install_argv(no_mlx=no_mlx))
        async for raw in proc.stdout:
            line = raw.decode("utf-8", "replace").rstrip("\n")
            yield {"phase": "log", "line": line}
        returncode = await proc.wait()
    except Exception as exc:  # noqa: BLE001 — headers are already sent, so surface ANY write/spawn/stream failure as a terminal error event rather than a truncated stream + 500
        yield {"phase": "error", "ok": False, "returncode": None, "message": str(exc)}
        return

    if returncode != 0:
        yield {"phase": "error", "ok": False, "returncode": returncode}
        return

    # Install succeeded. Hot-reload is best-effort — a failure refreshing the
    # probes must NOT swallow the terminal `done` (the install itself worked);
    # surface it as a log note and still finish.
    if on_success is not None:
        try:
            on_success()
        except Exception as exc:  # noqa: BLE001 — install already succeeded; report + continue
            yield {"phase": "log", "line": f"· note: backend refresh failed ({exc}); a restart may be needed"}
    yield {"phase": "done", "ok": True, "returncode": 0}
