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

import pytest
from conftest import fake_install_spawn  # type: ignore[import-not-found]

# The picker lives in the package since ADR-0015 (the wheel doesn't ship
# `tools/`). Only the TEST imports it; the app still only ever spawns it.
from tapscribe import install_picker
from tapscribe.setup_install import (
    InstallSelectionError,
    picker_install_argv,
    read_live_choice,
    run_install,
    sse,
    to_picker_state,
    validate_live,
    validate_selection,
    write_picker_state,
)

_MAC = install_picker.MachineCaps(os_name="Darwin", arch="arm64", mlx=True, cuda=False)
_CUDA = install_picker.MachineCaps(os_name="Linux", arch="x86_64", mlx=False, cuda=True)
_CPU = install_picker.MachineCaps(os_name="Linux", arch="x86_64", mlx=False, cuda=False)


def _extras(selection: dict[str, str], caps, *, live: bool = True) -> set[str]:
    """Resolve a catalog-family selection to pip extras THROUGH the real picker."""
    state = to_picker_state(selection, live=live)
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


# ── live-channel opt-out (#374) — drift-tested against the real picker ──────


def test_live_true_resolves_with_whisper_live_extra():
    # Explicit True behaves exactly like the (unspecified) default.
    assert _extras({"whisper": "cpu"}, _CPU, live=True) == {"whisper-live", "whisper-cpu"}


def test_live_false_resolves_without_whisper_live_extra():
    assert _extras({"whisper": "cpu"}, _CPU, live=False) == {"whisper-cpu"}


def test_live_default_is_true_when_unspecified():
    """DEFAULT MUST NOT CHANGE: a caller that doesn't pass `live` at all
    (an existing client, or the picker_install_argv path) must keep
    getting the live channel — the whole point of an OPT-OUT."""
    assert _extras({"whisper": "cpu"}, _CPU) == {"whisper-live", "whisper-cpu"}


def test_live_false_still_resolves_nb_whisper_extras_it_just_drops_live():
    # nb-whisper rides the same picker `whisper` family/backend — live=False
    # drops the shared live extra but leaves nb-whisper's own atom alone.
    assert _extras({"nb-whisper": "cpu"}, _CPU, live=False) == {"whisper-cpu"}


def test_to_picker_state_carries_live_onto_the_whisper_choice():
    assert to_picker_state({"whisper": "cpu"}, live=False)["choices"]["whisper"]["live"] is False
    assert to_picker_state({"whisper": "cpu"}, live=True)["choices"]["whisper"]["live"] is True


def test_to_picker_state_defaults_live_true():
    assert to_picker_state({"whisper": "cpu"})["choices"]["whisper"]["live"] is True


def test_to_picker_state_sets_live_even_when_whisper_is_not_selected():
    """The toggle is a per-REQUEST flag (there's only one live row in
    /setup), not conditioned on whisper/nb-whisper being part of THIS
    install — so it stays sticky across e.g. a Parakeet-only install."""
    state = to_picker_state({"parakeet": "cpu"}, live=False)
    assert state["choices"]["whisper"]["enabled"] is False
    assert state["choices"]["whisper"]["live"] is False


def test_read_live_choice_roundtrips_to_picker_state(tmp_path):
    """`read_live_choice` is the decode symmetric with `to_picker_state`'s
    write, and owns the picker-state shape for the app side. A missing/absent
    `live` reads as ON (matching `install_picker.FamilyChoice.live`)."""
    path = tmp_path / ".tapscribe-install.json"
    assert read_live_choice(path) is True  # no file → live-on default
    write_picker_state(to_picker_state({"whisper": "cpu"}, live=False), path=path)
    assert read_live_choice(path) is False
    write_picker_state(to_picker_state({"whisper": "cpu"}, live=True), path=path)
    assert read_live_choice(path) is True


# ── validate_live ────────────────────────────────────────────────────────


def test_validate_live_defaults_true_when_absent():
    assert validate_live(None) is True


def test_validate_live_accepts_explicit_booleans():
    assert validate_live(True) is True
    assert validate_live(False) is False


def test_validate_live_rejects_non_bool():
    with pytest.raises(InstallSelectionError):
        validate_live("false")
    with pytest.raises(InstallSelectionError):
        validate_live(0)


def test_picker_install_argv_runs_the_picker_as_a_module():
    """`-m tapscribe.install_picker`, not a script path. A Bundle installs a
    wheel into a venv — there is no repo-relative `tools/install_picker.py` to
    point at, and `-m` resolves wherever the package actually landed
    (ADR-0015)."""
    argv = picker_install_argv(python="/venv/bin/python")
    assert argv[:3] == ["/venv/bin/python", "-m", "tapscribe.install_picker"]
    assert "--non-interactive" in argv
    assert "--no-mlx" not in argv
    assert "--no-mlx" in picker_install_argv(python="py", no_mlx=True)


def test_picker_install_argv_forwards_the_install_spec(tmp_path):
    """The Bundle's wheel path reaches the picker as an argument, so the
    subprocess installs from the SAME wheel the installer shipped rather than
    falling back to an editable checkout that isn't there."""
    wheel = tmp_path / "tapscribe-1.1.0-py3-none-any.whl"
    wheel.write_bytes(b"")
    argv = picker_install_argv(python="py", install_spec=str(wheel))
    assert "--install-spec" in argv
    assert argv[argv.index("--install-spec") + 1] == str(wheel)


def test_picker_install_argv_omits_install_spec_by_default():
    """Absent flag = checkout topology. Devs launch on Windows without the
    installer, so the default argv must stay exactly as it was."""
    assert "--install-spec" not in picker_install_argv(python="py")


def test_picker_install_argv_pins_the_state_file_to_the_data_dir():
    """The saved model selection must survive a Bundle upgrade, so it lives
    under TAPSCRIBE_BASE_DIR — not beside the package, which in a wheel install
    is `site-packages`. In a checkout BASE_DIR *is* the repo root, so this is
    the same path devs have always had."""
    from tapscribe import config

    argv = picker_install_argv(python="py")
    assert "--state-file" in argv
    assert argv[argv.index("--state-file") + 1] == str(config.BASE_DIR / ".tapscribe-install.json")


def test_write_picker_state_roundtrips_through_picker_load(tmp_path):
    state = to_picker_state({"whisper": "mlx", "parakeet": "cpu"})
    path = tmp_path / ".tapscribe-install.json"
    write_picker_state(state, path=path)
    sel = install_picker.Selection.load(path, _MAC)
    assert sel.choices["whisper"].enabled is True
    assert sel.choices["whisper"].backend == "mlx"
    assert sel.choices["parakeet"].enabled is True
    assert sel.choices["voxtral"].enabled is False


def test_write_picker_state_preserves_families_setup_does_not_manage(tmp_path):
    """A /setup install must not silently clear a family /setup has no row for.

    `_PICKER_FAMILIES` is a deliberate SUBSET of `install_picker.FAMILIES`
    (moonshine is live-only, so /setup shows no row for it). A wholesale
    rewrite of the state file dropped the `moonshine` key, the picker read the
    absence back as `enabled=False`, and its next `Selection.save` re-persisted
    that — the operator's terminal-picker choice was gone for good. It kept
    WORKING until the venv was rebuilt (pip doesn't uninstall), so nothing
    surfaced the loss.
    """
    path = tmp_path / ".tapscribe-install.json"

    # The operator's terminal picker run: everything on, MLX where offered.
    prior = install_picker.Selection.defaults_for(_MAC)
    for fam in install_picker.FAMILIES:
        prior.choices[fam.key].enabled = True
    prior.save(path)

    # Now they use /setup, which only knows about Whisper.
    write_picker_state(to_picker_state({"whisper": "mlx"}), path=path)

    reloaded = install_picker.Selection.load(path, _MAC)
    # Every family the picker declares still has a key on disk...
    on_disk = json.loads(path.read_text(encoding="utf-8"))["choices"]
    assert set(on_disk) == {fam.key for fam in install_picker.FAMILIES}
    # ...and the one /setup doesn't manage kept BOTH its flag and its backend.
    assert reloaded.choices["moonshine"].enabled is True
    assert reloaded.choices["moonshine"].backend == prior.choices["moonshine"].backend
    # The families /setup DOES manage are still overwritten by the new pick.
    assert reloaded.choices["whisper"].backend == "mlx"
    assert reloaded.choices["parakeet"].enabled is False


def test_write_picker_state_ignores_a_corrupt_prior_file(tmp_path):
    """Merging must not make a hand-mangled state file fail the install —
    /setup falls back to writing its own selection alone."""
    path = tmp_path / ".tapscribe-install.json"
    path.write_text("{not json", encoding="utf-8")

    write_picker_state(to_picker_state({"whisper": "cpu"}), path=path)

    sel = install_picker.Selection.load(path, _CPU)
    assert sel.choices["whisper"].enabled is True


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


# ── streaming runner (fake subprocess from conftest.fake_install_spawn) ───────


async def _collect(selection, *, lines, returncode, live=True, on_success=None):
    written = {}
    events = []
    async for ev in run_install(
        selection,
        live=live,
        spawn=fake_install_spawn(lines, returncode),
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
    assert written["choices"]["whisper"]["live"] is True  # default carried through


async def test_run_install_forwards_live_false_to_the_written_state():
    _events, written = await _collect({"whisper": "cpu"}, lines=[b"installed\n"], returncode=0, live=False)
    assert written["choices"]["whisper"]["live"] is False


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
    assert "python missing" not in events[-1]["message"]  # exception text must NOT leak to the client
    assert calls == []  # reload must NOT fire when the install never ran


async def test_run_install_finishes_with_done_even_if_hot_reload_raises():
    # A successful install must still terminate with `done` even if the
    # best-effort hot-reload (on_success) raises — the install itself worked.
    def boom_reload():
        raise RuntimeError("probe refresh boom")

    events, _ = await _collect(
        {"whisper": "cpu"}, lines=[b"installed\n"], returncode=0, on_success=boom_reload
    )
    assert [e["phase"] for e in events][-1] == "done"
    assert any(e["phase"] == "log" and "refresh failed" in e.get("line", "") for e in events)


# ── hot-reload primitive ─────────────────────────────────────────────────────


def test_refresh_backend_probes_reenables_detection():
    from tapscribe.runtime_probe import (
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
