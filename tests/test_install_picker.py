"""Tests for tapscribe/install_picker.py — per-family + per-backend bootstrap.

The picker is stdlib-only (it runs before TapScribe's extras are installed) but
now lives in the package, because `tools/` isn't shipped in the wheel and a
Bundle installs a wheel (ADR-0015). Importing it is free — `tapscribe/__init__.py`
has no imports — so these tests import it normally rather than via the old
`sys.path` manipulation.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest
from conftest import atomic_extras
from packaging.requirements import Requirement
from packaging.version import Version

from tapscribe import install_picker
from tapscribe.install_picker import (
    BACKEND_BOTH,
    BACKEND_CPU,
    BACKEND_MLX,
    FAMILIES,
    FamilyChoice,
    MachineCaps,
    Selection,
)


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
    _enable(sel, "voxtral", BACKEND_CPU)
    _enable(sel, "whisper", BACKEND_CPU)
    _enable(sel, "parakeet", BACKEND_CPU)
    extras = install_picker.resolve_extras(sel, _caps())
    # FAMILIES order (whisper, voxtral, parakeet) is preserved regardless of
    # the order the operator toggled them in.
    assert extras.index("whisper-live") < extras.index("voxtral-cpu")
    assert extras.index("voxtral-cpu") < extras.index("parakeet-cpu")


# ── CUDA runtime libs (faster-whisper / CTranslate2 GPU path) ────────


def test_resolve_extras_appends_cuda_libs_for_whisper_cpu_on_cuda_box():
    """CTranslate2 (faster-whisper) doesn't bundle cuBLAS/cuDNN, so a CUDA
    box that installs the Whisper CPU/CUDA backend gets `cuda-libs`
    appended automatically — otherwise the GPU path fails with
    'cublas64_12.dll is not found'."""
    sel = Selection()
    _enable(sel, "whisper", BACKEND_CPU)
    extras = install_picker.resolve_extras(sel, _caps(cuda=True))
    assert install_picker.CUDA_RUNTIME_EXTRA in extras
    # Appended after the family atoms it backs, not before them.
    assert extras.index("whisper-cpu") < extras.index(install_picker.CUDA_RUNTIME_EXTRA)
    # Whole resolved set, pinned for reproducibility.
    assert extras == ["whisper-live", "whisper-cpu", "cuda-libs"]


def test_resolve_extras_no_cuda_libs_without_cuda():
    """No nvidia-smi → no CUDA → don't drag in the runtime libs even with
    the Whisper CPU backend selected (it'll just run on CPU)."""
    sel = Selection()
    _enable(sel, "whisper", BACKEND_CPU)
    extras = install_picker.resolve_extras(sel, _caps(cuda=False))
    assert install_picker.CUDA_RUNTIME_EXTRA not in extras


def test_resolve_extras_no_cuda_libs_for_mlx_only_whisper():
    """An MLX-only Whisper selection (Apple Silicon) has no faster-whisper
    atom and isn't on CUDA, so the libs must not be added — even if caps
    somehow reported cuda=True (Apple boxes never do, but tie the gate to
    the whisper-cpu atom, not the OS)."""
    sel = Selection()
    _enable(sel, "whisper", BACKEND_MLX)
    # Apple caps (cuda=False) — the realistic case.
    extras_apple = install_picker.resolve_extras(sel, _apple_caps())
    assert "whisper-cpu" not in extras_apple
    assert install_picker.CUDA_RUNTIME_EXTRA not in extras_apple
    # And even with a contrived cuda=True on an MLX-capable box: still no
    # whisper-cpu atom → still no cuda-libs.
    cuda_mlx = MachineCaps(os_name="Darwin", arch="arm64", mlx=True, cuda=True)
    extras_both = install_picker.resolve_extras(sel, cuda_mlx)
    assert "whisper-cpu" not in extras_both
    assert install_picker.CUDA_RUNTIME_EXTRA not in extras_both


def test_resolve_extras_no_cuda_libs_for_torch_only_selection():
    """Parakeet (transformers, Torch-based) gets CUDA from Torch's own
    bundle — no whisper-cpu atom in the install means no cuda-libs, even
    on a CUDA box."""
    sel = Selection()
    _enable(sel, "parakeet", BACKEND_CPU)
    _enable(sel, "voxtral", BACKEND_CPU)
    extras = install_picker.resolve_extras(sel, _caps(cuda=True))
    assert "whisper-cpu" not in extras
    assert install_picker.CUDA_RUNTIME_EXTRA not in extras


def test_resolve_extras_whisper_both_on_cuda_box_still_adds_cuda_libs():
    """'Both' includes whisper-cpu, so a CUDA box gets the libs once,
    appended after the MLX atom (which the Linux host can't use but the
    selection still resolves cleanly)."""
    sel = Selection()
    _enable(sel, "whisper", BACKEND_BOTH)
    # Linux+CUDA: MLX atom downgrades out (host can't run it), CPU stays.
    extras = install_picker.resolve_extras(sel, _caps(cuda=True))
    assert "whisper-cpu" in extras
    assert install_picker.CUDA_RUNTIME_EXTRA in extras
    # Dedupe: the libs appear exactly once.
    assert extras.count(install_picker.CUDA_RUNTIME_EXTRA) == 1


def test_pyproject_declares_cuda_libs_extra_gated_off_macos():
    """The `cuda-libs` extra must exist and keep its non-darwin marker so
    a macOS install (no CUDA there) skips the wheels instead of failing
    to resolve them."""
    lines = atomic_extras(install_picker.CUDA_RUNTIME_EXTRA)
    for pkg in ("nvidia-cublas-cu12", "nvidia-cudnn-cu12"):
        req = _requirement_for(lines, pkg)
        assert req.marker is not None, f"{pkg} in cuda-libs must stay sys_platform-gated"
        assert "darwin" in str(req.marker), f"cuda-libs → {pkg} marker {req.marker!r} dropped the macOS gate"


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


def test_build_pip_argv_installs_the_bundled_wheel_when_given_one(tmp_path):
    """The Bundle topology: install the wheel the installer shipped, not an
    editable checkout that doesn't exist in `%LOCALAPPDATA%` (ADR-0015)."""
    wheel = tmp_path / "tapscribe-1.1.0-py3-none-any.whl"
    wheel.write_bytes(b"")
    argv = install_picker.build_pip_argv(
        ["whisper-live", "whisper-cpu"], python="py", install_spec=str(wheel)
    )
    assert "-e" not in argv
    assert argv[-1] == f"{wheel.resolve()}[whisper-live,whisper-cpu]"


def test_main_forwards_install_spec_to_the_install(tmp_path, monkeypatch, tmp_stamp):
    """End of the thread: `--install-spec` on the picker's own CLI reaches the
    pip argv. Without this the flag would parse and be silently ignored, and a
    Bundle would quietly try (and fail) to install an editable checkout."""
    wheel = tmp_path / "tapscribe-1.1.0-py3-none-any.whl"
    wheel.write_bytes(b"")
    state = tmp_path / ".tapscribe-install.json"
    state.write_text(
        json.dumps(
            {
                "version": install_picker.STATE_VERSION,
                "choices": {"whisper": {"enabled": True, "backend": BACKEND_CPU}},
            }
        )
    )
    monkeypatch.setattr(install_picker, "STATE_FILE", state)
    monkeypatch.setattr(install_picker, "detect_caps", lambda **_: _caps())
    seen: list[list[str]] = []
    monkeypatch.setattr(install_picker.subprocess, "call", lambda argv, **kw: seen.append(argv) or 0)

    assert install_picker.main(["--non-interactive", "--install-spec", str(wheel)]) == 0
    assert seen, "pip was never invoked"
    assert "-e" not in seen[0]
    assert str(wheel.resolve()) in seen[0][-1]


def test_main_reads_and_writes_the_state_file_given_on_the_cli(tmp_path, monkeypatch, tmp_stamp):
    """`--state-file` relocates the saved selection. A Bundle keeps it under
    TAPSCRIBE_BASE_DIR so it survives an upgrade; the module-level default
    (repo root) is what a dev checkout keeps using."""
    state = tmp_path / "elsewhere" / ".tapscribe-install.json"
    state.parent.mkdir()
    state.write_text(
        json.dumps(
            {
                "version": install_picker.STATE_VERSION,
                "choices": {"whisper": {"enabled": True, "backend": BACKEND_MLX}},
            }
        )
    )
    monkeypatch.setattr(install_picker, "detect_caps", lambda **_: _apple_caps())
    monkeypatch.setattr(install_picker, "run_install", lambda *a, **k: 0)

    assert install_picker.main(["--non-interactive", "--state-file", str(state)]) == 0
    # Read from the given path (mlx would be absent had it loaded defaults)…
    reloaded = install_picker.Selection.load(state, _apple_caps())
    assert reloaded.choices["whisper"].backend == BACKEND_MLX
    # …and written back there, not to the module-level default.
    assert not install_picker.STATE_FILE.exists() or install_picker.STATE_FILE != state


def test_main_records_removed_backends_to_a_sidecar(tmp_path, monkeypatch, tmp_stamp):
    """`removed_backend_families()` has always warned to STDERR — invisible in a
    Bundle, where the Launcher pipes output to a log file nobody opens. The
    operator upgrades, their models quietly stop installing, and nothing they'd
    look at says so. Record it where the dashboard can read it."""
    state = tmp_path / ".tapscribe-install.json"
    state.write_text(
        json.dumps(
            {
                "version": install_picker.STATE_VERSION,
                # 'mlx' is real today; monkeypatching the family's declared
                # backends below is what makes it "removed by a later version".
                "choices": {"parakeet": {"enabled": True, "backend": BACKEND_MLX}},
            }
        )
    )
    shrunk = tuple(
        install_picker.FamilyDef(
            key=f.key,
            label=f.label,
            description=f.description,
            size_hint=f.size_hint,
            shared_extras=f.shared_extras,
            default_selected=f.default_selected,
            backends=tuple(b for b in f.backends if b.key != BACKEND_MLX),
        )
        for f in FAMILIES
    )
    monkeypatch.setattr(install_picker, "FAMILIES", shrunk)
    monkeypatch.setattr(install_picker, "detect_caps", lambda **_: _apple_caps())
    monkeypatch.setattr(install_picker, "run_install", lambda *a, **k: 0)

    assert install_picker.main(["--non-interactive", "--state-file", str(state)]) == 0

    sidecar = state.parent / install_picker.WARNINGS_FILENAME
    assert sidecar.exists(), "expected a warnings sidecar next to the state file"
    stale = json.loads(sidecar.read_text())["stale_backends"]
    assert {entry["family"] for entry in stale} == {"parakeet"}
    assert stale[0]["backend"] == BACKEND_MLX


def test_main_clears_a_stale_sidecar_once_the_selection_is_valid(tmp_path, monkeypatch, tmp_stamp):
    """The banner must disappear when the operator re-picks — a warning file
    that outlives its cause is worse than no warning."""
    state = tmp_path / ".tapscribe-install.json"
    state.write_text(
        json.dumps(
            {
                "version": install_picker.STATE_VERSION,
                "choices": {"whisper": {"enabled": True, "backend": BACKEND_CPU}},
            }
        )
    )
    sidecar = state.parent / install_picker.WARNINGS_FILENAME
    sidecar.write_text(json.dumps({"stale_backends": [{"family": "parakeet", "backend": "mlx"}]}))
    monkeypatch.setattr(install_picker, "detect_caps", lambda **_: _caps())
    monkeypatch.setattr(install_picker, "run_install", lambda *a, **k: 0)

    assert install_picker.main(["--non-interactive", "--state-file", str(state)]) == 0
    assert not sidecar.exists()


def test_main_rejects_a_bogus_install_spec(tmp_path, capsys):
    """Validated at the boundary, not handed to pip — CLAUDE.md's CodeQL rule
    for argparse values that flow onward."""
    assert install_picker.main(["--non-interactive", "--install-spec", "requests"]) == 2
    assert "install spec" in capsys.readouterr().err


# ── Skip-install stamp ──────────────────────────────────────────────


@pytest.fixture
def tmp_stamp(monkeypatch, tmp_path):
    """Point STAMP_FILE at a tmpdir so tests don't read/write the real
    venv's install stamp."""
    stamp = tmp_path / ".tapscribe-install-stamp.json"
    monkeypatch.setattr(install_picker, "STAMP_FILE", stamp)
    return stamp


def test_install_stamp_round_trips(tmp_stamp):
    install_picker.write_install_stamp(tmp_stamp, ["whisper-live", "whisper-cpu"], "abc123")
    stamp = install_picker.read_install_stamp(tmp_stamp)
    assert stamp == {"extras": ["whisper-live", "whisper-cpu"], "pyproject": "abc123"}


def test_read_install_stamp_missing_file_returns_none(tmp_stamp):
    assert install_picker.read_install_stamp(tmp_stamp) is None


def test_read_install_stamp_malformed_returns_none(tmp_stamp):
    tmp_stamp.write_text("not json")
    assert install_picker.read_install_stamp(tmp_stamp) is None


def test_install_is_current_true_when_extras_and_fingerprint_match():
    stamp = {"extras": ["whisper-live", "whisper-cpu"], "pyproject": "fp"}
    assert install_picker.install_is_current(stamp, ["whisper-live", "whisper-cpu"], "fp") is True


def test_install_is_current_false_when_extras_differ():
    stamp = {"extras": ["whisper-live", "whisper-cpu"], "pyproject": "fp"}
    assert install_picker.install_is_current(stamp, ["whisper-live", "whisper-mlx"], "fp") is False


def test_install_is_current_false_when_fingerprint_differs():
    """A pyproject bump (e.g. after git pull) must re-trigger pip even
    when the operator's selection is identical."""
    stamp = {"extras": ["whisper-live", "whisper-cpu"], "pyproject": "old"}
    assert install_picker.install_is_current(stamp, ["whisper-live", "whisper-cpu"], "new") is False


def test_install_is_current_false_when_no_stamp():
    assert install_picker.install_is_current(None, ["whisper-live"], "fp") is False


# ── pyproject_fingerprint / package_is_installed / STAMP_FILE (real behaviour) ──


@pytest.fixture
def tmp_repo_root(monkeypatch, tmp_path):
    """Point REPO_ROOT at a tmpdir with a writable pyproject.toml so tests
    can exercise the REAL fingerprint against a file they control (and edit
    mid-test), instead of monkeypatching pyproject_fingerprint to a constant."""
    root = tmp_path / "repo"
    root.mkdir()
    pyproject = root / "pyproject.toml"
    pyproject.write_text("[project]\nname = 'tapscribe'\nversion = '0.1.0'\ndependencies = []\n")
    monkeypatch.setattr(install_picker, "REPO_ROOT", root)
    return pyproject


def test_pyproject_fingerprint_is_sha256_of_file_and_stable(tmp_repo_root):
    import hashlib

    expected = hashlib.sha256(tmp_repo_root.read_bytes()).hexdigest()
    assert install_picker.pyproject_fingerprint() == expected
    # Stable: identical content hashes identically across calls.
    assert install_picker.pyproject_fingerprint() == expected


def test_pyproject_fingerprint_changes_when_file_content_changes(tmp_repo_root):
    """The core invalidation signal: a real edit to pyproject.toml (a
    dependency bump) must yield a different fingerprint."""
    before = install_picker.pyproject_fingerprint()
    tmp_repo_root.write_text(
        "[project]\nname = 'tapscribe'\nversion = '0.1.0'\ndependencies = ['torch>=2.2']\n"
    )
    after = install_picker.pyproject_fingerprint()
    assert before != after


def test_pyproject_fingerprint_falls_back_to_the_installed_version(monkeypatch, tmp_path):
    """No pyproject.toml is the NORMAL state of a Bundle (it installs a wheel,
    not a checkout), so "" would be recorded in the stamp AND computed on every
    later run — they'd match, and pip would be skipped forever.

    That is exactly the upgrade bug: after installing a new Bundle, the
    operator's model extras would never be re-resolved against the new wheel's
    dependency pins. Fall back to the installed version so an upgrade
    invalidates the stamp the way a pyproject edit does in a checkout.
    """
    empty = tmp_path / "no-repo"
    empty.mkdir()
    monkeypatch.setattr(install_picker, "REPO_ROOT", empty)

    monkeypatch.setattr(install_picker, "_installed_version", lambda: "1.1.0")
    before = install_picker.pyproject_fingerprint()
    assert before  # not the empty sentinel — it must be able to CHANGE

    monkeypatch.setattr(install_picker, "_installed_version", lambda: "1.2.0")
    assert install_picker.pyproject_fingerprint() != before


def test_fingerprint_is_empty_only_when_nothing_identifies_the_install(monkeypatch, tmp_path):
    """Neither a pyproject nor a resolvable version → "" so we never skip pip on
    the strength of an install we can't identify at all."""
    empty = tmp_path / "no-repo"
    empty.mkdir()
    monkeypatch.setattr(install_picker, "REPO_ROOT", empty)
    monkeypatch.setattr(install_picker, "_installed_version", lambda: None)
    assert install_picker.pyproject_fingerprint() == ""


def test_bundle_upgrade_invalidates_a_current_looking_stamp(monkeypatch, tmp_path):
    """The whole point, at the seam that decides: same extras, no pyproject, but
    a newer wheel ⇒ NOT current, so the picker re-runs pip."""
    empty = tmp_path / "no-repo"
    empty.mkdir()
    monkeypatch.setattr(install_picker, "REPO_ROOT", empty)

    monkeypatch.setattr(install_picker, "_installed_version", lambda: "1.1.0")
    stamp = {"extras": ["whisper-live"], "pyproject": install_picker.pyproject_fingerprint()}
    assert install_picker.install_is_current(stamp, ["whisper-live"], install_picker.pyproject_fingerprint())

    monkeypatch.setattr(install_picker, "_installed_version", lambda: "1.2.0")
    assert not install_picker.install_is_current(
        stamp, ["whisper-live"], install_picker.pyproject_fingerprint()
    )


def test_package_is_installed_true_for_a_real_distribution():
    # Exercises the real importlib.metadata wiring against a distribution that
    # is always present wherever the test suite runs.
    assert install_picker.package_is_installed("pytest") is True


def test_package_is_installed_false_for_absent_module():
    assert install_picker.package_is_installed("tapscribe_not_a_real_pkg_zzz") is False


def test_stamp_file_lives_inside_the_venv_prefix():
    """The skip-stamp must live under sys.prefix so that start.sh's
    `rm -rf .venv` (venv recreation) drops it and forces a fresh install.
    Pin the location so a refactor can't silently move it to a
    venv-surviving path like REPO_ROOT and re-introduce the stale-stamp bug."""
    assert install_picker.STAMP_FILE == Path(sys.prefix) / ".tapscribe-install-stamp.json"


def _patch_run_install_counter(monkeypatch) -> list[list[str]]:
    """Replace run_install with a no-op that records each call's extras."""
    calls: list[list[str]] = []

    def fake_run_install(extras, *, dry_run=False, install_spec=None):
        calls.append(list(extras))
        return 0

    monkeypatch.setattr(install_picker, "run_install", fake_run_install)
    return calls


# Named stubs for monkeypatching install_picker's module-level functions in
# the tests below. Defined as proper functions (not lambdas) so CodeQL's
# "Unnecessary lambda" rule stays clean — every stub here has to match the
# real signature even though the body ignores the arguments.


def _detect_caps_cpu(*, force_no_mlx=False):
    """Stand-in for `install_picker.detect_caps` that returns a deterministic
    Linux/x86_64 CPU-only profile regardless of the host or the --no-mlx flag.
    Lets the main() tests run identically across CI runners."""
    return _caps()


def _package_present():
    return True


def _package_missing():
    return False


def _pip_install_fails(extras, *, dry_run=False, install_spec=None):
    """Stand-in for `install_picker.run_install` that pretends pip exited
    non-zero, so tests can verify failure paths without invoking real pip."""
    return 1


def test_main_skips_pip_on_unchanged_rerun(tmp_state, tmp_stamp, monkeypatch, capsys):
    """The behaviour the operator asked for: an unchanged re-run installs
    once, stamps it, and skips pip the second time — telling the operator
    why instead of silently doing nothing."""
    monkeypatch.setattr(install_picker, "detect_caps", _detect_caps_cpu)
    monkeypatch.setattr(install_picker, "package_is_installed", _package_present)
    calls = _patch_run_install_counter(monkeypatch)

    assert install_picker.main(["--non-interactive"]) == 0
    assert len(calls) == 1  # first run installs
    assert tmp_stamp.exists()
    capsys.readouterr()  # drop first-run output

    assert install_picker.main(["--non-interactive"]) == 0
    assert len(calls) == 1  # second, unchanged run skips pip
    assert "skipping pip" in capsys.readouterr().out


def test_main_reinstalls_when_package_missing_despite_current_stamp(tmp_state, tmp_stamp, monkeypatch):
    """A current stamp must NOT short-circuit pip when the package itself is
    gone (manual `pip uninstall`, out-of-band venv recreation) — otherwise
    the picker would leave a broken install with no pip run."""
    monkeypatch.setattr(install_picker, "detect_caps", _detect_caps_cpu)
    calls = _patch_run_install_counter(monkeypatch)

    monkeypatch.setattr(install_picker, "package_is_installed", _package_present)
    assert install_picker.main(["--non-interactive"]) == 0
    assert len(calls) == 1  # installed + stamped

    monkeypatch.setattr(install_picker, "package_is_installed", _package_missing)
    assert install_picker.main(["--non-interactive"]) == 0
    assert len(calls) == 2  # stamp current, but package gone → reinstall


def test_main_reinstalls_when_selection_changes(tmp_state, tmp_stamp, monkeypatch):
    """Flipping a family back on between runs must re-run pip — the stamp
    only suppresses genuinely-unchanged re-runs."""
    monkeypatch.setattr(install_picker, "detect_caps", _detect_caps_cpu)
    calls = _patch_run_install_counter(monkeypatch)

    assert install_picker.main(["--non-interactive"]) == 0
    assert len(calls) == 1

    # Operator edits their saved selection (here: enable Voxtral too).
    sel = Selection.load(tmp_state, _caps())
    sel.choices["voxtral"] = FamilyChoice(enabled=True, backend=BACKEND_CPU)
    sel.save(tmp_state)

    assert install_picker.main(["--non-interactive"]) == 0
    assert len(calls) == 2  # changed selection forces a fresh install


def test_main_reinstalls_when_pyproject_actually_changes(tmp_state, tmp_stamp, tmp_repo_root, monkeypatch):
    """End-to-end for the dependency-bump path, with NO mocking of the
    fingerprint: a real edit to pyproject.toml re-runs pip, and the fresh
    stamp re-stabilises so the next unchanged run skips again. Exercises
    pyproject_fingerprint + read/write_install_stamp against real files."""
    monkeypatch.setattr(install_picker, "detect_caps", _detect_caps_cpu)
    monkeypatch.setattr(install_picker, "package_is_installed", _package_present)
    calls = _patch_run_install_counter(monkeypatch)

    assert install_picker.main(["--non-interactive"]) == 0
    assert len(calls) == 1  # first install
    # Same selection, untouched pyproject → skip.
    assert install_picker.main(["--non-interactive"]) == 0
    assert len(calls) == 1

    # A real dependency bump lands in pyproject.toml between runs.
    tmp_repo_root.write_text(
        "[project]\nname = 'tapscribe'\nversion = '0.1.0'\ndependencies = ['torch>=2.2']\n"
    )
    assert install_picker.main(["--non-interactive"]) == 0
    assert len(calls) == 2  # changed fingerprint forces a reinstall

    # The reinstall re-stamped with the new fingerprint, so a fourth
    # unchanged run skips again — proving the stamp was actually refreshed.
    assert install_picker.main(["--non-interactive"]) == 0
    assert len(calls) == 2


def test_main_stamp_records_resolved_extras_and_real_fingerprint(tmp_state, tmp_stamp, monkeypatch):
    """The stamp the install writes must be exactly what install_is_current
    later compares: the real resolved extras and the real pyproject
    fingerprint. A drift between what's written and what's read would make
    every run either always-skip (stale install) or never-skip (the churn
    we set out to remove)."""
    monkeypatch.setattr(install_picker, "detect_caps", _detect_caps_cpu)
    monkeypatch.setattr(install_picker, "package_is_installed", _package_present)
    _patch_run_install_counter(monkeypatch)

    assert install_picker.main(["--non-interactive"]) == 0

    written = json.loads(tmp_stamp.read_text())
    expected_extras = install_picker.resolve_extras(Selection.load(tmp_state, _caps()), _caps())
    # Default selection on a plain CPU box is Whisper/CPU.
    assert expected_extras == ["whisper-live", "whisper-cpu"]
    assert written["extras"] == expected_extras
    assert written["pyproject"] == install_picker.pyproject_fingerprint()
    # And the round-trip predicate agrees the install is current.
    assert install_picker.install_is_current(written, expected_extras, install_picker.pyproject_fingerprint())


def test_main_does_not_stamp_on_pip_failure(tmp_state, tmp_stamp, monkeypatch):
    """A failed install must not write the stamp — otherwise the next run
    would wrongly skip pip on a broken install."""
    monkeypatch.setattr(install_picker, "detect_caps", _detect_caps_cpu)
    monkeypatch.setattr(install_picker, "run_install", _pip_install_fails)

    assert install_picker.main(["--non-interactive"]) == 1
    assert not tmp_stamp.exists()


def test_main_dry_run_does_not_write_stamp(tmp_state, tmp_stamp, monkeypatch):
    """--dry-run is read-only: it must not persist a stamp that would let
    a later real run skip the install."""
    monkeypatch.setattr(install_picker, "detect_caps", _detect_caps_cpu)
    assert install_picker.main(["--non-interactive", "--dry-run"]) == 0
    assert not tmp_stamp.exists()


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


def test_render_surfaces_cuda_libs_line_on_cuda_box():
    """When the resolved set includes cuda-libs (CUDA + Whisper CPU), the
    operator sees an explicit note that the faster-whisper GPU runtime
    libs are coming along."""
    sel = Selection()
    _enable(sel, "whisper", BACKEND_CPU)
    text = install_picker.render(sel, _caps(cuda=True))
    assert "cuda-libs" in text  # in the pip command itself
    assert "+ CUDA runtime libs" in text
    assert "nvidia-cublas-cu12" in text
    assert "faster-whisper GPU" in text


def test_render_omits_cuda_libs_line_without_cuda():
    """No CUDA → no extra line, even with the Whisper CPU backend on."""
    sel = Selection()
    _enable(sel, "whisper", BACKEND_CPU)
    text = install_picker.render(sel, _caps(cuda=False))
    assert "+ CUDA runtime libs" not in text


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
    req = _requirement_for(atomic_extras("whisper-mlx"), "mlx-whisper")
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
        ("moonshine-mlx", "mlx-audio"),
    ],
)
def test_pyproject_mlx_extras_stay_platform_gated(extra_name, pkg):
    """The MLX-only atomic extras must keep their Darwin/arm64 env marker
    so pip on Linux/Windows/Intel-Mac skips them instead of erroring out
    on wheels that don't exist for those platforms."""
    req = _requirement_for(atomic_extras(extra_name), pkg)
    assert req.marker is not None, f"{extra_name} → {pkg} must stay sys_platform-gated"
    marker = str(req.marker)
    assert "darwin" in marker and "arm64" in marker, (
        f"{extra_name} → {pkg} marker {marker!r} dropped Darwin+arm64 gating"
    )


def test_pyproject_cpu_extras_do_not_pull_mlx_packages():
    """The whole point of splitting `whisper` into atoms: a Linux CI box
    that installs `.[whisper-cpu]` must NOT see mlx-whisper in the
    resolved set."""
    cpu = atomic_extras("whisper-cpu")
    assert not any("mlx" in line for line in cpu), cpu


def test_pyproject_parakeet_alias_is_mlx_only_on_apple_silicon():
    """On Apple Silicon, `tapscribe[parakeet]` should resolve to
    parakeet-mlx alone (GPU via Metal, faster than torch); everywhere else
    to the transformers `parakeet-cpu` backend. The alias gates the CPU
    atom out on darwin+arm64 via a PEP 508 marker so a mac install doesn't
    pull transformers when MLX is the path, and the two backends never
    coexist in one resolve."""
    parakeet_lines = atomic_extras("parakeet")
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
        "would drag transformers onto a mac where MLX is the path. The atom "
        f"must be MLX-only on darwin+arm64. Got: {darwin_line!r}"
    )


def test_pyproject_moonshine_alias_is_mlx_only_on_apple_silicon():
    """Same split as Parakeet: `tapscribe[moonshine]` resolves to
    moonshine-mlx alone on Apple Silicon, moonshine-cpu everywhere else."""
    moonshine_lines = atomic_extras("moonshine")
    assert len(moonshine_lines) == 2, (
        f"moonshine alias must declare two marker-gated entries; got: {moonshine_lines}"
    )
    darwin_line = next((line for line in moonshine_lines if "moonshine-mlx" in line), None)
    other_line = next((line for line in moonshine_lines if "moonshine-cpu" in line), None)
    assert darwin_line is not None, f"missing moonshine-mlx entry; got: {moonshine_lines}"
    assert other_line is not None, f"missing moonshine-cpu entry; got: {moonshine_lines}"
    assert "moonshine-cpu" not in darwin_line


def test_picker_moonshine_family_registered_with_cpu_and_mlx_backends():
    moonshine = next(f for f in FAMILIES if f.key == "moonshine")
    assert {b.key for b in moonshine.backends} == {BACKEND_CPU, BACKEND_MLX}
    assert moonshine.default_selected is False  # opt-in, not part of the recommended baseline


def test_picker_moonshine_disabled_by_default_does_not_appear_in_extras():
    sel = Selection.defaults_for(_apple_caps())
    extras = install_picker.resolve_extras(sel, _apple_caps())
    assert not any("moonshine" in e for e in extras)


def test_picker_moonshine_resolves_to_mlx_extra_on_apple_silicon():
    sel = Selection()
    _enable(sel, "moonshine", BACKEND_MLX)
    extras = install_picker.resolve_extras(sel, _apple_caps())
    assert extras == ["moonshine-mlx"]


def test_picker_moonshine_resolves_to_cpu_extra_on_linux():
    sel = Selection()
    _enable(sel, "moonshine", BACKEND_CPU)
    extras = install_picker.resolve_extras(
        sel, install_picker.MachineCaps(os_name="Linux", arch="x86_64", mlx=False, cuda=False)
    )
    assert extras == ["moonshine-cpu"]


def test_picker_apple_silicon_mlx_only_matches_failing_invocation_atoms():
    """End-to-end: an Apple-Silicon MLX-only selection across Whisper +
    Parakeet must resolve without dragging in the `whisper-cpu` atom."""
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


# ── review findings: dry-run purity, state-file topology, install probe ──────


def test_dry_run_does_not_touch_the_warnings_sidecar(tmp_path, monkeypatch, tmp_stamp):
    """`--dry-run` is documented as "purely read-only: don't persist the selection
    or stamp". The sidecar write/clear escaped that gate, so previewing the pip
    command DELETED the stale-selection banner an operator still needed — and a
    dry run started needing write permission on the state dir."""
    state = tmp_path / ".tapscribe-install.json"
    state.write_text(
        json.dumps(
            {
                "version": install_picker.STATE_VERSION,
                "choices": {"whisper": {"enabled": True, "backend": BACKEND_CPU}},
            }
        )
    )
    sidecar = tmp_path / install_picker.WARNINGS_FILENAME
    sidecar.write_text(json.dumps({"stale_backends": [{"family": "parakeet", "backend": "mlx"}]}))
    monkeypatch.setattr(install_picker, "detect_caps", lambda **_: _caps())

    assert install_picker.main(["--non-interactive", "--dry-run", "--state-file", str(state)]) == 0
    assert sidecar.exists(), "dry-run must not clear the sidecar"


def test_state_file_default_follows_the_data_dir(tmp_path):
    """start.sh/start.ps1 invoke the picker with NO --state-file, so its default
    must land where the app's `setup_install._STATE_FILE` does. Before this they
    diverged the moment TAPSCRIBE_BASE_DIR was set — a documented topology
    (config.py names Docker/systemd, and systemd runs start.sh) — so /setup wrote
    the selection to the data dir while the next start.sh silently re-applied a
    stale one from the repo root.

    Driven in a SUBPROCESS: both values are module-level constants resolved at
    import, and reloading the modules in-process would hand other tests a fresh
    `InstallSelectionError` class their `pytest.raises` no longer matches.
    """
    import os
    import subprocess

    probe = (
        "from tapscribe import install_picker, setup_install, config;"
        "print(install_picker.STATE_FILE);"
        "print(setup_install._STATE_FILE);"
        "print(config.BASE_DIR)"
    )
    out = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        check=True,
        cwd=Path(__file__).resolve().parent.parent,
        env={**os.environ, "TAPSCRIBE_BASE_DIR": str(tmp_path)},
    )
    picker_state, app_state, base_dir = out.stdout.split("\n")[:3]
    assert picker_state == app_state, "picker default and /setup must write the same file"
    assert Path(picker_state).parent == Path(base_dir)


def test_package_is_installed_asks_for_a_distribution_not_an_import(monkeypatch):
    """The guard exists to catch "the stamp outlived its package" (a manual
    `pip uninstall`, a recreated venv). `find_spec` stopped answering that once
    the picker moved INTO the package and callers switched to `-m` from the repo
    root: cwd lands on sys.path, so the module is importable whether or not it
    was ever installed, and the guard could never return False again."""
    assert install_picker.package_is_installed("tapscribe_not_a_real_pkg_zzz") is False
    # `json` is importable but is NOT an installed distribution — the old
    # find_spec probe answered True here, which is exactly the wrong answer.
    assert install_picker.package_is_installed("json") is False
