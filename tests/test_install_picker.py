"""Tests for tools/install_picker.py — the family-selection bootstrap.

The picker is a standalone stdlib-only script (it runs before TapScribe's
extras are installed), so these tests import it via path manipulation
rather than as a package module.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

# tools/ isn't a package — make install_picker importable by name.
TOOLS_DIR = Path(__file__).resolve().parent.parent / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import install_picker  # noqa: E402


@pytest.fixture
def tmp_state(monkeypatch, tmp_path):
    """Point STATE_FILE at a fresh tmpdir so tests don't touch the
    operator's real `.tapscribe-install.json`."""
    state = tmp_path / ".tapscribe-install.json"
    monkeypatch.setattr(install_picker, "STATE_FILE", state)
    return state


# ── Selection persistence ───────────────────────────────────────────


def test_selection_load_returns_defaults_when_file_missing(tmp_state):
    sel = install_picker.Selection.load(tmp_state)
    # Default seed = whatever families are flagged default_selected=True.
    expected = {f.key for f in install_picker.FAMILIES if f.default_selected}
    assert sel.families == expected
    assert "whisper" in sel.families  # baseline


def test_selection_round_trips_through_disk(tmp_state):
    written = install_picker.Selection(families={"whisper", "voxtral"})
    written.save(tmp_state)
    assert tmp_state.exists()
    loaded = install_picker.Selection.load(tmp_state)
    assert loaded.families == {"whisper", "voxtral"}


def test_selection_load_ignores_unknown_family_keys(tmp_state):
    """A state file from a future TapScribe version that mentions a
    family this build doesn't know about must not crash the picker."""
    tmp_state.write_text(json.dumps({"families": ["whisper", "future-family-x"]}))
    sel = install_picker.Selection.load(tmp_state)
    assert sel.families == {"whisper"}


def test_selection_load_handles_malformed_file(tmp_state):
    tmp_state.write_text("not json at all")
    sel = install_picker.Selection.load(tmp_state)
    # Fell back to defaults instead of raising.
    expected = {f.key for f in install_picker.FAMILIES if f.default_selected}
    assert sel.families == expected


# ── Extras resolution ───────────────────────────────────────────────


def _caps(*, mlx: bool = False, cuda: bool = False) -> install_picker.MachineCaps:
    return install_picker.MachineCaps(os_name="Linux", arch="x86_64", mlx=mlx, cuda=cuda)


def test_resolve_extras_empty_selection_emits_no_extras():
    assert install_picker.resolve_extras(install_picker.Selection(), _caps()) == []


def test_resolve_extras_whisper_only_on_cpu():
    sel = install_picker.Selection(families={"whisper"})
    assert install_picker.resolve_extras(sel, _caps()) == ["whisper"]


def test_resolve_extras_whisper_adds_mlx_extra_on_apple_silicon():
    sel = install_picker.Selection(families={"whisper"})
    extras = install_picker.resolve_extras(sel, _caps(mlx=True))
    assert extras == ["whisper", "mlx"]


def test_resolve_extras_parakeet_picks_parakeet_mlx_when_mlx_present():
    sel = install_picker.Selection(families={"parakeet"})
    extras = install_picker.resolve_extras(sel, _caps(mlx=True))
    assert "parakeet" in extras
    assert "parakeet-mlx" in extras


def test_resolve_extras_canary_does_not_repeat_extras():
    """The canary extra already bundles mlx-audio via env marker, so
    we don't add a separate MLX-flavoured extra; the result list must
    not contain duplicates."""
    sel = install_picker.Selection(families={"canary"})
    extras = install_picker.resolve_extras(sel, _caps(mlx=True))
    assert extras == ["canary"]


def test_resolve_extras_preserves_family_order_for_reproducibility():
    """Stable order across runs = stable `pip install` invocation =
    operators can compare picker output across launches."""
    sel = install_picker.Selection(families={"canary", "whisper", "parakeet"})
    extras = install_picker.resolve_extras(sel, _caps())
    # whisper comes before parakeet which comes before canary in FAMILIES.
    assert extras.index("whisper") < extras.index("parakeet")
    assert extras.index("parakeet") < extras.index("canary")


# ── pip argv construction ───────────────────────────────────────────


def test_build_pip_argv_uses_editable_install_with_extras():
    argv = install_picker.build_pip_argv(["whisper", "voxtral"], python="/usr/bin/python3")
    assert argv == ["/usr/bin/python3", "-m", "pip", "install", "-e", ".[whisper,voxtral]"]


def test_build_pip_argv_drops_extras_brackets_when_empty():
    argv = install_picker.build_pip_argv([], python="/usr/bin/python3")
    assert argv[-1] == "."


# ── Picker command parsing ──────────────────────────────────────────


def test_parse_command_enter_confirms():
    sel = install_picker.Selection(families={"whisper"})
    assert install_picker._parse_command("", sel) == ""
    assert install_picker._parse_command("   \n", sel) == ""


def test_parse_command_q_quits():
    sel = install_picker.Selection()
    assert install_picker._parse_command("q", sel) == "quit"
    assert install_picker._parse_command("quit", sel) == "quit"


def test_parse_command_toggles_single_number():
    sel = install_picker.Selection(families={"whisper"})
    # Voxtral is family #2 in the FAMILIES tuple ordering.
    result = install_picker._parse_command("2", sel)
    assert "Voxtral" in result
    assert "voxtral" in sel.families


def test_parse_command_toggles_comma_separated_numbers():
    sel = install_picker.Selection(families=set())
    install_picker._parse_command("1, 3", sel)
    assert sel.families == {"whisper", "parakeet"}
    # Toggling them again clears.
    install_picker._parse_command("1 3", sel)
    assert sel.families == set()


def test_parse_command_a_toggles_all():
    sel = install_picker.Selection(families={"whisper"})
    install_picker._parse_command("a", sel)
    assert sel.families == {f.key for f in install_picker.FAMILIES}
    install_picker._parse_command("a", sel)
    assert sel.families == set()


def test_parse_command_r_resets_to_defaults():
    sel = install_picker.Selection(families={"voxtral", "parakeet", "canary"})
    install_picker._parse_command("r", sel)
    assert sel.families == {f.key for f in install_picker.FAMILIES if f.default_selected}


def test_parse_command_ignores_bogus_tokens():
    sel = install_picker.Selection(families={"whisper"})
    result = install_picker._parse_command("99 foo 2", sel)
    # Family #2 toggled (now on); 99 and 'foo' were ignored without raising.
    assert "voxtral" in sel.families
    assert "ignored" in result


# ── Interactive loop (driven by StringIO so it's deterministic) ─────


def test_interactive_loop_enter_confirms_immediately():
    sel = install_picker.Selection(families={"whisper"})
    caps = _caps()
    out = io.StringIO()
    inp = io.StringIO("\n")
    assert install_picker.interactive_loop(sel, caps, stream_in=inp, stream_out=out) is True
    # Nothing changed.
    assert sel.families == {"whisper"}


def test_interactive_loop_q_aborts_and_returns_false():
    sel = install_picker.Selection(families={"whisper"})
    caps = _caps()
    inp = io.StringIO("q\n")
    out = io.StringIO()
    assert install_picker.interactive_loop(sel, caps, stream_in=inp, stream_out=out) is False


def test_interactive_loop_toggles_then_confirms():
    sel = install_picker.Selection(families={"whisper"})
    caps = _caps()
    inp = io.StringIO("2\n\n")  # toggle voxtral, then confirm
    out = io.StringIO()
    assert install_picker.interactive_loop(sel, caps, stream_in=inp, stream_out=out) is True
    assert sel.families == {"whisper", "voxtral"}


def test_interactive_loop_eof_aborts():
    """Closed stdin (e.g. piped invocation) must not infinite-loop; the
    loop should treat EOF as an abort so start.sh exits cleanly."""
    sel = install_picker.Selection(families={"whisper"})
    caps = _caps()
    inp = io.StringIO("")  # immediate EOF
    out = io.StringIO()
    assert install_picker.interactive_loop(sel, caps, stream_in=inp, stream_out=out) is False


# ── render() ────────────────────────────────────────────────────────


def test_render_includes_machine_summary():
    sel = install_picker.Selection(families={"whisper"})
    text = install_picker.render(sel, _caps(mlx=True))
    assert "MLX detected" in text
    # The whisper line shows a checked box.
    assert "[x] 1. Whisper" in text


def test_render_shows_planned_pip_command_when_extras_resolved():
    sel = install_picker.Selection(families={"whisper"})
    text = install_picker.render(sel, _caps(mlx=True))
    assert "pip install" in text
    assert "whisper" in text and "mlx" in text


def test_render_with_empty_selection_explains_consequences():
    sel = install_picker.Selection(families=set())
    text = install_picker.render(sel, _caps())
    assert "nothing" in text or "empty" in text


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
