"""RED contract for `tapscribe.preflight` — the shared bring-up steps.

`start.ps1` grew a pile of probe-then-repair logic that has to happen between
"a venv exists" and "the recorder boots": repair `onnxruntime` (the core VAD
backend) if the venv is incomplete, pull the `[summarize]` extra, and swap CPU
torch for a CUDA build on an NVIDIA Windows box. A Bundle has no `start.ps1`, so those steps
were homeless — and a Bundle that skips the CUDA swap silently gives every
Windows NVIDIA operator `Available backends: ['cpu']` with no clue why
(`start.ps1`'s own comment says exactly this).

Rather than reimplement them in C#, they move here and BOTH callers — the
PowerShell script and the Launcher — run the same module. A fifth check added
later then can't drift between a PowerShell copy and a C# copy.

The seam is `plan_steps()`, which returns the work as DATA. Every probe is
injected, so these tests assert the full decision matrix without a GPU, without
torch installed, and without running pip.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tapscribe import preflight
from tapscribe.preflight import Step, plan_steps

_WHEEL_MISSING: frozenset[str] = frozenset()


def _present(*names: str):
    """A `module_present` probe reporting exactly `names` as importable."""
    return lambda name: name in names


_ALL = _present("onnxruntime", "llama_cpp", "mlx_lm")


def _names(steps: list[Step]) -> list[str]:
    return [s.name for s in steps]


# --- the quiet path --------------------------------------------------------


def test_nothing_to_do_when_every_probe_passes():
    """The common case — a warm venv on a re-launch. Preflight must be silent
    and cheap, not re-run pip on every boot."""
    assert plan_steps(python="py", system="Linux", module_present=_ALL) == []


# --- onnxruntime (the core VAD backend) -------------------------------------


def test_missing_onnxruntime_repairs_the_core_install():
    """onnxruntime is a CORE dependency (no extra) — the VAD's only backend
    since #374 vendored the Silero model. The repair is a plain reinstall."""
    steps = plan_steps(python="py", system="Linux", module_present=_present("llama_cpp"))
    assert "onnxruntime" in _names(steps)
    step = next(s for s in steps if s.name == "onnxruntime")
    assert step.argv[:4] == ["py", "-m", "pip", "install"]
    assert step.argv[-1] == "."  # checkout topology, no extras group


def test_every_core_repair_probes_a_module_a_core_dependency_provides():
    """A core repair must be SATISFIABLE by the reinstall it plans.

    `plan_steps` probed `silero_vad` for a year after #374 dropped the package
    from `dependencies`. On every post-#374 install the probe was permanently
    False, so a `pip install -e .` was planned on EVERY launch and could never
    make the module importable — breaking `plan_steps`' own documented "returns
    [] on a warm venv, must not re-run pip" contract on 100% of fresh installs.

    Nothing caught it because every test injects `module_present`, so the probe
    set was never checked against the real dependency list. This test closes
    that: a step whose argv is the no-extras reinstall is a CORE repair, and the
    distribution it declares as `provided_by` must be one `[project].dependencies`
    actually installs.

    `Step.provided_by` — not the step NAME — is what's checked, because a module
    name and a distribution name are different things (`pillow` provides `PIL`,
    `pyyaml` provides `yaml`). Equating them worked for `onnxruntime` by luck,
    and would have failed a correct future step, whose likeliest repair is
    weakening this assertion.
    """
    core = _core_dependency_names()
    reinstall_argv = plan_steps(
        python="py", system="Linux", module_present=lambda _: False
    )  # every probe fails -> every step planned

    for step in reinstall_argv:
        if step.argv[:4] != ["py", "-m", "pip", "install"]:
            continue  # not a pip step (cuda-torch shells out to a module)
        if any(a.endswith("]") for a in step.argv):
            continue  # an EXTRA repair (e.g. `.[summarize]`) — not a core dep.
            # Scan the whole argv, not argv[-1]: the llama_cpp branch appends
            # --extra-index-url/--only-binary AFTER the target.
        assert step.provided_by.replace("_", "-").lower() in core, (
            f"core repair {step.name!r} probes a module that no core dependency "
            f"provides ({step.provided_by!r}); the reinstall it plans can never "
            f"satisfy it. Core deps: {sorted(core)}"
        )


def test_provided_by_defaults_to_the_step_name():
    """Every step today probes a module whose name matches its distribution, so
    the link only has to be RESTATED when they diverge."""
    assert Step(name="onnxruntime", reason="…").provided_by == "onnxruntime"
    assert Step(name="imaging", reason="…", provided_by="pillow").provided_by == "pillow"


def _core_dependency_names() -> set[str]:
    """Normalised distribution names from `[project].dependencies`."""
    import tomllib

    with (Path(__file__).resolve().parent.parent / "pyproject.toml").open("rb") as fh:
        deps = tomllib.load(fh)["project"]["dependencies"]
    return {re.split(r"[<>=!~\[; ]", d, maxsplit=1)[0].strip().replace("_", "-").lower() for d in deps}


def test_onnxruntime_repair_is_not_fatal():
    """The recorder still boots without it — the gate falls back to passthrough.
    Failing the whole launch over it would be worse than the degraded mode."""
    steps = plan_steps(python="py", system="Linux", module_present=_present("llama_cpp"))
    assert next(s for s in steps if s.name == "onnxruntime").fatal is False


# --- the [summarize] extra -------------------------------------------------


@pytest.mark.parametrize(
    ("system", "machine", "probe"),
    [
        ("Darwin", "arm64", "mlx_lm"),
        ("Darwin", "x86_64", "llama_cpp"),
        ("Linux", "x86_64", "llama_cpp"),
        ("Windows", "AMD64", "llama_cpp"),
    ],
)
def test_summarize_probe_follows_the_routed_backend(system, machine, probe):
    """Mirrors LocalSummarizer's resolve_local_backend: mlx_lm on Apple
    Silicon, llama_cpp everywhere else. Probing the wrong module would either
    reinstall on every boot or never install at all."""
    steps = plan_steps(
        python="py",
        system=system,
        machine=machine,
        module_present=_present("onnxruntime", probe),
    )
    assert "summarize" not in _names(steps)

    steps = plan_steps(python="py", system=system, machine=machine, module_present=_present("onnxruntime"))
    assert "summarize" in _names(steps)


def test_summarize_extra_is_requested_by_name():
    steps = plan_steps(python="py", system="Linux", module_present=_present("onnxruntime"))
    argv = next(s for s in steps if s.name == "summarize").argv
    assert any(a.endswith("[summarize]") for a in argv), argv


def test_llama_cpp_install_uses_the_prebuilt_wheel_index():
    """llama-cpp-python builds from source by default, which needs cmake + a C++
    toolchain. A Bundle operator has neither, so the maintainer's prebuilt CPU
    wheel index is not optional here."""
    steps = plan_steps(python="py", system="Linux", module_present=_present("onnxruntime"))
    argv = next(s for s in steps if s.name == "summarize").argv
    assert "--extra-index-url" in argv
    # Exact equality, not a substring: a substring check on a URL is the shape of
    # a sanitisation bug (the host could sit anywhere in the string), and the
    # index is a constant we own — there is nothing to match loosely.
    assert argv[argv.index("--extra-index-url") + 1] == preflight.LLAMA_CPP_WHEEL_INDEX


def test_mlx_summarize_install_has_no_wheel_index():
    """The index is a llama-cpp-python workaround; mlx_lm ships normal wheels."""
    steps = plan_steps(python="py", system="Darwin", machine="arm64", module_present=_present("onnxruntime"))
    assert "--extra-index-url" not in next(s for s in steps if s.name == "summarize").argv


# --- the CUDA torch swap ---------------------------------------------------


def test_cuda_swap_is_planned_on_windows():
    """PyPI's default Windows torch wheel is CPU-only; the Linux wheel bundles
    CUDA. This step is the whole reason `start.ps1` had it and `start.sh` did
    not."""
    steps = plan_steps(python="py", system="Windows", module_present=_ALL)
    assert _names(steps) == ["cuda-torch"]
    assert next(s for s in steps if s.name == "cuda-torch").argv == ["py", "-m", "tapscribe.cuda_torch"]


@pytest.mark.parametrize("system", ["Linux", "Darwin"])
def test_cuda_swap_is_not_planned_off_windows(system):
    assert "cuda-torch" not in _names(plan_steps(python="py", system=system, module_present=_ALL))


# --- topology --------------------------------------------------------------


def test_bundle_wheel_reaches_every_pip_step(tmp_path):
    """A Bundle's repairs must install from the shipped wheel — `-e .` would
    point at a checkout that doesn't exist under %LOCALAPPDATA%."""
    wheel = tmp_path / "tapscribe-1.1.0-py3-none-any.whl"
    wheel.write_bytes(b"")
    steps = plan_steps(
        python="py",
        system="Windows",
        install_spec=str(wheel),
        module_present=_WHEEL_MISSING.__contains__,
    )
    pip_steps = [s for s in steps if "pip" in s.argv]
    assert pip_steps, "expected onnxruntime + summarize repairs"
    for step in pip_steps:
        assert "-e" not in step.argv
        assert any(str(wheel.resolve()) in a for a in step.argv), step.argv


def test_llama_cpp_install_refuses_to_build_from_source():
    """The prebuilt index is an EXTRA index, so pip resolves across it and PyPI and
    picks the highest version — which, whenever PyPI is ahead of abetlen's wheel
    index, is a source distribution.

    That matters far more under a Bundle than it did under start.sh: a Bundle box
    has, by construction, no cmake and no MSVC (the whole point of shipping an
    embedded interpreter), so the build fails, the probe never goes green, and the
    same multi-minute doomed compile repeats on EVERY launch with 'Local summarizer
    needs the [summarize] extra' as the only symptom.

    `--only-binary` is the fix rather than `--index-url`: that index carries ONLY
    llama-cpp-python (numpy and the other deps 404 there), so making it the sole
    index would break dependency resolution outright. Refusing the sdist instead
    fails fast and legibly when no wheel matches.
    """
    steps = plan_steps(python="py", system="Linux", module_present=_present("onnxruntime"))
    argv = next(s for s in steps if s.name == "summarize").argv
    assert "--only-binary" in argv
    assert argv[argv.index("--only-binary") + 1] == "llama-cpp-python"


def test_mlx_summarize_install_does_not_restrict_binaries():
    """The restriction is a llama-cpp-python workaround; mlx_lm resolves normally."""
    steps = plan_steps(python="py", system="Darwin", machine="arm64", module_present=_present("onnxruntime"))
    assert "--only-binary" not in next(s for s in steps if s.name == "summarize").argv
