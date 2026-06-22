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

import sys
from pathlib import Path

# tools/ isn't a package — make install_picker importable by name (same as
# tests/test_install_picker.py). Only the TEST imports it; the app never does.
_TOOLS_DIR = Path(__file__).resolve().parent.parent / "tools"
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

import install_picker  # noqa: E402

from tapscribe.setup_install import (  # noqa: E402
    picker_install_argv,
    to_picker_state,
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
