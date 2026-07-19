"""TapScribe install picker — interactive selection of model families AND
backends to install, persisted across runs.

Called by `start.sh` / `start.ps1` after the venv exists and pip has been
upgraded. For each model family (Whisper / Voxtral / Parakeet / Moonshine)
the operator picks:

  - whether to install it at all (Space toggle), AND
  - which runtime backend to install:
      • CPU/CUDA  — torch / faster-whisper / transformers (auto-uses CUDA when
        nvidia-smi reports a device, else CPU). For the faster-whisper
        path specifically, CTranslate2 doesn't bundle cuBLAS/cuDNN, so
        the `cuda-libs` extra (nvidia-cublas-cu12 / nvidia-cudnn-cu12) is
        added automatically when CUDA is detected — the Torch-based
        backends get CUDA from Torch's own bundle and don't need it.
      • MLX       — Apple Silicon GPU (only offered on Darwin/arm64)
      • Both      — install both so the catalog can switch at runtime

Backend choices map to per-family atomic extras in `pyproject.toml`
(e.g. `whisper-cpu`, `whisper-mlx`, plus a shared `whisper-live` for
the live-socket server). The picker composes the final `pip install`
argv from those atoms, delegating the install TARGET (checkout / Bundle
wheel / pinned PyPI) to `tapscribe.install_target` — see ADR-0015.

Why this lives in the package rather than `tools/`
--------------------------------------------------
`tools/` is not shipped in the wheel (`packages.find` includes
`tapscribe*` only), and a Bundle installs a wheel — so a picker under
`tools/` is unreachable in every non-checkout topology. It moved here to
be reachable, NOT to become part of the app's import surface: the app
still only ever runs it as a subprocess (`setup_install.py`). Importing
`tapscribe` costs nothing (`__init__.py` is a docstring and
`__version__`), so living in the package doesn't compromise the
stdlib-only rule below. `tests/test_install_picker_not_imported_by_app.py`
pins the never-imported half.

To keep re-launches quiet, the picker stamps each successful install
(resolved extras + a pyproject.toml fingerprint) inside the venv and
skips pip on the next run when neither changed — so an unchanged
selection no longer triggers the editable package's
uninstall/rebuild/reinstall churn.

Stdlib-only by design: this file runs before the operator has agreed to
install anything, and certainly before TapScribe's runtime deps exist in
the venv. Don't import third-party modules here.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess  # nosec B404 — we run pip with a fixed argv list, no shell.
import sys
from dataclasses import dataclass, field
from pathlib import Path

# Stdlib-only sibling (see the module docstring): `install_target` imports
# nothing beyond re/sys/pathlib, so this doesn't break the pre-install rule.
from tapscribe import install_target

REPO_ROOT = Path(__file__).resolve().parent.parent
STATE_FILE = REPO_ROOT / ".tapscribe-install.json"
STATE_VERSION = 2

# Sidecar next to the state file recording families the picker had to SKIP
# because their saved backend is gone from this version's catalog. A separate
# file rather than a key inside the state file: the state file has a versioned
# schema with a v1 migration path, and this is a derived observation about the
# last run, not part of the operator's selection.
#
# It exists because `removed_backend_families()` only ever warned to stderr,
# which in a Bundle goes to a log file nobody opens (ADR-0015) — the operator
# upgrades, their models quietly stop installing, and nothing they'd look at
# says so. `setup_state.build_setup_state` reads this into /api/setup/state so
# /setup can say it out loud.
WARNINGS_FILENAME = ".tapscribe-install-warnings.json"

# Install stamp: records the extras + pyproject fingerprint of the last
# successful `pip install`, so a re-run with an unchanged selection skips
# pip instead of triggering the editable package's noisy
# uninstall/rebuild/reinstall churn (which `pip install -e` does on EVERY
# invocation, even when nothing changed). Kept inside the venv
# (`sys.prefix`) rather than next to STATE_FILE so that blowing away
# `.venv` naturally invalidates it and forces a fresh install.
STAMP_FILE = Path(sys.prefix) / ".tapscribe-install-stamp.json"

# Backend key sentinels — used both in the FamilyDef declarations and in
# persisted state, so spelling matters.
BACKEND_CPU = "cpu"
BACKEND_MLX = "mlx"
BACKEND_BOTH = "both"

# CUDA runtime libs (cuBLAS/cuDNN) for the faster-whisper / CTranslate2 GPU
# path. CTranslate2 doesn't bundle them (unlike Torch's own backends), so
# `resolve_extras` appends this extra automatically when CUDA is detected
# AND the Whisper CPU/CUDA atom (`whisper-cpu`) is in the install. See the
# `cuda-libs` extra in pyproject.toml.
CUDA_RUNTIME_EXTRA = "cuda-libs"

# The faster-whisper / CTranslate2 atom. Its presence in the resolved set is
# what gates CUDA_RUNTIME_EXTRA — it's only there when the Whisper family is
# enabled with a CPU/CUDA backend on a non-Apple box.
WHISPER_CPU_EXTRA = "whisper-cpu"


# ---------------------------------------------------------------------------
# Family + backend catalog.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BackendDef:
    """One per-machine backend choice within a family.

    `extras` are the pyproject extras to install when this backend is
    picked. `key` is one of the BACKEND_* sentinels above and is what
    gets persisted in `.tapscribe-install.json`.
    """

    key: str
    label: str  # e.g. "CPU/CUDA", "MLX"
    extras: tuple[str, ...]


@dataclass(frozen=True)
class FamilyDef:
    """One row in the picker.

    `shared_extras` get installed regardless of which backend the
    operator chose (e.g. whisperlivekit is needed for the live socket
    server on both CPU and MLX). `backends` lists the optional runtimes
    in machine-natural order — CPU first, MLX second — and the picker
    filters them down to whatever's actually available on the host.
    """

    key: str
    label: str
    description: str
    size_hint: str
    backends: tuple[BackendDef, ...]
    shared_extras: tuple[str, ...] = ()
    default_selected: bool = False

    def has_mlx(self) -> bool:
        return self.declares_backend(BACKEND_MLX)

    def declares_backend(self, backend_key: str) -> bool:
        """True iff this family currently declares `backend_key`.
        `BACKEND_BOTH` is a virtual key — valid only when ≥2 atomic
        backends are declared."""
        if backend_key == BACKEND_BOTH:
            return len(self.backends) >= 2
        return any(b.key == backend_key for b in self.backends)


FAMILIES: tuple[FamilyDef, ...] = (
    FamilyDef(
        key="whisper",
        label="Whisper / NB-Whisper",
        description=(
            "OpenAI Whisper + NB-AiLab's Norwegian-tuned variants. "
            "Main batch backend; also drives the live caption channel. "
            "Recommended baseline."
        ),
        size_hint="~150 MB CPU / ~80 MB MLX",
        # whisperlivekit is the live-socket server — the recorder spawns
        # it whether MLX or faster-whisper drives transcription, so it's
        # shared between both backends.
        shared_extras=("whisper-live",),
        backends=(
            BackendDef(key=BACKEND_CPU, label="CPU/CUDA", extras=("whisper-cpu",)),
            BackendDef(key=BACKEND_MLX, label="MLX", extras=("whisper-mlx",)),
        ),
        default_selected=True,
    ),
    FamilyDef(
        key="voxtral",
        label="Voxtral (Mistral)",
        description=("Mistral Voxtral 3B audio LLM. Batch + live. Pulls PyTorch + transformers."),
        size_hint="~2 GB",
        backends=(BackendDef(key=BACKEND_CPU, label="CPU/CUDA", extras=("voxtral-cpu",)),),
    ),
    FamilyDef(
        key="parakeet",
        label="Parakeet (NVIDIA)",
        description=("NVIDIA Parakeet TDT 0.6B v3 — 25 EU langs, top of HF Open ASR. Batch only."),
        size_hint="~1.5 GB CPU / ~2.5 GB MLX",
        backends=(
            BackendDef(key=BACKEND_CPU, label="CPU/CUDA", extras=("parakeet-cpu",)),
            BackendDef(key=BACKEND_MLX, label="MLX", extras=("parakeet-mlx",)),
        ),
    ),
    FamilyDef(
        key="moonshine",
        label="Moonshine (Useful Sensors)",
        description=(
            "Lightweight, low-latency live-captioning engine. English only. Live only "
            "(no batch adapter) — see MoonshineLiveChannel."
        ),
        size_hint="~200 MB CPU (onnxruntime) / ~50 MB MLX",
        backends=(
            BackendDef(key=BACKEND_CPU, label="CPU", extras=("moonshine-cpu",)),
            BackendDef(key=BACKEND_MLX, label="MLX", extras=("moonshine-mlx",)),
        ),
    ),
)


# ---------------------------------------------------------------------------
# Machine capability detection.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MachineCaps:
    """What `start.sh`/`.ps1`-equivalent introspection turns up. Drives
    both the human-readable header AND which backend choices the picker
    surfaces per family."""

    os_name: str  # "Darwin" / "Linux" / "Windows"
    arch: str  # "arm64" / "x86_64" / etc.
    mlx: bool  # Apple Silicon — MLX backends become selectable
    cuda: bool  # `nvidia-smi` exit zero — best-effort signal


def detect_caps(*, force_no_mlx: bool = False) -> MachineCaps:
    """Probe the host. CUDA detection is `nvidia-smi`-based since we have
    no torch yet at picker time; it'll false-negative on machines with
    a GPU but no driver tooling, which is the right way to err — the
    operator can still pick CPU/CUDA explicitly."""
    os_name = platform.system() or "unknown"
    arch = platform.machine() or "unknown"
    mlx = (not force_no_mlx) and os_name == "Darwin" and arch == "arm64"
    cuda = False
    nvidia_smi = _which("nvidia-smi")
    if nvidia_smi is not None:
        try:
            res = subprocess.run(  # nosec B603 — explicit argv, absolute path resolved by _which.
                [nvidia_smi, "-L"], capture_output=True, timeout=4, check=False
            )
            cuda = res.returncode == 0 and bool(res.stdout.strip())
        except (OSError, subprocess.TimeoutExpired):
            cuda = False
    return MachineCaps(os_name=os_name, arch=arch, mlx=mlx, cuda=cuda)


def _which(cmd: str) -> str | None:
    """`shutil.which` wrapper that returns None if missing — kept separate
    so tests can monkeypatch detection without touching PATH."""
    from shutil import which

    return which(cmd)


def _backend_available(backend: BackendDef, caps: MachineCaps) -> bool:
    """Whether this machine can install this backend. CPU/CUDA is always
    available; MLX needs Apple Silicon."""
    if backend.key == BACKEND_MLX:
        return caps.mlx
    return True


def available_backends(fam: FamilyDef, caps: MachineCaps) -> list[BackendDef]:
    """Backends a family can offer on this machine, in declaration order."""
    return [b for b in fam.backends if _backend_available(b, caps)]


def natural_backend_key(fam: FamilyDef, caps: MachineCaps) -> str:
    """The default backend choice on first install / when the saved
    choice is unavailable. Apple Silicon → MLX where the family supports
    it; everywhere else → CPU."""
    if caps.mlx and fam.has_mlx():
        return BACKEND_MLX
    return BACKEND_CPU


def cycleable_backend_keys(fam: FamilyDef, caps: MachineCaps) -> list[str]:
    """The values ←/→ will rotate through for this family on this
    machine. When only one backend is available we don't offer a "Both"
    cycle option — there's nothing to combine."""
    avail = [b.key for b in available_backends(fam, caps)]
    if len(avail) >= 2:
        return [*avail, BACKEND_BOTH]
    return avail


# ---------------------------------------------------------------------------
# Persistence.
# ---------------------------------------------------------------------------


@dataclass
class FamilyChoice:
    """One row's persisted state. Stale backend values (e.g. "mlx"
    loaded on a Linux box) are silently downgraded at install-time, not
    on load — so moving the checkout back to Apple Silicon restores the
    MLX preference instead of losing it."""

    enabled: bool = False
    backend: str = BACKEND_CPU


@dataclass
class Selection:
    """The operator's choices, persisted across runs."""

    choices: dict[str, FamilyChoice] = field(default_factory=dict)

    def for_family(self, family_key: str) -> FamilyChoice:
        """Return (and lazily create) the choice for `family_key`. Mutating
        the returned object mutates the Selection."""
        return self.choices.setdefault(family_key, FamilyChoice())

    @classmethod
    def defaults_for(cls, caps: MachineCaps) -> Selection:
        out = cls()
        for fam in FAMILIES:
            out.choices[fam.key] = FamilyChoice(
                enabled=fam.default_selected,
                backend=natural_backend_key(fam, caps),
            )
        return out

    @classmethod
    def load(cls, path: Path, caps: MachineCaps) -> Selection:
        if not path.exists():
            return cls.defaults_for(caps)
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return cls.defaults_for(caps)
        if isinstance(data, dict) and data.get("version") == STATE_VERSION:
            return cls._load_v2(data, caps)
        # Older "families: [...]" format — migrate it onto v2 with the
        # natural backend for the current machine. Operators who picked
        # Whisper on Apple Silicon under v1 always got the "both"
        # behaviour, so preserve that.
        if isinstance(data, dict) and "families" in data:
            return cls._load_v1(data, caps)
        return cls.defaults_for(caps)

    @classmethod
    def _load_v1(cls, data: dict, caps: MachineCaps) -> Selection:
        raw = data.get("families", [])
        known_keys = {f.key for f in FAMILIES}
        old_enabled = {k for k in raw if k in known_keys}
        out = cls()
        for fam in FAMILIES:
            on = fam.key in old_enabled
            # v1 always installed both atomic backends on Apple Silicon
            # when a family was ticked — preserve that explicitly so the
            # migration doesn't silently shrink the install.
            if on and caps.mlx and fam.has_mlx():
                backend = BACKEND_BOTH
            else:
                backend = natural_backend_key(fam, caps)
            out.choices[fam.key] = FamilyChoice(enabled=on, backend=backend)
        return out

    @classmethod
    def _load_v2(cls, data: dict, caps: MachineCaps) -> Selection:
        choices_raw = data.get("choices", {}) or {}
        out = cls()
        for fam in FAMILIES:
            raw = choices_raw.get(fam.key, {}) or {}
            enabled = bool(raw.get("enabled", False))
            backend = raw.get("backend", "")
            if backend not in (BACKEND_CPU, BACKEND_MLX, BACKEND_BOTH):
                backend = natural_backend_key(fam, caps)
            out.choices[fam.key] = FamilyChoice(enabled=enabled, backend=backend)
        return out

    def removed_backend_families(self) -> list[FamilyDef]:
        """Enabled families whose saved backend isn't in the catalog
        anymore (a PR removed it). Surfaced by the picker so the
        operator re-picks instead of silently inheriting a heavy
        alternative."""
        out: list[FamilyDef] = []
        for fam in FAMILIES:
            choice = self.choices.get(fam.key)
            if not choice or not choice.enabled:
                continue
            if not fam.declares_backend(choice.backend):
                out.append(fam)
        return out

    def save(self, path: Path) -> None:
        body = {
            "version": STATE_VERSION,
            "choices": {
                fam.key: {
                    "enabled": self.choices.get(fam.key, FamilyChoice()).enabled,
                    "backend": self.choices.get(fam.key, FamilyChoice()).backend,
                }
                for fam in FAMILIES
            },
        }
        path.write_text(json.dumps(body, indent=2, sort_keys=True) + "\n")


# ---------------------------------------------------------------------------
# Extras resolution — selection × machine caps → pip extras list.
# ---------------------------------------------------------------------------


def effective_backends(fam: FamilyDef, choice: FamilyChoice, caps: MachineCaps) -> list[BackendDef]:
    """Filter the operator's requested backend down to what this machine
    can actually install.

    Two failure shapes when the requested backend isn't in `available_backends`:

    1. The backend exists in the family's catalog but this host can't
       run it (e.g. saved MLX choice opened on a Linux box). Silently
       downgrade to the first available backend so the install still
       produces something usable.
    2. The backend is no longer in the family's catalog at all
       (`canary-mlx` removal in PR #61 motivated this case). Returning
       the other backend here would drag in a heavy alternative the
       operator didn't choose, so return `[]` and let the renderer /
       main loop surface the situation.
    """
    avail = available_backends(fam, caps)
    if not avail:
        return []
    if choice.backend == BACKEND_BOTH:
        return avail
    matched = next((b for b in avail if b.key == choice.backend), None)
    if matched is not None:
        return [matched]
    if not fam.declares_backend(choice.backend):
        return []
    return [avail[0]]


def resolve_extras(selection: Selection, caps: MachineCaps) -> list[str]:
    """Turn the selection into the pyproject extras to install. Order is
    stable (family-declaration × shared-then-backends) so the install
    command is reproducible across runs."""
    out: list[str] = []
    seen: set[str] = set()

    def add(extra: str) -> None:
        if extra not in seen:
            out.append(extra)
            seen.add(extra)

    for fam in FAMILIES:
        choice = selection.choices.get(fam.key)
        if not choice or not choice.enabled:
            continue
        backends = effective_backends(fam, choice, caps)
        if not backends:
            continue
        for extra in fam.shared_extras:
            add(extra)
        for be in backends:
            for extra in be.extras:
                add(extra)

    # CTranslate2 (the faster-whisper backend) doesn't bundle cuBLAS/cuDNN,
    # so the Whisper CPU/CUDA path can't drive the GPU without them. Append
    # them iff CUDA was detected AND that path is actually in the install
    # (`whisper-cpu` present). Tied to whisper-cpu specifically: the
    # Torch-based backends (Voxtral / Parakeet via transformers) get CUDA
    # from Torch's own bundle, and MLX-only selections aren't on CUDA at all.
    if caps.cuda and WHISPER_CPU_EXTRA in seen:
        add(CUDA_RUNTIME_EXTRA)
    return out


def family_extras_preview(fam: FamilyDef, choice: FamilyChoice, caps: MachineCaps) -> list[str]:
    """The extras this single family contributes given a choice + caps —
    used by the renderer to show operators which `[...]` tokens map to
    which row."""
    out: list[str] = []
    seen: set[str] = set()
    for extra in fam.shared_extras:
        if extra not in seen:
            out.append(extra)
            seen.add(extra)
    for be in effective_backends(fam, choice, caps):
        for extra in be.extras:
            if extra not in seen:
                out.append(extra)
                seen.add(extra)
    return out


# ---------------------------------------------------------------------------
# Rendering.
# ---------------------------------------------------------------------------


def _format_caps_line(caps: MachineCaps) -> str:
    parts = [f"{caps.os_name} {caps.arch}"]
    if caps.mlx:
        parts.append("MLX detected")
    if caps.cuda:
        parts.append("CUDA detected")
    if not caps.mlx and not caps.cuda:
        parts.append("CPU only")
    return " · ".join(parts)


def _render_backend_row(fam: FamilyDef, choice: FamilyChoice, caps: MachineCaps) -> str:
    """One-line summary of the backend selector for `fam`.

    Cases:
      • only CPU/CUDA available → "Backend: CPU/CUDA (only option)"
      • CPU+MLX available, choice = cpu/mlx/both → radio row
      • no backends (shouldn't happen) → explanatory fallback
    """
    avail = available_backends(fam, caps)
    if not avail:
        return "Backend: (none available on this machine)"
    if len(avail) == 1:
        return f"Backend: {avail[0].label} (only option on this machine)"

    cuda_hint = " (CUDA via torch)" if caps.cuda else ""
    parts: list[str] = []
    for be in avail:
        marker = "●" if choice.backend == be.key else "○"
        label = be.label
        if be.key == BACKEND_CPU and cuda_hint:
            label = f"{label}{cuda_hint}"
        parts.append(f"{marker} {label}")
    both_marker = "●" if choice.backend == BACKEND_BOTH else "○"
    parts.append(f"{both_marker} Both")
    return "Backend: " + "   ".join(parts)


def render(selection: Selection, caps: MachineCaps, *, cursor: int | None = None) -> str:
    """Return the picker as a string. Pure — easy to snapshot in tests.

    When `cursor` is set, the corresponding row is highlighted with a
    `>` marker and the help block describes arrow-key navigation;
    when `cursor` is None, we render the numbered-fallback help."""
    lines: list[str] = []
    lines.append("TapScribe install picker")
    lines.append("=" * 60)
    lines.append(f"Machine: {_format_caps_line(caps)}")
    lines.append("")
    lines.append("Model families to install:")
    for idx, fam in enumerate(FAMILIES, start=1):
        choice = selection.choices.get(fam.key) or FamilyChoice()
        mark = "x" if choice.enabled else " "
        arrow = ">" if cursor is not None and cursor == idx - 1 else " "
        lines.append(f"{arrow} [{mark}] {idx}. {fam.label}  ({fam.size_hint})")
        lines.append(f"          {fam.description}")
        lines.append(f"          {_render_backend_row(fam, choice, caps)}")
        if choice.enabled:
            extras = family_extras_preview(fam, choice, caps)
            if extras:
                lines.append(f"          Installs: [{', '.join(extras)}]")
            elif not fam.declares_backend(choice.backend):
                lines.append(
                    f"          Installs: (nothing — the '{choice.backend}' backend was removed "
                    "in a recent update; re-pick to confirm a fallback)"
                )
            else:
                lines.append("          Installs: (nothing — no backend available)")
        else:
            lines.append("          (not selected)")
        lines.append("")
    extras = resolve_extras(selection, caps)
    if extras:
        lines.append(f"Will install:  pip install -e '.[{','.join(extras)}]'")
        if CUDA_RUNTIME_EXTRA in extras:
            lines.append(
                "               + CUDA runtime libs (nvidia-cublas-cu12, "
                "nvidia-cudnn-cu12) — faster-whisper GPU"
            )
    else:
        lines.append("Will install:  (nothing — base package only; the dashboard will be empty)")
    lines.append("")
    if cursor is None:
        lines.append("Commands:")
        lines.append("  <numbers>   toggle items (e.g. '2,4' or '2 4')")
        lines.append("  a           toggle all")
        lines.append("  r           reset to defaults")
        lines.append("  Enter       confirm and install")
        lines.append("  q           quit without launching")
        lines.append("  (backend choice needs the arrow-key UI — re-run on a real TTY to switch)")
    else:
        lines.append("↑/↓ row · ←/→ backend · Space toggle · a all · r reset · Enter install · q quit")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Numbered (fallback) interactive loop. Used when stdin/stdout isn't a real
# TTY (CI, piped, tests with StringIO) so the picker still works without
# raw-mode keyboard input. Backend choice is NOT toggleable here — operators
# in piped contexts just get whatever they picked last or the machine
# default, since there's no clean line-grammar for "set row 1's backend".
# ---------------------------------------------------------------------------


def _parse_command(raw: str, selection: Selection) -> str:
    """Mutate `selection` in place according to `raw`. Returns a status
    string the caller prints between renders. `""` means "confirm";
    `"quit"` means "abort"."""
    cmd = raw.strip().lower()
    if cmd == "":
        return ""
    if cmd in ("q", "quit", "exit"):
        return "quit"
    if cmd == "a":
        # Toggle all enable flags. Backend choices are left as-is.
        all_on = all(selection.choices.get(f.key, FamilyChoice()).enabled for f in FAMILIES)
        for f in FAMILIES:
            selection.for_family(f.key).enabled = not all_on
        return "toggled all"
    if cmd == "r":
        for f in FAMILIES:
            selection.for_family(f.key).enabled = f.default_selected
        return "reset to defaults"
    tokens = [t for t in cmd.replace(",", " ").split() if t]
    toggled: list[str] = []
    bad: list[str] = []
    for tok in tokens:
        try:
            n = int(tok)
        except ValueError:
            bad.append(tok)
            continue
        if not 1 <= n <= len(FAMILIES):
            bad.append(tok)
            continue
        fam = FAMILIES[n - 1]
        ch = selection.for_family(fam.key)
        ch.enabled = not ch.enabled
        toggled.append(fam.label)
    parts: list[str] = []
    if toggled:
        parts.append(f"toggled: {', '.join(toggled)}")
    if bad:
        parts.append(f"ignored unknown: {', '.join(bad)}")
    return " · ".join(parts) if parts else "(no change)"


def _numbered_loop(selection: Selection, caps: MachineCaps, *, stream_in, stream_out) -> bool:
    """The line-buffered numbered picker. Kept as the fallback for non-TTY
    contexts — CI, piped invocations, and the unit tests (which pass
    StringIO)."""
    status: str | None = None
    while True:
        print(render(selection, caps), file=stream_out)
        if status:
            print(f"({status})", file=stream_out)
        print("> ", end="", flush=True, file=stream_out)
        try:
            line = stream_in.readline()
        except KeyboardInterrupt:
            print("\n(aborted)", file=stream_out)
            return False
        if line == "":  # EOF
            print("\n(EOF — aborting)", file=stream_out)
            return False
        result = _parse_command(line, selection)
        if result == "":
            return True
        if result == "quit":
            return False
        status = result


# ---------------------------------------------------------------------------
# Arrow-key interactive loop. Uses termios raw mode on POSIX and msvcrt
# on Windows. Renders the menu in-place via ANSI escapes.
# ---------------------------------------------------------------------------


# Single-byte → symbolic-name table shared by both raw-mode readers. POSIX
# delivers a couple of extras (ctrl-d / backspace) that Windows' msvcrt
# never surfaces, but a lookup miss falls through to the UTF-8 decode path
# either way, so listing them here is harmless on Windows.
_SYMBOLIC_BYTES: dict[bytes, str] = {
    b"\r": "enter",
    b"\n": "enter",
    b" ": "space",
    b"\t": "tab",
    b"\x03": "ctrl-c",
    b"\x04": "ctrl-d",
    b"\x1b": "esc",
    b"\x7f": "backspace",
}


def _classify_byte(ch: bytes) -> str:
    """Map one raw byte to a symbolic key name, falling back to a
    lowercase decoded char (or "esc" if the byte isn't UTF-8 — strict
    decode so an invalid byte doesn't trickle through to `_handle_key`
    as the replacement character)."""
    if ch in _SYMBOLIC_BYTES:
        return _SYMBOLIC_BYTES[ch]
    try:
        return ch.decode("utf-8").lower()
    except UnicodeDecodeError:
        return "esc"


def _read_key_posix(fd: int) -> str:
    """Read one keystroke from a POSIX raw-mode terminal. Returns one of
    the symbolic names handled by `_handle_key` or a single lowercase
    character."""
    import select

    try:
        ch = os.read(fd, 1)
    except OSError:
        return "eof"
    if not ch:
        return "eof"
    if ch == b"\x1b":
        # ESC alone vs the start of an escape sequence — wait briefly
        # for follow-on bytes; if none arrive, treat as a lone Esc.
        rlist, _, _ = select.select([fd], [], [], 0.05)
        if not rlist:
            return "esc"
        rest = os.read(fd, 16)
        if rest.startswith(b"[A") or rest.startswith(b"OA"):
            return "up"
        if rest.startswith(b"[B") or rest.startswith(b"OB"):
            return "down"
        if rest.startswith(b"[C") or rest.startswith(b"OC"):
            return "right"
        if rest.startswith(b"[D") or rest.startswith(b"OD"):
            return "left"
        return "esc"
    return _classify_byte(ch)


def _read_key_windows() -> str:
    import msvcrt  # type: ignore[import-not-found]  # Windows-only stdlib module.

    ch = msvcrt.getch()
    if ch in (b"\x00", b"\xe0"):
        ch2 = msvcrt.getch()
        return {b"H": "up", b"P": "down", b"K": "left", b"M": "right"}.get(ch2, "esc")
    return _classify_byte(ch)


def _enable_windows_vt() -> None:
    """Best-effort enable ANSI escape processing on Windows 10+ consoles.
    No-op (and swallowed) on older / non-Windows shells — worst case the
    picker prints raw escape codes, which is ugly but not fatal."""
    if sys.platform != "win32":
        return
    try:
        import ctypes  # noqa: PLC0415 — only needed on this branch.

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_ulong()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except OSError as exc:
        # Pre-Windows-10 consoles don't expose Get/SetConsoleMode and the
        # call raises OSError. Without VT processing the picker prints
        # raw escape codes (ugly), but the operator can still confirm/
        # quit, so the picker shouldn't crash the bring-up over a
        # cosmetic terminal-feature probe — surface a one-line hint
        # instead so the garbled output isn't mysterious.
        print(
            f"[install-picker] note: couldn't enable ANSI escapes on this console "
            f"({type(exc).__name__}); arrow-key UI may render as raw escape codes. "
            "Re-run on Windows Terminal / PowerShell 7+ for a clean UI.",
            file=sys.stderr,
        )


def _cycle_backend(fam: FamilyDef, choice: FamilyChoice, caps: MachineCaps, *, direction: int) -> None:
    """Step the active backend by `direction` (-1 or +1) through the
    cycleable backends for `fam`. No-op when the family has only one
    backend on this machine."""
    keys = cycleable_backend_keys(fam, caps)
    if not keys:
        return
    if choice.backend not in keys:
        choice.backend = keys[0]
        return
    i = (keys.index(choice.backend) + direction) % len(keys)
    choice.backend = keys[i]


def _handle_key(
    key: str,
    selection: Selection,
    cursor_box: list[int],
    caps: MachineCaps,
) -> str | None:
    """Dispatch one keystroke against `selection` / `cursor_box`. Returns
    `"confirm"`, `"quit"`, or `None` to keep looping."""
    fam = FAMILIES[cursor_box[0]]
    if key in ("up", "k"):
        cursor_box[0] = (cursor_box[0] - 1) % len(FAMILIES)
    elif key in ("down", "j"):
        cursor_box[0] = (cursor_box[0] + 1) % len(FAMILIES)
    elif key == "left":
        _cycle_backend(fam, selection.for_family(fam.key), caps, direction=-1)
    elif key == "right":
        _cycle_backend(fam, selection.for_family(fam.key), caps, direction=+1)
    elif key == "home":
        cursor_box[0] = 0
    elif key == "end":
        cursor_box[0] = len(FAMILIES) - 1
    elif key in ("space", "x"):
        ch = selection.for_family(fam.key)
        ch.enabled = not ch.enabled
    elif key == "enter":
        return "confirm"
    elif key in ("q", "esc", "ctrl-c", "ctrl-d", "eof"):
        return "quit"
    elif key == "a":
        all_on = all(selection.choices.get(f.key, FamilyChoice()).enabled for f in FAMILIES)
        for f in FAMILIES:
            selection.for_family(f.key).enabled = not all_on
    elif key == "r":
        defaults = Selection.defaults_for(caps)
        selection.choices = defaults.choices
    elif len(key) == 1 and key.isdigit():
        n = int(key)
        if 1 <= n <= len(FAMILIES):
            cursor_box[0] = n - 1
            target = FAMILIES[n - 1]
            ch = selection.for_family(target.key)
            ch.enabled = not ch.enabled
    return None


def _drive_picker(
    selection: Selection,
    caps: MachineCaps,
    *,
    paint,
    read_key,
) -> bool:
    """Pure dispatch loop: paint, read keystroke, handle, repeat. Caller
    is responsible for setting up the terminal (raw mode, VT processing)
    and tearing it down. Split out from `_arrow_key_loop` so the
    keystroke→state-mutation pipeline is testable without termios /
    msvcrt — tests inject a scripted `read_key` and a no-op `paint`."""
    cursor_box = [0]
    # Pre-position cursor on the first enabled row, if any — feels less
    # arbitrary than always starting at the top.
    for i, fam in enumerate(FAMILIES):
        if selection.choices.get(fam.key, FamilyChoice()).enabled:
            cursor_box[0] = i
            break
    while True:
        paint(cursor_box[0])
        key = read_key()
        action = _handle_key(key, selection, cursor_box, caps)
        if action == "confirm":
            return True
        if action == "quit":
            return False


def _arrow_key_loop(selection: Selection, caps: MachineCaps, *, stream_in, stream_out) -> bool:
    """Drive the picker with arrow keys + space + ←/→. Caller has already
    confirmed both streams are real TTYs."""

    def paint(cursor: int) -> None:
        # Clear screen + home + hide cursor, then redraw.
        stream_out.write("\x1b[2J\x1b[H\x1b[?25l")
        stream_out.write(render(selection, caps, cursor=cursor))
        stream_out.write("\n")
        stream_out.flush()

    def restore_terminal() -> None:
        stream_out.write("\x1b[?25h\n")
        stream_out.flush()

    if sys.platform == "win32":
        _enable_windows_vt()
        try:
            return _drive_picker(selection, caps, paint=paint, read_key=_read_key_windows)
        finally:
            restore_terminal()

    # POSIX: switch the controlling terminal into cbreak mode so we get
    # one keystroke at a time without losing Ctrl+C signal handling.
    import termios  # noqa: PLC0415 — POSIX-only.
    import tty  # noqa: PLC0415 — POSIX-only.

    fd = stream_in.fileno()
    old_attrs = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        return _drive_picker(
            selection,
            caps,
            paint=paint,
            read_key=lambda: _read_key_posix(fd),
        )
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_attrs)
        restore_terminal()


def _can_use_arrow_keys(stream_in, stream_out) -> bool:
    """True iff both streams are real TTYs AND we have a way to read raw
    keystrokes on this platform. StringIO and piped invocations come out
    False here, dropping us into the numbered fallback."""
    try:
        if not (stream_in.isatty() and stream_out.isatty()):
            return False
    except (AttributeError, ValueError):
        return False
    try:
        stream_in.fileno()
    except (AttributeError, OSError, ValueError):
        return False
    if sys.platform == "win32":
        try:
            import msvcrt  # type: ignore[import-not-found]  # noqa: F401,PLC0415
        except ImportError:
            return False
        return True
    try:
        import termios  # noqa: F401,PLC0415
        import tty  # noqa: F401,PLC0415
    except ImportError:
        return False
    return True


def interactive_loop(selection: Selection, caps: MachineCaps, *, stream_in, stream_out) -> bool:
    """Drive the picker until the operator confirms or quits.

    Returns True on confirm, False on quit. Selection is mutated in
    place. On a real TTY we use the arrow-key UI; otherwise the
    numbered fallback (kept so CI / piped invocations and unit tests
    with StringIO still work)."""
    if _can_use_arrow_keys(stream_in, stream_out):
        try:
            return _arrow_key_loop(selection, caps, stream_in=stream_in, stream_out=stream_out)
        except (OSError, ImportError) as exc:
            # Terminal didn't let us into raw mode (rare; usually means
            # we lost the controlling tty). Fall back cleanly — only
            # name the exception class so the operator sees something
            # legible instead of a tcsetattr errno repr.
            print(
                "(arrow-key UI unavailable on this terminal "
                f"[{type(exc).__name__}]; falling back to numbered prompt)",
                file=stream_out,
            )
    return _numbered_loop(selection, caps, stream_in=stream_in, stream_out=stream_out)


# ---------------------------------------------------------------------------
# pip invocation.
# ---------------------------------------------------------------------------


def write_install_warnings(state_file: Path, selection: Selection, removed: list[FamilyDef]) -> None:
    """Record (or clear) the skipped-family sidecar beside `state_file`.

    Clearing on an empty list is the point as much as writing is: once the
    operator re-picks a valid backend the dashboard banner must go away.
    """
    sidecar = state_file.parent / WARNINGS_FILENAME
    if not removed:
        sidecar.unlink(missing_ok=True)
        return
    body = {
        "stale_backends": [
            {"family": fam.key, "label": fam.label, "backend": selection.choices[fam.key].backend}
            for fam in removed
        ]
    }
    try:
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        sidecar.write_text(json.dumps(body, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except OSError as exc:
        # A read-only or vanished data dir must not fail the install — the
        # stderr warning above already carried the message, and this file is
        # only the nicer surface for it.
        print(f"[install-picker] could not write {sidecar}: {exc}", file=sys.stderr, flush=True)


def build_pip_argv(
    extras: list[str],
    *,
    python: str = sys.executable,
    install_spec: str | None = None,
) -> list[str]:
    """Argv for the install, keeping us inside the current venv.

    The install TARGET is delegated to `tapscribe.install_target`, which knows
    the three topologies (ADR-0015). `install_spec=None` — the default and what
    a dev checkout passes — still yields the historical
    `pip install -e ".[a,b,c]"`, editable so source-tree edits land
    immediately. A Bundle passes its shipped wheel instead.
    """
    return install_target.pip_install_argv(extras, install_spec=install_spec, python=python)


def run_install(extras: list[str], *, dry_run: bool = False, install_spec: str | None = None) -> int:
    """Execute the install. `dry_run` skips the subprocess (used by tests
    and by `--dry-run` in the CLI)."""
    argv = build_pip_argv(extras, install_spec=install_spec)
    print(f"[install-picker] Running: {' '.join(argv)}", flush=True)
    if dry_run:
        return 0
    # cwd only matters for the checkout topology, where `-e .` is relative;
    # a wheel / PyPI spec is absolute, so this is harmless there.
    return subprocess.call(argv, cwd=REPO_ROOT)  # nosec B603 — fixed argv.


# ---------------------------------------------------------------------------
# Skip-install stamp — avoids re-running pip when nothing changed.
# ---------------------------------------------------------------------------


def _installed_version() -> str | None:
    """The installed TapScribe version, or None if it can't be determined.

    Split out as a seam so the Bundle-upgrade branch below is testable without
    reinstalling the package.
    """
    try:
        from tapscribe import __version__
    except ImportError:
        return None
    return __version__ or None


def pyproject_fingerprint() -> str:
    """A stamp of what a `pip install` would resolve, so an unchanged selection
    can skip pip but a CHANGED dependency set cannot.

    In a checkout that's the SHA-256 of pyproject.toml: a dependency bump after
    a `git pull` changes it even when the family/backend selection is
    byte-for-byte identical.

    A Bundle has no pyproject.toml — it installs a wheel — so falling back to
    the empty sentinel would be recorded in the stamp AND recomputed identically
    on every later run. They'd match forever, and after an upgrade the
    operator's model extras would never be re-resolved against the new wheel's
    pins. So fall back to the installed VERSION, which changes on exactly the
    event that matters.
    """
    try:
        return hashlib.sha256((REPO_ROOT / "pyproject.toml").read_bytes()).hexdigest()
    except OSError:
        pass  # Not a checkout — fall through to the version-based stamp below.
    version = _installed_version()
    if version is None:
        # Nothing identifies this install; return a sentinel rather than let a
        # stamp comparison skip pip on the strength of an unknown.
        return ""
    return f"version:{version}"


def read_install_stamp(path: Path) -> dict | None:
    """Load the last-successful-install stamp, or None if it's
    absent/unreadable — both of which mean 'don't skip, run pip'."""
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def install_is_current(stamp: dict | None, extras: list[str], fingerprint: str) -> bool:
    """True iff a prior install recorded these exact extras AND the same
    pyproject fingerprint — i.e. re-running pip would change nothing
    (modulo the editable package's own rebuild churn we're avoiding)."""
    if not stamp:
        return False
    return stamp.get("extras") == extras and stamp.get("pyproject") == fingerprint


def package_is_installed(module: str = "tapscribe") -> bool:
    """Whether the TapScribe package is importable in this venv. Guards the
    skip-install fast path against a stamp that outlived the package it
    describes — e.g. the operator ran `pip uninstall tapscribe`, or the
    venv was recreated by something other than start.sh. The picker runs
    from tools/ with the repo root NOT on sys.path, so `find_spec` only
    finds the module when an actual install put it there; a missing module
    means we must run pip even when the stamp looks current."""
    from importlib.util import find_spec

    return find_spec(module) is not None


def write_install_stamp(path: Path, extras: list[str], fingerprint: str) -> None:
    """Record a successful install so the next unchanged run can skip pip."""
    body = {"extras": extras, "pyproject": fingerprint}
    try:
        path.write_text(json.dumps(body, indent=2, sort_keys=True) + "\n")
    except OSError:
        # The stamp is a pure optimisation. If `sys.prefix` isn't writable
        # (read-only venv, odd permissions) we just lose the skip fast-path
        # and re-run pip next time — wasteful but harmless, and not worth
        # failing an otherwise-successful install over.
        pass


# ---------------------------------------------------------------------------
# Entry point.
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="install_picker",
        description=(
            "Interactive picker for which TapScribe model families and "
            "backends to install. Persists the selection to "
            ".tapscribe-install.json and runs pip."
        ),
    )
    p.add_argument(
        "--non-interactive",
        action="store_true",
        help="Don't prompt; use the saved selection (or defaults on first run) and install.",
    )
    p.add_argument(
        "--no-mlx",
        action="store_true",
        help="Treat this machine as non-MLX even on Apple Silicon — mirrors `start.sh --no-mlx`.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the resolved pip command and exit without running it.",
    )
    p.add_argument(
        "--state-file",
        default=None,
        help="Where the saved family/backend selection lives. Defaults to the "
        "repo root. A Bundle points this at TAPSCRIBE_BASE_DIR so the selection "
        "survives an upgrade instead of sitting in site-packages (ADR-0015).",
    )
    p.add_argument(
        "--install-spec",
        default=None,
        help="What pip installs TapScribe from: omitted (a dev checkout, the "
        "default), a path to the Bundle's shipped .whl, or a pinned "
        "'tapscribe==X.Y.Z'. See ADR-0015.",
    )
    args = p.parse_args(argv)

    # Validate before anything else touches it: this is a CLI value that flows
    # into a pip argv, which CodeQL treats as external input regardless of who
    # launched the process (CLAUDE.md). Fail with a message the operator can
    # act on rather than letting pip report an unfindable requirement.
    try:
        install_target.resolve_install_spec(args.install_spec)
    except install_target.InstallSpecError as exc:
        print(f"[install-picker] {exc}", file=sys.stderr, flush=True)
        return 2

    caps = detect_caps(force_no_mlx=args.no_mlx)
    # `--state-file` wins when given (a Bundle relocates it to the operator's
    # data dir); the module-level default is the dev checkout's repo root.
    state_file = Path(args.state_file) if args.state_file else STATE_FILE
    first_run = not state_file.exists()
    selection = Selection.load(state_file, caps)

    interactive = not args.non_interactive and sys.stdin.isatty() and sys.stdout.isatty()

    # In interactive mode the renderer surfaces removed-backend rows
    # inline, so a separate stderr warning is redundant (and worse,
    # scrolls off the moment the operator re-picks). In non-interactive
    # mode there's no renderer — print one line per affected family so
    # `start.sh --non-interactive` doesn't silently produce an
    # incomplete install.
    removed = selection.removed_backend_families()
    if not interactive:
        for fam in removed:
            backend = selection.choices[fam.key].backend
            print(
                f"[install-picker] WARNING: '{fam.key}' was saved with backend "
                f"'{backend}', which is no longer in this version's catalog. "
                "Not auto-selecting an alternative — re-pick interactively, "
                "or edit .tapscribe-install.json. The family will be skipped this run.",
                file=sys.stderr,
                flush=True,
            )
    # Persist the same finding where the dashboard can see it. Written on every
    # run (including interactive) so re-picking clears the banner — a warning
    # file that outlives its cause is worse than no warning at all.
    write_install_warnings(state_file, selection, removed)

    if interactive:
        if first_run:
            print(
                "[install-picker] First run on this checkout. The picker now lets "
                "you choose a backend (CPU/CUDA vs MLX vs Both) per family — use "
                "←/→ on each row. Default is whichever backend is fastest on this "
                "machine.",
                flush=True,
            )
        confirmed = interactive_loop(selection, caps, stream_in=sys.stdin, stream_out=sys.stdout)
        if not confirmed:
            print("[install-picker] aborted by operator.", file=sys.stderr)
            return 1
    else:
        summary = (
            ", ".join(
                f"{k}={selection.choices[k].backend}"
                for k in sorted(selection.choices)
                if selection.choices[k].enabled
            )
            or "(none)"
        )
        print(
            f"[install-picker] non-interactive mode — using saved selection: {summary}",
            flush=True,
        )

    extras = resolve_extras(selection, caps)

    # Dry-run is purely read-only: don't persist the selection or stamp so
    # the operator can preview the pip command without committing to it.
    if args.dry_run:
        return run_install(extras, dry_run=True, install_spec=args.install_spec)

    selection.save(state_file)

    # Skip pip entirely when nothing that affects the installed package set
    # has changed since the last successful install AND the package is still
    # actually importable. The editable install (`pip install -e`) already
    # reflects source-tree edits without a reinstall, so the only things that
    # force a real install are a changed extras selection or a changed
    # pyproject.toml (dependency bump). The `package_is_installed()` check
    # keeps us from trusting a stamp that outlived its package (manual
    # uninstall, out-of-band venv recreation), which would otherwise leave a
    # broken install with no pip run.
    fingerprint = pyproject_fingerprint()
    stamp = read_install_stamp(STAMP_FILE)
    if install_is_current(stamp, extras, fingerprint) and package_is_installed():
        spec = f".[{','.join(extras)}]" if extras else "."
        print(
            f"[install-picker] '{spec}' already installed and unchanged since "
            "the last run — skipping pip. (Editable install: source edits are "
            "already live. Change the selection to force a reinstall.)",
            flush=True,
        )
        return 0

    rc = run_install(extras, install_spec=args.install_spec)
    if rc != 0:
        print(f"[install-picker] pip exited with status {rc}.", file=sys.stderr)
        return rc
    write_install_stamp(STAMP_FILE, extras, fingerprint)
    return 0


if __name__ == "__main__":  # pragma: no cover — exercised by start.sh
    raise SystemExit(main())
