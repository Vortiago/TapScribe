"""RED contract for `tapscribe.preflight` — the shared bring-up steps.

`start.ps1` grew a pile of probe-then-repair logic that has to happen between
"a venv exists" and "the recorder boots": repair silero-vad if the venv predates
it becoming a core dep, pull the `[summarize]` extra, and swap CPU torch for a
CUDA build on an NVIDIA Windows box. A Bundle has no `start.ps1`, so those steps
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

import pytest

from tapscribe.preflight import Step, plan_steps

_WHEEL_MISSING: frozenset[str] = frozenset()


def _present(*names: str):
    """A `module_present` probe reporting exactly `names` as importable."""
    return lambda name: name in names


_ALL = _present("silero_vad", "llama_cpp", "mlx_lm")


def _names(steps: list[Step]) -> list[str]:
    return [s.name for s in steps]


# --- the quiet path --------------------------------------------------------


def test_nothing_to_do_when_every_probe_passes():
    """The common case — a warm venv on a re-launch. Preflight must be silent
    and cheap, not re-run pip on every boot."""
    assert plan_steps(python="py", system="Linux", module_present=_ALL) == []


# --- silero-vad ------------------------------------------------------------


def test_missing_silero_repairs_the_core_install():
    """silero-vad is a CORE dependency (no extra) — a venv without it predates
    that change. The repair is a plain reinstall, no extras."""
    steps = plan_steps(python="py", system="Linux", module_present=_present("llama_cpp"))
    assert "silero-vad" in _names(steps)
    step = next(s for s in steps if s.name == "silero-vad")
    assert step.argv[:4] == ["py", "-m", "pip", "install"]
    assert step.argv[-1] == "."  # checkout topology, no extras group


def test_silero_repair_is_not_fatal():
    """The recorder still boots without it — the gate falls back to passthrough.
    Failing the whole launch over it would be worse than the degraded mode."""
    steps = plan_steps(python="py", system="Linux", module_present=_present("llama_cpp"))
    assert next(s for s in steps if s.name == "silero-vad").fatal is False


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
        module_present=_present("silero_vad", probe),
    )
    assert "summarize" not in _names(steps)

    steps = plan_steps(python="py", system=system, machine=machine, module_present=_present("silero_vad"))
    assert "summarize" in _names(steps)


def test_summarize_extra_is_requested_by_name():
    steps = plan_steps(python="py", system="Linux", module_present=_present("silero_vad"))
    argv = next(s for s in steps if s.name == "summarize").argv
    assert any(a.endswith("[summarize]") for a in argv), argv


def test_llama_cpp_install_uses_the_prebuilt_wheel_index():
    """llama-cpp-python builds from source by default, which needs cmake + a C++
    toolchain. A Bundle operator has neither, so the maintainer's prebuilt CPU
    wheel index is not optional here."""
    steps = plan_steps(python="py", system="Linux", module_present=_present("silero_vad"))
    argv = next(s for s in steps if s.name == "summarize").argv
    assert "--extra-index-url" in argv
    assert "abetlen.github.io" in argv[argv.index("--extra-index-url") + 1]


def test_mlx_summarize_install_has_no_wheel_index():
    """The index is a llama-cpp-python workaround; mlx_lm ships normal wheels."""
    steps = plan_steps(python="py", system="Darwin", machine="arm64", module_present=_present("silero_vad"))
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
    assert pip_steps, "expected silero + summarize repairs"
    for step in pip_steps:
        assert "-e" not in step.argv
        assert any(str(wheel.resolve()) in a for a in step.argv), step.argv
