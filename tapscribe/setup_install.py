"""Install resolver for the browser setup surface (GET /setup).

The app does NOT resolve pip extras or run pip itself — it delegates to the
dependency-free `tapscribe/install_picker.py`, which already encapsulates the messy
parts (shared/live extras like `whisper-live`, the auto-appended `cuda-libs`,
per-backend extras, the skip-if-unchanged stamp). The app's job is the
translation seam: turn the catalog-family selection the UI speaks — plus the
top-level `live` flag (the WhisperLiveKit live-caption channel opt-out, #374;
default True, unchanged from before the flag existed) — into the picker's
selection (`.tapscribe-install.json` v2), then run the picker
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

from . import config

# Spawned as `python -m <this>`, never imported — see the module docstring.
_PICKER_MODULE = "tapscribe.install_picker"

# The saved selection lives with the operator's DATA, not beside the package:
# in a wheel install (the Bundle topology) the package's parent is
# `site-packages`, where a selection would be at the mercy of the next
# reinstall. `BASE_DIR` is the repo root in a checkout, so this is byte-for-byte
# the path devs already had (ADR-0015).
_STATE_FILE = config.BASE_DIR / ".tapscribe-install.json"

# Mirror of tapscribe/install_picker.py's keys (see module docstring on why these
# are duplicated rather than imported; the test pins them to the real picker).
_STATE_VERSION = 2
_BK_CPU, _BK_MLX, _BK_BOTH = "cpu", "mlx", "both"
# The picker families /setup MANAGES. Deliberately a SUBSET of the picker's own
# `install_picker.FAMILIES`: `moonshine` is live-only (no catalog family, no
# /setup row — see setup_state.FAMILY_META), so /setup has no opinion about it
# and must not express one. That is exactly why `write_picker_state` MERGES
# rather than replaces: a wholesale rewrite dropped the `moonshine` key, the
# picker read the absence back as `enabled=False`, and its next `Selection.save`
# re-persisted that — permanently losing an operator's Moonshine choice (pip
# doesn't uninstall, so it kept working until the venv was rebuilt).
# `test_write_picker_state_preserves_families_setup_does_not_manage` pins this.
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


def validate_live(value: object) -> bool:
    """Validate the request body's top-level `live` field (the WhisperLiveKit
    live-caption channel opt-out, #374).

    Absent (``None``, the field omitted from the body) defaults True — a
    fresh install and an existing request that predates this flag both keep
    the live channel ON, matching `install_picker.FamilyChoice.live`'s own
    default. Present but non-bool is rejected rather than coerced, same
    stance as `validate_selection` on an unknown family/kind: a malformed
    value must not silently pick an interpretation the operator didn't ask
    for.
    """
    if value is None:
        return True
    if not isinstance(value, bool):
        raise InstallSelectionError(f"live must be a boolean, got {value!r}")
    return value


def to_picker_state(selection: dict[str, str], *, live: bool = True) -> dict:
    """Translate a catalog-family selection into the picker's v2 state dict.

    `selection` maps a catalog family to the chosen backend kind
    (``"mlx" | "cuda" | "cpu"`` — the host-valid kind the UI resolved). "cuda"
    and "cpu" both ride the picker's CPU/CUDA (faster-whisper / transformers)
    backend; "mlx" rides the MLX backend. Unknown families are ignored.

    `live` (default True) is the WhisperLiveKit live-caption channel opt-out
    (#374) — it's a per-request flag, not per-family, because /setup only
    offers it on the one row (Whisper) that has a live channel. It's written
    onto the picker's `whisper` choice regardless of whether Whisper/NB-Whisper
    was selected THIS request: the picker's `FamilyChoice.live` only matters
    while the family is also `enabled`, so persisting it unconditionally is
    harmless and keeps the operator's toggle sticky across an install that
    happens not to touch Whisper.

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
    # Only Whisper declares `live_extras` in the picker's catalog — see
    # install_picker.FamilyDef. "whisper" is always a key in `choices` (it's
    # in `_PICKER_FAMILIES`), so this is never a KeyError.
    choices["whisper"]["live"] = live
    return {"version": _STATE_VERSION, "choices": choices}


def read_picker_state(path: Path | None = None) -> dict:
    """Best-effort read of the picker's on-disk selection. An absent,
    unreadable, non-JSON or non-object file yields `{}` — the same
    "fall back to nothing preserved" stance `install_picker.Selection.load`
    takes, so a corrupt file can't fail a /setup install.

    `path` defaults to `_STATE_FILE`, looked up FRESH on each call (not
    bound as the parameter's default value) so `monkeypatch.setattr(module,
    "_STATE_FILE", ...)` reaches every caller that omits `path` — including
    `setup_state.build_setup_state`'s live-channel read, called from the real
    `/api/setup/state` route with no override of its own. A bound default
    (`path: Path = _STATE_FILE`) would freeze the value at import time and
    silently ignore that monkeypatch, the same trap `config.INSTALL_WARNINGS_FILE`
    avoids by being re-read from the module each call.
    """
    if path is None:
        path = _STATE_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def merge_picker_state(existing: object, fresh: dict) -> dict:
    """Overlay `fresh` (the families /setup manages) onto `existing` (what
    the terminal picker last wrote), keeping every OTHER family's choice.

    Pure, so the preservation rule is testable without touching disk. Only
    `choices` merges; the rest of `fresh` (currently just `version`) wins
    outright, since /setup writes the schema version it speaks.
    """
    out = dict(fresh)
    fresh_choices = fresh.get("choices")
    if not isinstance(fresh_choices, dict):
        return out
    prior = existing.get("choices") if isinstance(existing, dict) else None
    if not isinstance(prior, dict):
        return out
    # Fresh wins per family; key order isn't observable (the state file is
    # written with sort_keys=True).
    out["choices"] = {**prior, **fresh_choices}
    return out


def write_picker_state(state: dict, *, path: Path = _STATE_FILE) -> None:
    """Persist the picker state where `install_picker --non-interactive` reads
    it, MERGED over whatever is already there (see `merge_picker_state` and
    the `_PICKER_FAMILIES` note). Matches the picker's own `Selection.save`
    formatting."""
    merged = merge_picker_state(read_picker_state(path), state)
    path.write_text(json.dumps(merged, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def picker_install_argv(
    *,
    python: str = sys.executable,
    no_mlx: bool = False,
    install_spec: str | None = None,
) -> list[str]:
    """argv to run the install picker non-interactively against the written
    selection. The picker resolves extras, runs pip, and writes the stamp.

    Invoked as `-m tapscribe.install_picker` rather than by script path: a
    Bundle installs a wheel into a venv, where no repo-relative
    `tapscribe/install_picker.py` exists, and `-m` resolves wherever the package
    actually landed (ADR-0015).

    `install_spec` forwards the Bundle's wheel path so the subprocess installs
    from the SAME wheel the installer shipped. Omitted by default — absent flag
    means the checkout topology, which is what a dev launching `start.ps1`
    without the installer must keep getting.
    """
    argv = [python, "-m", _PICKER_MODULE, "--non-interactive", "--state-file", str(_STATE_FILE)]
    if no_mlx:
        argv.append("--no-mlx")
    if install_spec is not None:
        argv += ["--install-spec", install_spec]
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
    live: bool = True,
    no_mlx: bool = False,
    install_spec: str | None = None,
    spawn: Callable[[list[str]], Awaitable] | None = None,
    write_state: Callable[..., None] | None = None,
    on_success: Callable[[], None] | None = None,
) -> AsyncIterator[dict]:
    """Write the resolved picker selection, run the picker `--non-interactive`,
    and yield progress events: one ``{"phase":"start"}``, a ``{"phase":"log"}``
    per output line, then ``{"phase":"done"}`` (returncode 0) or
    ``{"phase":"error"}``. `on_success` (hot-reload) fires only on success.

    `install_spec` is the recorder's `--install-spec` (ADR-0015), forwarded so a
    Bundle installs extras from the wheel it shipped instead of an editable
    checkout that isn't there. `None` (a dev checkout) keeps the historical argv.

    `live` (default True) is the WhisperLiveKit live-caption channel opt-out
    (#374), forwarded to `to_picker_state`.

    `spawn` / `write_state` are injectable for tests; they default to the real
    asyncio subprocess and the on-disk picker-state writer at call time.
    """
    spawn = spawn or _create_subprocess
    write_state = write_state or write_picker_state

    yield {"phase": "start"}
    try:
        write_state(to_picker_state(selection, live=live))
        proc = await spawn(picker_install_argv(no_mlx=no_mlx, install_spec=install_spec))
        async for raw in proc.stdout:
            line = raw.decode("utf-8", "replace").rstrip("\n")
            yield {"phase": "log", "line": line}
        returncode = await proc.wait()
    except Exception as exc:  # noqa: BLE001 — headers are already sent, so surface ANY write/spawn/stream failure as a terminal error event rather than a truncated stream + 500
        # Log the detail server-side; do NOT stream the exception text to the
        # client (CodeQL py/stack-trace-exposure). pip's own output, if it ran,
        # already streamed as `log` events.
        print(f"[tapscribe] setup-install failed: {exc!r}", flush=True)
        yield {
            "phase": "error",
            "ok": False,
            "returncode": None,
            "message": "install failed — check the server logs",
        }
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
        except Exception as exc:  # noqa: BLE001 — install already succeeded; log + continue
            print(f"[tapscribe] setup-install backend refresh failed: {exc!r}", flush=True)
            yield {"phase": "log", "line": "· note: backend refresh failed; a restart may be needed"}
    yield {"phase": "done", "ok": True, "returncode": 0}
