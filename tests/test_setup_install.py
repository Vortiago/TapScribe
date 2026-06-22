"""Install resolver — translates a catalog-family selection (what the browser
setup UI shows) into the dependency-free install picker's selection
(`.tapscribe-install.json` v2), which the app then runs `--non-interactive`.

The translation is the integration seam between the catalog families the UI
speaks (whisper, nb-whisper, voxtral, parakeet) and the picker's INSTALL
families (whisper covers both Whisper + NB-Whisper). We validate it against the
picker's OWN `resolve_extras` so the produced selection is pinned to the real
extras logic — if the picker's family/backend model changes, these fail.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# tools/ isn't a package — make install_picker importable by name (same as
# tests/test_install_picker.py). Only the TEST imports it; the app never does.
_TOOLS_DIR = Path(__file__).resolve().parent.parent / "tools"
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

import install_picker  # noqa: E402
import pytest  # noqa: E402

from tapscribe.setup_install import (  # noqa: E402
    InstallSelectionError,
    picker_install_argv,
    run_install,
    sse,
    to_picker_state,
    validate_selection,
    write_picker_state,
)

_MAC = install_picker.MachineCaps(os_name="Darwin", arch="arm64", mlx=True, cuda=False)
_CUDA = install_picker.MachineCaps(os_name="Linux", arch="x86_64", mlx=False, cuda=True)
_CPU = install_picker.MachineCaps(os_name="Linux", arch="x86_64", mlx=False, cuda=False)


def _extras(selection: dict[str, str], caps) -> set[str]:
    """Resolve a catalog-family selection to pip extras THROUGH the real picker."""
    state = to_picker_state(selection)
    sel = install_picker.Selection._load_v2(state, caps)
    return set(install_picker.resolve_extras(sel, caps))


def test_state_has_picker_v2_shape():
    state = to_picker_state({"whisper": "cpu"})
    assert state["version"] == install_picker.STATE_VERSION
    assert set(state["choices"]) == {"whisper", "voxtral", "parakeet"}


def test_nb_whisper_resolves_to_the_faster_whisper_extra():
    # NB-Whisper installs via the whisper family's CPU/CUDA (faster-whisper)
    # backend — not a separate extra, and never the MLX one.
    assert _extras({"nb-whisper": "cpu"}, _CPU) == {"whisper-live", "whisper-cpu"}


def test_whisper_mlx_resolves_to_the_mlx_extra():
    assert _extras({"whisper": "mlx"}, _MAC) == {"whisper-live", "whisper-mlx"}


def test_whisper_mlx_plus_nb_whisper_merges_to_both_backends():
    # The shared whisper family gets backend "both": MLX (for Whisper) AND
    # faster-whisper (for NB-Whisper). whisper-live dedups to one.
    assert _extras({"whisper": "mlx", "nb-whisper": "cpu"}, _MAC) == {
        "whisper-live",
        "whisper-mlx",
        "whisper-cpu",
    }


def test_cuda_kind_uses_cpu_backend_and_appends_cuda_libs():
    # "cuda" maps to the picker's CPU/CUDA (faster-whisper) backend; the picker
    # appends cuda-libs because whisper-cpu is in the install and CUDA is present.
    assert _extras({"whisper": "cuda"}, _CUDA) == {"whisper-live", "whisper-cpu", "cuda-libs"}


def test_parakeet_and_voxtral_map_through_without_cuda_libs():
    # Torch-based backends bundle their own CUDA, so no cuda-libs even on a
    # CUDA host (it's tied to whisper-cpu specifically).
    assert _extras({"parakeet": "cuda", "voxtral": "cpu"}, _CUDA) == {
        "parakeet-cpu",
        "voxtral-cpu",
    }


def test_unselected_families_are_disabled():
    state = to_picker_state({"whisper": "cpu"})
    assert state["choices"]["whisper"]["enabled"] is True
    assert state["choices"]["voxtral"]["enabled"] is False
    assert state["choices"]["parakeet"]["enabled"] is False


def test_empty_selection_installs_nothing():
    assert _extras({}, _CPU) == set()


def test_picker_install_argv_runs_the_picker_non_interactively():
    argv = picker_install_argv(python="/venv/bin/python")
    assert argv[0] == "/venv/bin/python"
    assert argv[1].endswith("tools/install_picker.py")
    assert "--non-interactive" in argv
    assert "--no-mlx" not in argv
    assert "--no-mlx" in picker_install_argv(python="py", no_mlx=True)


def test_write_picker_state_roundtrips_through_picker_load(tmp_path):
    state = to_picker_state({"whisper": "mlx", "parakeet": "cpu"})
    path = tmp_path / ".tapscribe-install.json"
    write_picker_state(state, path=path)
    sel = install_picker.Selection.load(path, _MAC)
    assert sel.choices["whisper"].enabled is True
    assert sel.choices["whisper"].backend == "mlx"
    assert sel.choices["parakeet"].enabled is True
    assert sel.choices["voxtral"].enabled is False


# ── validation ──────────────────────────────────────────────────────────────


def test_validate_selection_accepts_known_families_and_kinds():
    assert validate_selection({"whisper": "mlx", "nb-whisper": "cpu"}) == {
        "whisper": "mlx",
        "nb-whisper": "cpu",
    }


def test_validate_selection_rejects_unknown_family():
    with pytest.raises(InstallSelectionError):
        validate_selection({"whisper-evil": "cpu"})


def test_validate_selection_rejects_unknown_backend():
    with pytest.raises(InstallSelectionError):
        validate_selection({"whisper": "tpu"})


def test_validate_selection_rejects_non_mapping():
    with pytest.raises(InstallSelectionError):
        validate_selection(["whisper"])


# ── SSE framing ──────────────────────────────────────────────────────────────


def test_sse_frames_an_event_as_data_block():
    line = sse({"phase": "start"})
    assert line.startswith("data: ")
    assert line.endswith("\n\n")
    assert json.loads(line[len("data: ") :].strip()) == {"phase": "start"}


# ── streaming runner (fake subprocess) ───────────────────────────────────────


def _aiter(lines):
    async def gen():
        for ln in lines:
            yield ln

    return gen()


class _FakeProc:
    def __init__(self, lines, returncode):
        self.stdout = _aiter(lines)
        self._rc = returncode
        self.returncode = None

    async def wait(self):
        self.returncode = self._rc
        return self._rc


def _fake_spawn(lines, returncode):
    async def spawn(_argv):
        return _FakeProc(lines, returncode)

    return spawn


async def _collect(selection, *, lines, returncode, on_success=None):
    written = {}
    events = []
    async for ev in run_install(
        selection,
        spawn=_fake_spawn(lines, returncode),
        write_state=lambda state, **_: written.update(state),
        on_success=on_success,
    ):
        events.append(ev)
    return events, written


async def test_run_install_streams_start_logs_then_done_on_success():
    calls = []
    events, written = await _collect(
        {"whisper": "cpu"},
        lines=[b"resolving wheels\n", b"installed\n"],
        returncode=0,
        on_success=lambda: calls.append("reload"),
    )
    phases = [e["phase"] for e in events]
    assert phases == ["start", "log", "log", "done"]
    assert [e["line"] for e in events if e["phase"] == "log"] == ["resolving wheels", "installed"]
    assert events[-1] == {"phase": "done", "ok": True, "returncode": 0}
    assert calls == ["reload"]  # hot-reload fired exactly once, on success
    assert written["choices"]["whisper"]["enabled"] is True  # selection was written


async def test_run_install_reports_error_and_skips_reload_on_failure():
    calls = []
    events, _ = await _collect(
        {"whisper": "cpu"},
        lines=[b"resolving\n", b"ERROR: no matching distribution\n"],
        returncode=1,
        on_success=lambda: calls.append("reload"),
    )
    assert events[-1] == {"phase": "error", "ok": False, "returncode": 1}
    assert calls == []  # reload must NOT fire on a failed install


async def test_run_install_emits_error_event_when_spawn_fails():
    # Headers are already sent by the time the subprocess is spawned, so a spawn
    # failure (bad interpreter, OS refuses fork, …) must surface as a terminal
    # error event — not a truncated stream + 500.
    calls = []

    async def boom(_argv):
        raise FileNotFoundError("python missing")

    events = [
        ev
        async for ev in run_install(
            {"whisper": "cpu"},
            spawn=boom,
            write_state=lambda *a, **k: None,
            on_success=lambda: calls.append("reload"),
        )
    ]
    assert events[0] == {"phase": "start"}
    assert events[-1]["phase"] == "error" and events[-1]["ok"] is False
    assert "python missing" in events[-1]["message"]
    assert calls == []  # reload must NOT fire when the install never ran


# ── hot-reload primitive ─────────────────────────────────────────────────────


def test_refresh_backend_probes_reenables_detection():
    from tapscribe.transcribers.catalog import (
        available_backends,
        refresh_backend_probes,
        set_available_backends_for_testing,
    )

    set_available_backends_for_testing(frozenset({"made-up-kind"}))
    assert available_backends() == frozenset({"made-up-kind"})
    try:
        refresh_backend_probes()  # clears caches → next probe re-detects the real host
        assert "cpu" in available_backends()  # cpu is always present
        assert "made-up-kind" not in available_backends()
    finally:
        set_available_backends_for_testing(None)
