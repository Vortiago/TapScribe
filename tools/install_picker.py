"""TapScribe install picker — interactive selection of model families to
install, persisted across runs.

Called by `start.sh` / `start.ps1` after the venv exists and pip has been
upgraded. The picker:

  1. Detects this machine (Apple Silicon? CUDA via `nvidia-smi`?).
  2. Loads the previous selection from `.tapscribe-install.json`, if any;
     falls back to a minimal default (whisper only) on first run.
  3. Renders a checkbox-style menu. On a real TTY the operator navigates
     with arrow keys (↑/↓) and toggles with Space; otherwise we fall back
     to numbered toggles for piped / CI invocations.
  4. Writes the chosen selection back to `.tapscribe-install.json`.
  5. Runs `pip install -e ".[…]"` with the resolved extras.

Stdlib-only by design: this file runs before the operator has agreed to
install anything, and certainly before TapScribe's runtime deps exist in
the venv. Don't import third-party modules here.

The set of known families mirrors `pyproject.toml`'s optional-dependencies
groups and `tapscribe.transcribers.catalog`'s family list — kept in
manual sync since this module must run without importing tapscribe.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess  # nosec B404 — we run pip with a fixed argv list, no shell.
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
STATE_FILE = REPO_ROOT / ".tapscribe-install.json"


# ---------------------------------------------------------------------------
# Family catalog — what the picker lets the operator toggle.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FamilyDef:
    """One row in the picker.

    `extras_cpu_cuda` are the pyproject extras to install when the
    operator's machine has CPU and/or CUDA; `extras_mlx` are added when
    MLX is available too. Extras with `sys_platform`/`platform_machine`
    markers are still safe to list everywhere — pip skips them on the
    wrong platform.

    `mlx_via_env_marker` is True when the family's CPU/CUDA extra
    bundles an MLX-only package gated by a sys_platform env marker (so
    pip pulls it in on Apple Silicon without us adding a separate extra
    — currently only Canary).
    """

    key: str
    label: str
    description: str
    size_hint: str
    extras_cpu_cuda: tuple[str, ...]
    extras_mlx: tuple[str, ...]
    default_selected: bool = False
    mlx_via_env_marker: bool = False


FAMILIES: tuple[FamilyDef, ...] = (
    FamilyDef(
        key="whisper",
        label="Whisper / NB-Whisper",
        description=(
            "OpenAI Whisper + NB-AiLab's Norwegian-tuned variants. "
            "Powers the live caption channel. Recommended baseline."
        ),
        size_hint="~150 MB CPU, +~50 MB MLX",
        extras_cpu_cuda=("whisper",),
        extras_mlx=("mlx",),
        default_selected=True,
    ),
    FamilyDef(
        key="voxtral",
        label="Voxtral (Mistral)",
        description=("Mistral Voxtral 3B audio LLM. Batch + live. Pulls PyTorch + transformers."),
        size_hint="~2 GB",
        extras_cpu_cuda=("voxtral",),
        # No published MLX-voxtral PyPI extra yet — the catalog probes for
        # `mlx_voxtral` but `pyproject.toml` doesn't list it. Operators
        # who want it install it manually.
        extras_mlx=(),
    ),
    FamilyDef(
        key="parakeet",
        label="Parakeet (NVIDIA)",
        description=("NVIDIA Parakeet TDT 0.6B v3 — 25 EU langs, top of HF Open ASR. Batch only."),
        size_hint="~1.5 GB",
        extras_cpu_cuda=("parakeet",),
        extras_mlx=("parakeet-mlx",),
    ),
    FamilyDef(
        key="canary",
        label="Canary (NVIDIA)",
        description=(
            "NVIDIA Canary 1B v2 — translation + 25 EU langs. Batch only. "
            "MLX adapter is bundled in the same extra."
        ),
        size_hint="~2 GB",
        extras_cpu_cuda=("canary",),
        # The `canary` extra already lists `mlx-audio` with a Darwin/arm64
        # env marker, so a single extra covers both runtimes.
        extras_mlx=(),
        mlx_via_env_marker=True,
    ),
)


_FAMILIES_BY_KEY: dict[str, FamilyDef] = {f.key: f for f in FAMILIES}


# ---------------------------------------------------------------------------
# Machine capability detection.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MachineCaps:
    """What `start.sh`/`.ps1`-equivalent introspection turns up. The picker
    uses this both for the human-readable header AND to decide which
    extras a given family selection translates to."""

    os_name: str  # "Darwin" / "Linux" / "Windows"
    arch: str  # "arm64" / "x86_64" / etc.
    mlx: bool  # Apple Silicon — we'll install MLX-flavoured extras
    cuda: bool  # `nvidia-smi` exit zero — best-effort signal


def detect_caps(*, force_no_mlx: bool = False) -> MachineCaps:
    """Probe the host. CUDA detection is `nvidia-smi`-based since we have
    no torch yet at picker time; it'll false-negative on machines with
    a GPU but no driver tooling, which is the right way to err — the
    operator can opt-in by toggling Voxtral/Parakeet anyway."""
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


# ---------------------------------------------------------------------------
# Persistence.
# ---------------------------------------------------------------------------


@dataclass
class Selection:
    """The operator's choices, persisted across runs.

    `families` is the set of family keys the operator has ticked.
    Backends are derived from `MachineCaps` at install time; we don't
    persist them because the right answer depends on the current
    hardware (e.g. moving a checkout between a laptop and a workstation).
    """

    families: set[str] = field(default_factory=set)

    @classmethod
    def load(cls, path: Path) -> Selection:
        if not path.exists():
            return cls(families={f.key for f in FAMILIES if f.default_selected})
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return cls(families={f.key for f in FAMILIES if f.default_selected})
        raw = data.get("families", [])
        # Drop unknown keys so a stale state file from a future version
        # doesn't crash the picker.
        families = {k for k in raw if k in _FAMILIES_BY_KEY}
        return cls(families=families)

    def save(self, path: Path) -> None:
        path.write_text(json.dumps({"families": sorted(self.families)}, indent=2) + "\n")


# ---------------------------------------------------------------------------
# Extras resolution — selection × machine caps → pip extras list.
# ---------------------------------------------------------------------------


def resolve_extras(selection: Selection, caps: MachineCaps) -> list[str]:
    """Turn a family selection into the pyproject extras to install.

    Always picks `extras_cpu_cuda` (since CPU is always available, even
    when CUDA isn't — faster-whisper happily runs CPU-only). Adds
    `extras_mlx` for MLX-capable machines. Order is stable so the
    install command is reproducible and easy to compare across runs.
    """
    out: list[str] = []
    seen: set[str] = set()
    for fam in FAMILIES:
        if fam.key not in selection.families:
            continue
        for extra in fam.extras_cpu_cuda:
            if extra not in seen:
                out.append(extra)
                seen.add(extra)
        if caps.mlx:
            for extra in fam.extras_mlx:
                if extra not in seen:
                    out.append(extra)
                    seen.add(extra)
    return out


def family_backend_label(fam: FamilyDef, caps: MachineCaps) -> str:
    """Human-readable summary of which runtime(s) this family will install
    on the current machine. Shown next to each row so the operator can
    tell at a glance whether they'll get CPU, CUDA, or MLX packages."""
    backends: list[str] = []
    if caps.mlx and (fam.extras_mlx or fam.mlx_via_env_marker):
        backends.append("MLX")
    if fam.extras_cpu_cuda:
        # All current cpu_cuda extras pull torch (or faster-whisper, which
        # has optional CUDA via CTranslate2), so CUDA acceleration is
        # available whenever nvidia-smi reports a device.
        backends.append("CUDA" if caps.cuda else "CPU")
    return " + ".join(backends) if backends else "—"


def family_extras_preview(fam: FamilyDef, caps: MachineCaps) -> list[str]:
    """The extras THIS family contributes, given caps. Used in the picker
    to show operators which `[...]` tokens map to which family."""
    out = list(fam.extras_cpu_cuda)
    if caps.mlx:
        for extra in fam.extras_mlx:
            if extra not in out:
                out.append(extra)
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


def render(selection: Selection, caps: MachineCaps, *, cursor: int | None = None) -> str:
    """Return the picker as a string. Pure — easy to snapshot in tests.

    When `cursor` is set, the corresponding row is highlighted with a
    `>` marker and the controls block describes arrow-key navigation;
    when `cursor` is None, we render the numbered-fallback help block.
    """
    lines: list[str] = []
    lines.append("TapScribe install picker")
    lines.append("=" * 60)
    lines.append(f"Machine: {_format_caps_line(caps)}")
    lines.append("")
    lines.append("Model families to install:")
    for idx, fam in enumerate(FAMILIES, start=1):
        mark = "x" if fam.key in selection.families else " "
        arrow = ">" if cursor is not None and cursor == idx - 1 else " "
        lines.append(f"{arrow} [{mark}] {idx}. {fam.label}  ({fam.size_hint})")
        lines.append(f"          {fam.description}")
        backend = family_backend_label(fam, caps)
        extras_preview = family_extras_preview(fam, caps)
        suffix = ""
        if fam.key == "voxtral" and caps.mlx:
            suffix = "  — no MLX adapter on PyPI yet"
        if extras_preview:
            lines.append(
                f"          Backend: {backend}   extras: [{', '.join(extras_preview)}]{suffix}"
            )
        else:
            lines.append(f"          Backend: {backend}{suffix}")
        lines.append("")
    extras = resolve_extras(selection, caps)
    if extras:
        lines.append(f"Will install:  pip install -e '.[{','.join(extras)}]'")
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
    else:
        lines.append("↑/↓ move · Space toggle · a all · r reset · Enter install · q quit")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Numbered (fallback) interactive loop. Used when stdin/stdout isn't a real
# TTY (CI, piped, tests with StringIO) so the picker still works without
# raw-mode keyboard input.
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
        # Toggle all: if everything's already on, turn everything off;
        # otherwise turn everything on. Mirrors how checkbox "select all"
        # toggles in most UIs.
        if selection.families == {f.key for f in FAMILIES}:
            selection.families = set()
        else:
            selection.families = {f.key for f in FAMILIES}
        return "toggled all"
    if cmd == "r":
        selection.families = {f.key for f in FAMILIES if f.default_selected}
        return "reset to defaults"
    # Numbers (comma or whitespace separated)
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
        if fam.key in selection.families:
            selection.families.discard(fam.key)
        else:
            selection.families.add(fam.key)
        toggled.append(fam.label)
    parts: list[str] = []
    if toggled:
        parts.append(f"toggled: {', '.join(toggled)}")
    if bad:
        parts.append(f"ignored unknown: {', '.join(bad)}")
    return " · ".join(parts) if parts else "(no change)"


def _numbered_loop(selection: Selection, caps: MachineCaps, *, stream_in, stream_out) -> bool:
    """The original line-buffered numbered picker. Kept as the fallback
    for non-TTY contexts — CI, piped invocations, and the unit tests
    (which pass StringIO)."""
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


def _toggle(selection: Selection, key: str) -> None:
    if key in selection.families:
        selection.families.discard(key)
    else:
        selection.families.add(key)


def _read_key_posix(fd: int) -> str:
    """Read one keystroke from a POSIX raw-mode terminal. Returns one of
    the symbolic names handled by `_arrow_key_loop` (see _handle_key) or
    a single lowercase character."""
    import select

    try:
        ch = os.read(fd, 1)
    except OSError:
        return "eof"
    if not ch:
        return "eof"
    if ch == b"\x1b":
        # ESC: could be a lone Esc or the start of an escape sequence.
        # Wait briefly for follow-on bytes; if none arrive, treat as Esc.
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
    if ch in (b"\r", b"\n"):
        return "enter"
    if ch == b" ":
        return "space"
    if ch == b"\t":
        return "tab"
    if ch == b"\x03":
        return "ctrl-c"
    if ch == b"\x04":
        return "ctrl-d"
    if ch == b"\x7f":
        return "backspace"
    try:
        return ch.decode("utf-8", errors="replace").lower()
    except UnicodeDecodeError:
        return "esc"


def _read_key_windows() -> str:
    import msvcrt  # type: ignore[import-not-found]  # Windows-only stdlib module.

    ch = msvcrt.getch()
    if ch in (b"\x00", b"\xe0"):
        ch2 = msvcrt.getch()
        return {b"H": "up", b"P": "down", b"K": "left", b"M": "right"}.get(ch2, "esc")
    if ch in (b"\r", b"\n"):
        return "enter"
    if ch == b" ":
        return "space"
    if ch == b"\t":
        return "tab"
    if ch == b"\x03":
        return "ctrl-c"
    if ch == b"\x1b":
        return "esc"
    try:
        return ch.decode("utf-8", errors="replace").lower()
    except UnicodeDecodeError:
        return "esc"


def _enable_windows_vt() -> None:
    """Best-effort enable ANSI escape processing on Windows 10+ consoles.
    No-op (and swallowed) on older / non-Windows shells — the worst case
    is the picker prints raw escape codes, which is ugly but not fatal."""
    if sys.platform != "win32":
        return
    try:
        import ctypes  # noqa: PLC0415 — only needed on this branch.

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_ulong()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except OSError:
        pass


def _handle_key(
    key: str, selection: Selection, cursor_box: list[int]
) -> str | None:
    """Dispatch one keystroke against `selection` / `cursor_box`. Returns
    `"confirm"`, `"quit"`, or `None` to keep looping."""
    if key in ("up", "k"):
        cursor_box[0] = (cursor_box[0] - 1) % len(FAMILIES)
    elif key in ("down", "j"):
        cursor_box[0] = (cursor_box[0] + 1) % len(FAMILIES)
    elif key in ("home",):
        cursor_box[0] = 0
    elif key in ("end",):
        cursor_box[0] = len(FAMILIES) - 1
    elif key in ("space", "x"):
        _toggle(selection, FAMILIES[cursor_box[0]].key)
    elif key == "enter":
        return "confirm"
    elif key in ("q", "esc", "ctrl-c", "ctrl-d", "eof"):
        return "quit"
    elif key == "a":
        if selection.families == {f.key for f in FAMILIES}:
            selection.families = set()
        else:
            selection.families = {f.key for f in FAMILIES}
    elif key == "r":
        selection.families = {f.key for f in FAMILIES if f.default_selected}
    elif len(key) == 1 and key.isdigit():
        n = int(key)
        if 1 <= n <= len(FAMILIES):
            cursor_box[0] = n - 1
            _toggle(selection, FAMILIES[n - 1].key)
    return None


def _arrow_key_loop(
    selection: Selection, caps: MachineCaps, *, stream_in, stream_out
) -> bool:
    """Drive the picker with arrow keys + spacebar. Caller has already
    confirmed both streams are real TTYs."""
    cursor_box = [0]
    # Pre-position cursor on the first selected row, if any — feels less
    # arbitrary than always starting at the top.
    for i, fam in enumerate(FAMILIES):
        if fam.key in selection.families:
            cursor_box[0] = i
            break

    def paint() -> None:
        # Clear screen + home + hide cursor, then redraw.
        stream_out.write("\x1b[2J\x1b[H\x1b[?25l")
        stream_out.write(render(selection, caps, cursor=cursor_box[0]))
        stream_out.write("\n")
        stream_out.flush()

    def restore_terminal() -> None:
        stream_out.write("\x1b[?25h\n")
        stream_out.flush()

    if sys.platform == "win32":
        _enable_windows_vt()
        try:
            while True:
                paint()
                key = _read_key_windows()
                action = _handle_key(key, selection, cursor_box)
                if action == "confirm":
                    return True
                if action == "quit":
                    return False
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
        while True:
            paint()
            key = _read_key_posix(fd)
            action = _handle_key(key, selection, cursor_box)
            if action == "confirm":
                return True
            if action == "quit":
                return False
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
    line-buffered numbered fallback (kept so CI / piped invocations and
    unit tests with StringIO still work).
    """
    if _can_use_arrow_keys(stream_in, stream_out):
        try:
            return _arrow_key_loop(selection, caps, stream_in=stream_in, stream_out=stream_out)
        except (OSError, ImportError) as exc:
            # Terminal didn't let us into raw mode (rare; usually means
            # we lost the controlling tty). Fall back cleanly.
            print(f"(arrow-key UI unavailable: {exc}; falling back to numbered prompt)",
                  file=stream_out)
    return _numbered_loop(selection, caps, stream_in=stream_in, stream_out=stream_out)


# ---------------------------------------------------------------------------
# pip invocation.
# ---------------------------------------------------------------------------


def build_pip_argv(extras: list[str], *, python: str = sys.executable) -> list[str]:
    """Argv for the install. `python -m pip install -e ".[a,b,c]"` keeps
    us inside the current venv. Always editable so source-tree edits land
    immediately; the operator's TapScribe is a checkout, not a release."""
    spec = "."
    if extras:
        spec = f".[{','.join(extras)}]"
    return [python, "-m", "pip", "install", "-e", spec]


def run_install(extras: list[str], *, dry_run: bool = False) -> int:
    """Execute the install. `dry_run` skips the subprocess (used by tests
    and by `--print-install-command` in the CLI)."""
    argv = build_pip_argv(extras)
    print(f"[install-picker] Running: {' '.join(argv)}", flush=True)
    if dry_run:
        return 0
    return subprocess.call(argv, cwd=REPO_ROOT)  # nosec B603 — fixed argv.


# ---------------------------------------------------------------------------
# Entry point.
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="install_picker",
        description=(
            "Interactive picker for which TapScribe model families to install. "
            "Persists the selection to .tapscribe-install.json and runs pip."
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
    args = p.parse_args(argv)

    caps = detect_caps(force_no_mlx=args.no_mlx)
    first_run = not STATE_FILE.exists()
    selection = Selection.load(STATE_FILE)

    # Non-interactive: still requires a TTY to be polite to CI / piped
    # invocations. Operators in unattended contexts pass --non-interactive
    # explicitly.
    interactive = not args.non_interactive and sys.stdin.isatty() and sys.stdout.isatty()
    if interactive:
        if first_run:
            # Heads-up for operators upgrading from a TapScribe that
            # always installed Voxtral as part of start.sh's hard-coded
            # base set. The default selection is whisper-only now —
            # surface that so they don't silently lose Voxtral.
            print(
                "[install-picker] First run on this checkout. Previous versions of "
                "start.sh installed Whisper + Voxtral; the picker now defaults to "
                "Whisper only. Tick Voxtral (option 2) below if you used it.",
                flush=True,
            )
        confirmed = interactive_loop(selection, caps, stream_in=sys.stdin, stream_out=sys.stdout)
        if not confirmed:
            print("[install-picker] aborted by operator.", file=sys.stderr)
            return 1
    else:
        print(
            f"[install-picker] non-interactive mode — using saved selection: "
            f"{sorted(selection.families) or '(none)'}",
            flush=True,
        )

    extras = resolve_extras(selection, caps)
    # Dry-run is purely read-only: don't persist the selection so the
    # operator can preview the pip command without committing to it.
    if not args.dry_run:
        selection.save(STATE_FILE)
    rc = run_install(extras, dry_run=args.dry_run)
    if rc != 0:
        print(f"[install-picker] pip exited with status {rc}.", file=sys.stderr)
    return rc


if __name__ == "__main__":  # pragma: no cover — exercised by start.sh
    raise SystemExit(main())
