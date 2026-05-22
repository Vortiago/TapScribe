"""Tests for tools/install_picker.py — per-family + per-backend bootstrap.

The picker is a standalone stdlib-only script (it runs before TapScribe's
extras are installed), so these tests import it via path manipulation
rather than as a package module.
"""

from __future__ import annotations

import io
import json
import sys
import tomllib
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.version import Version

# tools/ isn't a package — make install_picker importable by name.
TOOLS_DIR = Path(__file__).resolve().parent.parent / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import install_picker  # noqa: E402
from install_picker import (  # noqa: E402
    BACKEND_BOTH,
    BACKEND_CPU,
    BACKEND_MLX,
    FAMILIES,
    FamilyChoice,
    MachineCaps,
    Selection,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


def _caps(*, mlx: bool = False, cuda: bool = False) -> MachineCaps:
    return MachineCaps(os_name="Linux", arch="x86_64", mlx=mlx, cuda=cuda)


def _apple_caps() -> MachineCaps:
    return MachineCaps(os_name="Darwin", arch="arm64", mlx=True, cuda=False)


@pytest.fixture
def tmp_state(monkeypatch, tmp_path):
    """Point STATE_FILE at a fresh tmpdir so tests don't touch the
    operator's real `.tapscribe-install.json`."""
    state = tmp_path / ".tapscribe-install.json"
    monkeypatch.setattr(install_picker, "STATE_FILE", state)
    return state


# ── Selection persistence + migration ───────────────────────────────


def test_selection_load_returns_defaults_when_file_missing(tmp_state):
    sel = Selection.load(tmp_state, _caps())
    # Whisper is the only family flagged default_selected=True.
    enabled = {k for k, c in sel.choices.items() if c.enabled}
    assert enabled == {"whisper"}
    # Default backend on a plain CPU box is CPU/CUDA.
    assert sel.choices["whisper"].backend == BACKEND_CPU


def test_selection_default_backend_is_mlx_on_apple_silicon(tmp_state):
    sel = Selection.load(tmp_state, _apple_caps())
    assert sel.choices["whisper"].backend == BACKEND_MLX
    # Voxtral has no MLX path, so even on Apple Silicon it defaults to CPU.
    assert sel.choices["voxtral"].backend == BACKEND_CPU


def test_selection_round_trips_through_disk(tmp_state):
    written = Selection()
    written.choices["whisper"] = FamilyChoice(enabled=True, backend=BACKEND_MLX)
    written.choices["voxtral"] = FamilyChoice(enabled=True, backend=BACKEND_CPU)
    written.save(tmp_state)
    assert tmp_state.exists()
    blob = json.loads(tmp_state.read_text())
    assert blob["version"] == install_picker.STATE_VERSION
    loaded = Selection.load(tmp_state, _apple_caps())
    assert loaded.choices["whisper"].enabled is True
    assert loaded.choices["whisper"].backend == BACKEND_MLX
    assert loaded.choices["voxtral"].backend == BACKEND_CPU


def test_selection_load_migrates_v1_format(tmp_state):
    """Operators who already ran the older picker have a `families: [...]`
    state file. Migrating must preserve enable flags and pick a sensible
    backend default for the current machine."""
    tmp_state.write_text(json.dumps({"families": ["whisper", "voxtral"]}))
    sel = Selection.load(tmp_state, _apple_caps())
    assert sel.choices["whisper"].enabled is True
    assert sel.choices["voxtral"].enabled is True
    assert sel.choices["parakeet"].enabled is False
    # v1 always installed both atomic backends when MLX was available —
    # preserve that explicitly so the migration doesn't silently shrink
    # the install on first re-launch.
    assert sel.choices["whisper"].backend == BACKEND_BOTH
    # Voxtral has no MLX path, so even after v1 migration it's CPU.
    assert sel.choices["voxtral"].backend == BACKEND_CPU


def test_selection_load_ignores_stale_backend_values(tmp_state):
    """A state file mentioning a backend this machine doesn't ship
    (e.g. 'mlx' from an Apple Silicon checkout opened on Linux) must
    persist as-is on disk but downgrade to a valid value on load. We
    DON'T silently rewrite the file — operators moving back to MLX get
    their choice restored."""
    tmp_state.write_text(
        json.dumps(
            {
                "version": install_picker.STATE_VERSION,
                "choices": {
                    "whisper": {"enabled": True, "backend": "gibberish"},
                },
            }
        )
    )
    sel = Selection.load(tmp_state, _caps())
    assert sel.choices["whisper"].enabled is True
    # Garbage backend value gets clamped to the machine-natural default.
    assert sel.choices["whisper"].backend == BACKEND_CPU


def test_selection_load_handles_malformed_file(tmp_state):
    tmp_state.write_text("not json at all")
    sel = Selection.load(tmp_state, _caps())
    # Falls back to defaults instead of raising.
    assert sel.choices["whisper"].enabled is True


# ── Backend availability + cycling ──────────────────────────────────


def test_available_backends_filters_by_caps():
    whisper = next(f for f in FAMILIES if f.key == "whisper")
    assert [b.key for b in install_picker.available_backends(whisper, _caps())] == [BACKEND_CPU]
    assert [b.key for b in install_picker.available_backends(whisper, _apple_caps())] == [
        BACKEND_CPU,
        BACKEND_MLX,
    ]
    voxtral = next(f for f in FAMILIES if f.key == "voxtral")
    # Voxtral has no MLX path even on Apple Silicon.
    assert [b.key for b in install_picker.available_backends(voxtral, _apple_caps())] == [BACKEND_CPU]


def test_cycleable_backend_keys_includes_both_when_two_backends_available():
    whisper = next(f for f in FAMILIES if f.key == "whisper")
    assert install_picker.cycleable_backend_keys(whisper, _apple_caps()) == [
        BACKEND_CPU,
        BACKEND_MLX,
        BACKEND_BOTH,
    ]


def test_cycleable_backend_keys_no_both_when_only_one_available():
    """No point cycling through 'Both' when there's nothing to combine."""
    voxtral = next(f for f in FAMILIES if f.key == "voxtral")
    assert install_picker.cycleable_backend_keys(voxtral, _apple_caps()) == [BACKEND_CPU]


# ── Extras resolution ───────────────────────────────────────────────


def _enable(sel: Selection, key: str, backend: str = BACKEND_CPU) -> None:
    sel.choices[key] = FamilyChoice(enabled=True, backend=backend)


def test_resolve_extras_empty_selection_emits_no_extras():
    assert install_picker.resolve_extras(Selection(), _caps()) == []


def test_resolve_extras_whisper_cpu_installs_shared_plus_cpu_atoms():
    sel = Selection()
    _enable(sel, "whisper", BACKEND_CPU)
    assert install_picker.resolve_extras(sel, _caps()) == ["whisper-live", "whisper-cpu"]


def test_resolve_extras_whisper_mlx_skips_cpu_atom():
    """The whole point of per-backend selection: MLX-only means no
    faster-whisper download."""
    sel = Selection()
    _enable(sel, "whisper", BACKEND_MLX)
    assert install_picker.resolve_extras(sel, _apple_caps()) == ["whisper-live", "whisper-mlx"]


def test_resolve_extras_whisper_both_installs_everything():
    sel = Selection()
    _enable(sel, "whisper", BACKEND_BOTH)
    assert install_picker.resolve_extras(sel, _apple_caps()) == [
        "whisper-live",
        "whisper-cpu",
        "whisper-mlx",
    ]


def test_resolve_extras_mlx_choice_on_non_mlx_machine_downgrades_silently():
    """Operator's CPU box doesn't ship MLX — picking it has to fall back
    cleanly so the install doesn't try to resolve a non-existent wheel.

    This is the *host caps* fallback: MLX is still in the Whisper catalog,
    just unavailable on Linux. Compare with the catalog-removed case
    below (`test_resolve_extras_removed_backend_does_not_silently_fall_back`)
    which deliberately does NOT downgrade."""
    sel = Selection()
    _enable(sel, "whisper", BACKEND_MLX)
    extras = install_picker.resolve_extras(sel, _caps())
    assert "whisper-mlx" not in extras
    assert "whisper-cpu" in extras


def _patch_whisper_without_mlx(monkeypatch) -> None:
    """Pretend a future PR removed the MLX backend from Whisper. Used by
    the catalog-removed regression tests so they don't depend on what
    the *current* catalog declares — they're testing the contract, not
    today's catalog shape."""
    whisper = next(f for f in FAMILIES if f.key == "whisper")
    cpu_only = install_picker.FamilyDef(
        key=whisper.key,
        label=whisper.label,
        description=whisper.description,
        size_hint=whisper.size_hint,
        shared_extras=whisper.shared_extras,
        backends=(install_picker.BackendDef(key=BACKEND_CPU, label="CPU/CUDA", extras=("whisper-cpu",)),),
        default_selected=whisper.default_selected,
    )
    others = tuple(f for f in FAMILIES if f.key != whisper.key)
    monkeypatch.setattr(install_picker, "FAMILIES", (cpu_only, *others))


def test_resolve_extras_removed_backend_does_not_silently_fall_back(monkeypatch):
    """Regression for PR #61's canary-mlx removal: a saved `backend=mlx`
    choice on a family that no longer declares MLX must NOT silently
    pick up the CPU atom. Apple Silicon caps so the failure isn't
    a host-caps fallback — purely a catalog-removed one."""
    _patch_whisper_without_mlx(monkeypatch)
    sel = Selection()
    _enable(sel, "whisper", BACKEND_MLX)
    extras = install_picker.resolve_extras(sel, _apple_caps())
    assert "whisper-cpu" not in extras


def test_removed_backend_families_surfaces_only_removed_catalog(monkeypatch):
    """`Selection.removed_backend_families` returns enabled families
    whose saved backend isn't in the catalog anymore — drives the
    main-loop stderr warning."""
    _patch_whisper_without_mlx(monkeypatch)
    sel = Selection()
    _enable(sel, "whisper", BACKEND_MLX)  # removed from catalog → surfaces
    _enable(sel, "voxtral", BACKEND_MLX)  # not in catalog (Voxtral has no MLX) → also surfaces
    keys = {f.key for f in sel.removed_backend_families()}
    assert keys == {"whisper", "voxtral"}

    sel2 = Selection()
    _enable(sel2, "whisper", BACKEND_CPU)
    assert sel2.removed_backend_families() == []


def test_familydef_declares_backend():
    whisper = next(f for f in FAMILIES if f.key == "whisper")
    voxtral = next(f for f in FAMILIES if f.key == "voxtral")
    # Voxtral has only one backend declared; 'Both' is meaningless and
    # `declares_backend` rejects it. Same for MLX.
    assert whisper.declares_backend(BACKEND_BOTH) is True
    assert voxtral.declares_backend(BACKEND_BOTH) is False
    assert whisper.declares_backend(BACKEND_CPU) is True
    assert whisper.declares_backend(BACKEND_MLX) is True
    assert voxtral.declares_backend(BACKEND_MLX) is False
    # has_mlx is the BACKEND_MLX special case of declares_backend.
    assert whisper.has_mlx() is True
    assert voxtral.has_mlx() is False


def test_resolve_extras_preserves_family_order_for_reproducibility():
    sel = Selection()
    _enable(sel, "canary", BACKEND_CPU)
    _enable(sel, "whisper", BACKEND_CPU)
    _enable(sel, "parakeet", BACKEND_CPU)
    extras = install_picker.resolve_extras(sel, _caps())
    assert extras.index("whisper-live") < extras.index("parakeet-cpu")
    assert extras.index("parakeet-cpu") < extras.index("canary-cpu")


# ── pip argv construction ───────────────────────────────────────────


def test_build_pip_argv_uses_editable_install_with_extras():
    argv = install_picker.build_pip_argv(["whisper-live", "whisper-mlx"], python="/usr/bin/python3")
    assert argv == [
        "/usr/bin/python3",
        "-m",
        "pip",
        "install",
        "-e",
        ".[whisper-live,whisper-mlx]",
    ]


def test_build_pip_argv_drops_extras_brackets_when_empty():
    argv = install_picker.build_pip_argv([], python="/usr/bin/python3")
    assert argv[-1] == "."


# ── Picker command parsing (numbered fallback) ──────────────────────


def test_parse_command_enter_confirms():
    sel = Selection()
    assert install_picker._parse_command("", sel) == ""
    assert install_picker._parse_command("   \n", sel) == ""


def test_parse_command_q_quits():
    sel = Selection()
    assert install_picker._parse_command("q", sel) == "quit"
    assert install_picker._parse_command("quit", sel) == "quit"


def test_parse_command_toggles_single_number():
    sel = Selection.defaults_for(_caps())
    result = install_picker._parse_command("2", sel)
    assert "Voxtral" in result
    assert sel.choices["voxtral"].enabled is True


def test_parse_command_a_toggles_all_enable_flags_keeping_backends():
    """The 'toggle all' shortcut must NOT churn backend choices — only
    enable flags. Operators who painstakingly set Whisper=MLX shouldn't
    lose that choice when they press `a` to flip everything on."""
    sel = Selection.defaults_for(_apple_caps())
    sel.choices["whisper"] = FamilyChoice(enabled=False, backend=BACKEND_MLX)
    install_picker._parse_command("a", sel)
    assert sel.choices["whisper"].enabled is True
    assert sel.choices["whisper"].backend == BACKEND_MLX


def test_parse_command_r_resets_enable_flags():
    sel = Selection.defaults_for(_caps())
    sel.choices["voxtral"].enabled = True
    install_picker._parse_command("r", sel)
    assert sel.choices["voxtral"].enabled is False
    # Default-selected stay on.
    assert sel.choices["whisper"].enabled is True


def test_parse_command_ignores_bogus_tokens():
    sel = Selection.defaults_for(_caps())
    result = install_picker._parse_command("99 foo 2", sel)
    assert sel.choices["voxtral"].enabled is True
    assert "ignored" in result


# ── Interactive loop (numbered fallback driven by StringIO) ─────────


def test_interactive_loop_enter_confirms_immediately():
    sel = Selection.defaults_for(_caps())
    out = io.StringIO()
    inp = io.StringIO("\n")
    assert install_picker.interactive_loop(sel, _caps(), stream_in=inp, stream_out=out) is True


def test_interactive_loop_q_aborts_and_returns_false():
    sel = Selection.defaults_for(_caps())
    inp = io.StringIO("q\n")
    out = io.StringIO()
    assert install_picker.interactive_loop(sel, _caps(), stream_in=inp, stream_out=out) is False


def test_interactive_loop_toggles_then_confirms():
    sel = Selection.defaults_for(_caps())
    inp = io.StringIO("2\n\n")  # toggle voxtral, confirm
    out = io.StringIO()
    assert install_picker.interactive_loop(sel, _caps(), stream_in=inp, stream_out=out) is True
    assert sel.choices["voxtral"].enabled is True


def test_interactive_loop_eof_aborts():
    sel = Selection.defaults_for(_caps())
    inp = io.StringIO("")
    out = io.StringIO()
    assert install_picker.interactive_loop(sel, _caps(), stream_in=inp, stream_out=out) is False


# ── render() ────────────────────────────────────────────────────────


def test_render_includes_machine_summary():
    sel = Selection.defaults_for(_apple_caps())
    text = install_picker.render(sel, _apple_caps())
    assert "MLX detected" in text
    assert "[x] 1. Whisper" in text


def test_render_shows_backend_selector_with_radio_markers():
    """Per-family backend row is the centrepiece of the redesign — must
    show the selected backend with a filled circle and the others empty."""
    sel = Selection.defaults_for(_apple_caps())
    text = install_picker.render(sel, _apple_caps())
    whisper_block = "\n".join(
        text.split("\n\n")[1].splitlines()  # first family block
    )
    # MLX is the default on Apple Silicon → filled radio next to MLX.
    assert "● MLX" in whisper_block
    assert "○ CPU/CUDA" in whisper_block
    assert "○ Both" in whisper_block


def test_render_shows_only_option_label_when_one_backend():
    """Voxtral has no MLX adapter, so the picker shouldn't pretend there's
    a backend choice to make."""
    sel = Selection.defaults_for(_apple_caps())
    text = install_picker.render(sel, _apple_caps())
    assert "CPU/CUDA (only option" in text


def test_render_with_cursor_marks_current_row_and_shows_arrow_help():
    sel = Selection.defaults_for(_caps())
    text = install_picker.render(sel, _caps(), cursor=1)
    voxtral_line = next(line for line in text.splitlines() if "2. Voxtral" in line)
    assert voxtral_line.lstrip().startswith(">")
    assert "↑/↓" in text
    assert "←/→" in text  # backend cycling hint is in the arrow-mode footer
    assert "<numbers>" not in text


def test_render_shows_planned_pip_command_with_atomic_extras():
    sel = Selection()
    _enable(sel, "whisper", BACKEND_MLX)
    text = install_picker.render(sel, _apple_caps())
    assert "pip install -e '.[whisper-live,whisper-mlx]'" in text


def test_render_with_empty_selection_explains_consequences():
    sel = Selection()
    text = install_picker.render(sel, _caps())
    assert "nothing" in text or "empty" in text


# ── Arrow-key UI dispatch ────────────────────────────────────────────


def test_can_use_arrow_keys_false_for_stringio():
    assert install_picker._can_use_arrow_keys(io.StringIO(), io.StringIO()) is False


def test_handle_key_up_down_wraps():
    sel = Selection.defaults_for(_caps())
    cursor = [0]
    install_picker._handle_key("down", sel, cursor, _caps())
    assert cursor == [1]
    cursor[0] = 0
    install_picker._handle_key("up", sel, cursor, _caps())
    assert cursor == [len(FAMILIES) - 1]


def test_handle_key_space_toggles_enabled():
    sel = Selection.defaults_for(_apple_caps())
    sel.choices["whisper"].enabled = False
    install_picker._handle_key("space", sel, [0], _apple_caps())
    assert sel.choices["whisper"].enabled is True


@pytest.mark.parametrize(
    "direction, expected_sequence",
    [
        ("right", [BACKEND_MLX, BACKEND_BOTH, BACKEND_CPU]),
        ("left", [BACKEND_BOTH, BACKEND_MLX, BACKEND_CPU]),
    ],
    ids=["right→cpu→mlx→both→cpu", "left→cpu→both→mlx→cpu"],
)
def test_handle_key_cycles_backend(direction, expected_sequence):
    """The ←/→ flow is the user-visible answer to 'I want MLX-only for
    Whisper but Both for Parakeet'. Last item in `expected_sequence`
    confirms the cycle wraps."""
    sel = Selection()
    _enable(sel, "whisper", BACKEND_CPU)
    apple = _apple_caps()
    for expected in expected_sequence:
        install_picker._handle_key(direction, sel, [0], apple)
        assert sel.choices["whisper"].backend == expected


def test_handle_key_left_right_noop_when_one_backend():
    """On a CPU box there's no second backend to cycle to — ←/→ shouldn't
    accidentally rotate to MLX behind the scenes."""
    sel = Selection.defaults_for(_caps())
    # Cursor on whisper, only CPU backend available on plain Linux.
    install_picker._handle_key("right", sel, [0], _caps())
    assert sel.choices["whisper"].backend == BACKEND_CPU


def test_handle_key_a_preserves_backend_choices():
    sel = Selection.defaults_for(_apple_caps())
    sel.choices["whisper"] = FamilyChoice(enabled=False, backend=BACKEND_MLX)
    install_picker._handle_key("a", sel, [0], _apple_caps())
    assert sel.choices["whisper"].enabled is True
    assert sel.choices["whisper"].backend == BACKEND_MLX


def test_handle_key_enter_and_quit_sentinels():
    sel = Selection()
    assert install_picker._handle_key("enter", sel, [0], _caps()) == "confirm"
    assert install_picker._handle_key("q", sel, [0], _caps()) == "quit"
    assert install_picker._handle_key("esc", sel, [0], _caps()) == "quit"


def test_handle_key_digit_jumps_and_toggles():
    sel = Selection.defaults_for(_caps())
    sel.choices["parakeet"].enabled = False
    cursor = [0]
    install_picker._handle_key("3", sel, cursor, _caps())
    assert cursor == [2]
    assert sel.choices["parakeet"].enabled is True


# ── _classify_byte / _read_key_posix (raw-mode keystroke parsing) ────


@pytest.mark.parametrize(
    "raw, expected",
    [
        (b"\r", "enter"),
        (b"\n", "enter"),
        (b" ", "space"),
        (b"\t", "tab"),
        (b"\x03", "ctrl-c"),
        (b"\x04", "ctrl-d"),
        (b"\x7f", "backspace"),
        (b"\x1b", "esc"),
        (b"a", "a"),
        (b"Q", "q"),  # case-folded
        (b"\xff", "esc"),  # invalid UTF-8 byte falls back to esc
    ],
)
def test_classify_byte_symbolic_mapping(raw, expected):
    assert install_picker._classify_byte(raw) == expected


def _patch_posix_reader(monkeypatch, byte_stream: list[bytes], *, has_followup: bool = True) -> None:
    """Wire `os.read` + `select.select` so `_read_key_posix` consumes
    bytes from `byte_stream` in order. `has_followup=False` simulates
    'no more bytes after ESC', so a lone Esc resolves correctly."""
    it = iter(byte_stream)
    monkeypatch.setattr(install_picker.os, "read", lambda fd, n: next(it, b""))
    import select

    monkeypatch.setattr(
        select,
        "select",
        lambda r, w, x, t: ([0], [], []) if has_followup else ([], [], []),
    )


@pytest.mark.parametrize(
    "bytes_in, expected",
    [
        ([b"\x1b", b"[A"], "up"),
        ([b"\x1b", b"[B"], "down"),
        ([b"\x1b", b"[C"], "right"),
        ([b"\x1b", b"[D"], "left"),
        ([b"\x1b", b"OA"], "up"),  # alt application-mode encoding
    ],
)
def test_read_key_posix_decodes_arrow_escape_sequences(monkeypatch, bytes_in, expected):
    """The whole point of arrow-key UX — guard against a regression in
    the ESC-[A/B/C/D parsing."""
    _patch_posix_reader(monkeypatch, bytes_in, has_followup=True)
    assert install_picker._read_key_posix(0) == expected


def test_read_key_posix_lone_esc_returns_esc_after_timeout(monkeypatch):
    """When select() times out waiting for follow-on bytes, ESC alone
    means 'quit' — not 'start of unknown sequence'."""
    _patch_posix_reader(monkeypatch, [b"\x1b"], has_followup=False)
    assert install_picker._read_key_posix(0) == "esc"


def test_read_key_posix_returns_eof_on_empty_read(monkeypatch):
    monkeypatch.setattr(install_picker.os, "read", lambda fd, n: b"")
    assert install_picker._read_key_posix(0) == "eof"


def test_read_key_posix_returns_eof_on_oserror(monkeypatch):
    def boom(fd, n):
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(install_picker.os, "read", boom)
    assert install_picker._read_key_posix(0) == "eof"


def test_read_key_posix_passes_plain_chars_through_classify(monkeypatch):
    monkeypatch.setattr(install_picker.os, "read", lambda fd, n: b"a")
    assert install_picker._read_key_posix(0) == "a"


# ── _drive_picker (orchestration loop) ───────────────────────────────


def _scripted_reader(keys: list[str]):
    """Build a `read_key` callable that returns `keys` in order, then
    yields 'eof' forever — so a forgotten 'enter' in a test ends the
    loop instead of looping infinitely."""
    it = iter(keys)
    return lambda: next(it, "eof")


def test_drive_picker_confirms_on_enter():
    sel = Selection.defaults_for(_caps())
    paints: list[int] = []
    result = install_picker._drive_picker(
        sel, _caps(), paint=paints.append, read_key=_scripted_reader(["enter"])
    )
    assert result is True
    assert len(paints) == 1


def test_drive_picker_quits_on_q():
    sel = Selection.defaults_for(_caps())
    result = install_picker._drive_picker(
        sel, _caps(), paint=lambda _c: None, read_key=_scripted_reader(["q"])
    )
    assert result is False


def test_drive_picker_routes_keys_through_handle_key():
    """End-to-end: a scripted keystroke sequence mutates the Selection
    the same way calling _handle_key directly would. This is the
    closest thing to a real arrow-key UI test that runs without a TTY."""
    sel = Selection.defaults_for(_apple_caps())
    sel.choices["whisper"] = FamilyChoice(enabled=True, backend=BACKEND_CPU)
    result = install_picker._drive_picker(
        sel,
        _apple_caps(),
        paint=lambda _c: None,
        # ↓ toggle voxtral (#2), then walk back up to whisper and
        # cycle its backend MLX→Both→CPU+1 to land on MLX, then confirm.
        read_key=_scripted_reader(["down", "space", "up", "right", "enter"]),
    )
    assert result is True
    assert sel.choices["voxtral"].enabled is True
    assert sel.choices["whisper"].backend == BACKEND_MLX


def test_drive_picker_pre_positions_cursor_on_first_enabled_row():
    """Cursor lands on Voxtral when Whisper is off but Voxtral is on —
    operators returning to the picker see their actual current state."""
    sel = Selection()
    sel.choices["voxtral"] = FamilyChoice(enabled=True)
    paints: list[int] = []
    install_picker._drive_picker(sel, _caps(), paint=paints.append, read_key=_scripted_reader(["enter"]))
    assert paints == [1]  # voxtral's index


# ── pyproject extras: pip resolution regression ─────────────────────


def _atomic_extras(extra_name: str) -> list[str]:
    """Pull one `[project.optional-dependencies]` entry out of pyproject."""
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    extras = data["project"]["optional-dependencies"]
    assert extra_name in extras, f"no `{extra_name}` extra in pyproject.toml"
    return list(extras[extra_name])


def _requirement_for(lines: list[str], project_name: str) -> Requirement:
    for line in lines:
        req = Requirement(line)
        if req.name == project_name:
            return req
    raise AssertionError(f"no requirement named {project_name!r} in {lines!r}")


def test_pyproject_whisper_mlx_admits_a_real_release():
    """Regression for the install error pasted in the PR description.

    The atomic `whisper-mlx` extra (formerly `mlx`) used to declare
    `mlx-whisper>=0.5`, but PyPI tops out at 0.4.x — so every Apple
    Silicon install that resolved the extra failed with "No matching
    distribution found". Guard against re-introducing an unsatisfiable
    floor."""
    req = _requirement_for(_atomic_extras("whisper-mlx"), "mlx-whisper")
    pypi_published = ["0.1.0", "0.2.0", "0.3.0", "0.4.0", "0.4.1", "0.4.2", "0.4.3"]
    satisfying = [v for v in pypi_published if Version(v) in req.specifier]
    assert satisfying, (
        f"mlx-whisper specifier {req.specifier!r} is not satisfied by any "
        f"version PyPI is known to publish ({pypi_published}). This is the "
        "exact failure mode that broke `bash start.sh` on Apple Silicon. "
        "Lower the floor to a version that exists."
    )


@pytest.mark.parametrize(
    "extra_name, pkg",
    [
        ("whisper-mlx", "mlx-whisper"),
        ("parakeet-mlx", "parakeet-mlx"),
    ],
)
def test_pyproject_mlx_extras_stay_platform_gated(extra_name, pkg):
    """The MLX-only atomic extras must keep their Darwin/arm64 env marker
    so pip on Linux/Windows/Intel-Mac skips them instead of erroring out
    on wheels that don't exist for those platforms."""
    req = _requirement_for(_atomic_extras(extra_name), pkg)
    assert req.marker is not None, f"{extra_name} → {pkg} must stay sys_platform-gated"
    marker = str(req.marker)
    assert "darwin" in marker and "arm64" in marker, (
        f"{extra_name} → {pkg} marker {marker!r} dropped Darwin+arm64 gating"
    )


def test_pyproject_cpu_extras_do_not_pull_mlx_packages():
    """The whole point of splitting `whisper` into atoms: a Linux CI box
    that installs `.[whisper-cpu]` must NOT see mlx-whisper in the
    resolved set."""
    cpu = _atomic_extras("whisper-cpu")
    assert not any("mlx" in line for line in cpu), cpu


def _requirements_named(lines: list[str], project_name: str) -> list[Requirement]:
    """All requirements matching `project_name`. The macOS NeMo fix
    declares the same package twice with different `sys_platform`
    markers, so callers need to look at the full set."""
    reqs = [Requirement(line) for line in lines]
    return [r for r in reqs if r.name == project_name]


@pytest.mark.parametrize("extra_name", ["canary-cpu", "parakeet-cpu"])
def test_pyproject_nemo_extras_pin_macos_to_pre_kaldialign_cap(extra_name):
    """Regression for the Mac mini "Build kaldialign failed" install
    failure:

    * `kaldialign 0.10.0` (released 2026-05-06) dropped macOS wheels.
    * `nemo_toolkit 2.6+` adds `Requires-Dist: kaldialign<=0.9.1`, and
      kaldialign 0.9.1 has no cp313 macOS wheel — only 0.9.2 and 0.9.3
      do.
    * Result: an unconstrained pyproject leaves pip resolving NeMo
      latest + kaldialign<=0.9.1 on macOS py3.13, then falling back to
      an sdist build of kaldialign that fails on a stock Mac mini.

    Fix: on macOS, cap NeMo to <2.6 (kaldialign unconstrained there)
    and pin kaldialign to `>=0.9.2,<0.10` (the wheel-available window).
    On Linux/Windows neither cap applies and pip stays on the latest
    NeMo.

    This test pins the shape so a future "cleanup" PR can't strip
    either half of the fix without understanding why both halves
    exist."""
    extras = _atomic_extras(extra_name)
    nemo_reqs = _requirements_named(extras, "nemo_toolkit")
    kaldi_reqs = _requirements_named(extras, "kaldialign")

    assert len(nemo_reqs) == 2, (
        f"{extra_name} must declare nemo_toolkit twice — once for "
        "sys_platform != 'darwin' (unbounded) and once for == 'darwin' "
        "(<2.6 to avoid NeMo's kaldialign<=0.9.1 cap). Got: "
        f"{[str(r) for r in nemo_reqs]}"
    )
    # `packaging` normalises marker quoting to double-quotes; pyproject
    # source can use either, so compare on the normalised form.
    darwin_nemo = [r for r in nemo_reqs if r.marker and '== "darwin"' in str(r.marker)]
    nondarwin_nemo = [r for r in nemo_reqs if r.marker and '!= "darwin"' in str(r.marker)]
    assert darwin_nemo and nondarwin_nemo, (
        f"{extra_name} → nemo_toolkit markers must be both `== 'darwin'` and "
        f"`!= 'darwin'`. Got: {[str(r) for r in nemo_reqs]}"
    )
    assert Version("2.5.3") in darwin_nemo[0].specifier, (
        f"{extra_name} darwin nemo specifier {darwin_nemo[0].specifier!r} must "
        "still admit the last 2.5.x release; that's the one without the "
        "kaldialign<=0.9.1 cap."
    )
    assert Version("2.7.3") not in darwin_nemo[0].specifier, (
        f"{extra_name} darwin nemo specifier {darwin_nemo[0].specifier!r} admits "
        "2.7.3, which pins kaldialign<=0.9.1 and re-introduces the macOS sdist "
        "build path."
    )

    assert len(kaldi_reqs) == 1 and kaldi_reqs[0].marker is not None, (
        f"{extra_name} → kaldialign requirement must be present and sys_platform-gated"
    )
    spec = kaldi_reqs[0].specifier
    assert "darwin" in str(kaldi_reqs[0].marker), (
        f"{extra_name} → kaldialign marker {kaldi_reqs[0].marker!r} dropped the "
        "macOS gate; Linux/Windows must keep the upstream-NeMo-pinned version."
    )
    assert Version("0.9.3") in spec and Version("0.9.2") in spec, (
        f"{extra_name} → kaldialign specifier {spec!r} excludes the only "
        "wheel-available versions on macOS arm64 cp313 (0.9.2 and 0.9.3)"
    )
    assert Version("0.9.1") not in spec, (
        f"{extra_name} → kaldialign specifier {spec!r} admits 0.9.1, which has "
        "no cp313 macOS wheel and forces a source build that fails on a stock "
        "Mac mini."
    )
    assert Version("0.10.0") not in spec, (
        f"{extra_name} → kaldialign specifier {spec!r} admits 0.10.0, which has no macOS wheel at all."
    )


def test_pyproject_parakeet_alias_is_mlx_only_on_apple_silicon():
    """Regression for the `tapscribe[parakeet]` install failure on
    macos-latest + py3.13: pulling `parakeet-cpu` (NeMo 2.5.x via our
    macOS cap) alongside `parakeet-mlx` causes pip's resolver to flag a
    transitive conflict between the two sub-graphs. On Apple Silicon
    parakeet-mlx alone is the right backend (faster, GPU, no NeMo
    dependency), so the alias gates the CPU atom out via a PEP 508
    marker. Without this gating, `pip install -e .[parakeet]` on a Mac
    mini fails with ResolutionImpossible."""
    parakeet_lines = _atomic_extras("parakeet")
    # Exactly two marker-gated self-references: one Apple-Silicon-only,
    # one everywhere-else.
    assert len(parakeet_lines) == 2, (
        f"parakeet alias must declare two marker-gated entries; got: {parakeet_lines}"
    )
    darwin_line = next((line for line in parakeet_lines if "parakeet-mlx" in line), None)
    other_line = next((line for line in parakeet_lines if "parakeet-cpu" in line), None)
    assert darwin_line is not None, (
        f"parakeet alias must include a parakeet-mlx-only entry for Apple Silicon; got: {parakeet_lines}"
    )
    assert other_line is not None, (
        f"parakeet alias must include a parakeet-cpu entry for non-Apple-Silicon hosts; got: {parakeet_lines}"
    )
    # The darwin/arm64 line must NOT mention parakeet-cpu — that's the
    # whole point of the split.
    assert "parakeet-cpu" not in darwin_line, (
        "parakeet alias's Apple-Silicon branch pulled in parakeet-cpu, which "
        "re-introduces the NeMo + parakeet-mlx resolver conflict. The atom must "
        f"be MLX-only on darwin+arm64. Got: {darwin_line!r}"
    )


def test_picker_apple_silicon_mlx_only_matches_failing_invocation_atoms():
    """End-to-end: the failure log was for
       tapscribe[canary,mlx,parakeet,parakeet-mlx,whisper]
    so reproduce the post-split equivalent and confirm the picker still
    resolves it without dragging in the `whisper-cpu` atom when the
    operator explicitly chose MLX-only on Apple Silicon. Canary has no
    MLX backend so a Mac-only selection covers just Whisper and
    Parakeet here."""
    sel = Selection()
    _enable(sel, "whisper", BACKEND_MLX)
    _enable(sel, "parakeet", BACKEND_MLX)
    extras = install_picker.resolve_extras(sel, _apple_caps())
    assert extras == [
        "whisper-live",
        "whisper-mlx",
        "parakeet-mlx",
    ]


# ── detect_caps ─────────────────────────────────────────────────────


def test_detect_caps_no_mlx_flag_forces_mlx_false(monkeypatch):
    monkeypatch.setattr(install_picker.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(install_picker.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(install_picker, "_which", lambda _: None)
    caps = install_picker.detect_caps(force_no_mlx=True)
    assert caps.mlx is False


def test_detect_caps_mlx_true_on_apple_silicon(monkeypatch):
    monkeypatch.setattr(install_picker.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(install_picker.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(install_picker, "_which", lambda _: None)
    caps = install_picker.detect_caps()
    assert caps.mlx is True
    assert caps.cuda is False


def test_detect_caps_cuda_false_when_nvidia_smi_missing(monkeypatch):
    monkeypatch.setattr(install_picker.platform, "system", lambda: "Linux")
    monkeypatch.setattr(install_picker.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(install_picker, "_which", lambda _: None)
    caps = install_picker.detect_caps()
    assert caps.cuda is False
    assert caps.mlx is False
