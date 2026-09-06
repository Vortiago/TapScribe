"""Bring-up steps that must run between "a venv exists" and "the recorder boots".

`start.sh` / `start.ps1` accumulated a set of probe-then-repair steps that are
not part of the model install picker: repair `onnxruntime` (the core VAD
backend) in an incomplete venv, pull the `[summarize]` extra so the Local
summarizer works on first Generate, and — on Windows only — swap PyPI's
CPU-only torch wheel for a CUDA build.

A Bundle (ADR-0015) has no `start.ps1`, so those steps were homeless. Skipping
the CUDA one in particular is silently expensive: every Windows NVIDIA operator
would get `Available backends: ['cpu']` with nothing pointing at why. Rather
than reimplement the probes in the tray's C#, both callers run this module,
so a fifth check added later can't drift between two copies.

`plan_steps` is pure and returns the work as data — every probe is injected —
so the decision matrix is testable without a GPU, without torch installed, and
without running pip. `main` executes a plan.

Stdlib-only, like its siblings: this runs against a venv that may hold nothing
but pip. (`PYTHONUNBUFFERED` is deliberately NOT here — it must be set in the
recorder's *own* environment by whoever launches it, which a separate preflight
process cannot do. `start.ps1` sets it; the Bundle's tray sets it on the child.)
"""

from __future__ import annotations

import argparse
import importlib.util
import platform
import subprocess  # nosec B404 — fixed argv lists, no shell.
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from tapscribe import install_target

#: Where a relative pip target (`-e .`) must resolve from — the repo root in a
#: checkout. Harmless for the wheel/PyPI topologies, whose specs are absolute.
REPO_ROOT = Path(__file__).resolve().parent.parent


def _call_in_repo_root(argv: list[str]) -> int:
    return subprocess.call(argv, cwd=REPO_ROOT)  # nosec B603 — fixed argv list, no shell.


#: llama-cpp-python builds from source by default (needs cmake + MSVC). A
#: Bundle operator has neither, so the maintainer's prebuilt CPU-wheel index is
#: required, not a nicety. CUDA-accelerated summary inference stays opt-in — a
#: cuXXX index from the same host swaps in.
LLAMA_CPP_WHEEL_INDEX = "https://abetlen.github.io/llama-cpp-python/whl/cpu"


@dataclass(frozen=True)
class Step:
    """One planned bring-up action.

    `fatal=False` means a failure is reported and the launch continues: every
    step here degrades a *feature* (the silence gate, the Local summarizer, GPU
    acceleration) rather than breaking the recorder, and refusing to boot over
    a failed optional repair would be worse than the degraded mode.

    `provided_by` names the **distribution** whose install satisfies this step's
    probe, which is not always the probed MODULE name (`pillow` provides `PIL`,
    `pyyaml` provides `yaml`). It defaults to `name` because today's steps all
    happen to match, but stating the link as DATA is what lets the test suite
    cross-check a core repair against `[project].dependencies` — inferring it by
    string equality would fail a perfectly correct future step and invite
    weakening the assertion instead.
    """

    name: str
    reason: str
    argv: list[str] = field(default_factory=list)
    fatal: bool = False
    provided_by: str = ""

    def __post_init__(self) -> None:
        if not self.provided_by:
            # frozen=True, so assign through object.__setattr__.
            object.__setattr__(self, "provided_by", self.name)


def _diarize_model_present() -> bool:
    """Imported at call time so this module stays stdlib-only at import — the
    venv it runs against may hold nothing but pip."""
    from tapscribe.diarizers import model

    return model.model_present()


def _module_present(name: str) -> bool:
    """`find_spec`, not `import` — probing must not pay torch's multi-second
    import cost on every bring-up."""
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        # A half-installed package can raise rather than return None; treat it
        # as absent so the repair below reinstalls it.
        return False


def summarize_probe_module(system: str, machine: str) -> str:
    """Which module proves the `[summarize]` extra is usable on this host.

    Mirrors `LocalSummarizer.resolve_local_backend`'s routing: the MLX backend
    on Apple Silicon, the GGUF/llama.cpp one everywhere else. Probing the wrong
    module would either reinstall on every boot or never install at all.
    """
    if system == "Darwin" and machine in ("arm64", "aarch64"):
        return "mlx_lm"
    return "llama_cpp"


def plan_steps(
    *,
    python: str = sys.executable,
    install_spec: str | None = None,
    system: str | None = None,
    machine: str | None = None,
    module_present: Callable[[str], bool] = _module_present,
    model_present: Callable[[], bool] = _diarize_model_present,
) -> list[Step]:
    """The bring-up steps this host needs, in execution order.

    Pure: every probe is injected, defaulting to the real host. Returns `[]` on
    a warm venv — the common re-launch case must be silent and must not re-run
    pip.
    """
    system = system or platform.system()
    machine = machine or platform.machine()
    steps: list[Step] = []

    if not module_present("onnxruntime"):
        steps.append(
            Step(
                name="onnxruntime",
                reason=(
                    "onnxruntime is a core dependency but isn't importable — this venv "
                    "is incomplete. Without it every /tap falls back to passthrough, "
                    "silently disabling the gate the operator picked."
                ),
                argv=install_target.pip_install_argv([], install_spec=install_spec, python=python),
            )
        )

    probe = summarize_probe_module(system, machine)
    if not module_present(probe):
        argv = install_target.pip_install_argv(["summarize"], install_spec=install_spec, python=python)
        if probe == "llama_cpp":
            # The wheel index is an EXTRA index, not the only one, because it carries
            # llama-cpp-python and nothing else — its deps (numpy, diskcache, jinja2)
            # would 404 if we made it authoritative with --index-url.
            #
            # But that means pip resolves across it AND PyPI and takes the highest
            # version, which whenever PyPI is ahead is an sdist. `--only-binary` refuses
            # that: a Bundle box has no cmake and no MSVC by construction, so a source
            # build is guaranteed to fail — and because this step is non-fatal and
            # nothing stamps the attempt, it would silently retry the same doomed
            # multi-minute compile on EVERY launch. Failing fast is strictly better.
            argv += [
                "--extra-index-url",
                LLAMA_CPP_WHEEL_INDEX,
                "--only-binary",
                "llama-cpp-python",
            ]
        steps.append(
            Step(
                name="summarize",
                reason=(
                    f"{probe} isn't importable, so the Local summarizer source would "
                    "report the [summarize] extra missing on the first Generate."
                ),
                argv=argv,
            )
        )

    if not model_present():
        steps.append(
            Step(
                name="diarize-model",
                reason=(
                    "the speaker-embedding model isn't on disk, so diarization would "
                    "leave every multi-person tap as one speaker in the transcript."
                ),
                # Unlike the llama_cpp step, retrying every launch is CORRECT
                # here: an offline box fails in milliseconds, and the operator
                # gets the model the first time it launches with connectivity.
                # There is nothing to build and nothing to stamp.
                argv=[python, "-m", "tapscribe.diarizers.model"],
            )
        )

    if system == "Windows":
        # Windows only: pip's default torch wheel is CPU-only there, while the
        # Linux wheel bundles CUDA. `cuda_torch` self-gates on nvidia-smi and on
        # torch already being a CUDA build, so this is cheap on a non-NVIDIA box.
        steps.append(
            Step(
                name="cuda-torch",
                reason=(
                    "pip's default Windows torch wheel is CPU-only, so an NVIDIA box "
                    "would report Available backends: ['cpu'] and the live channel "
                    "could not load cublas64_12.dll."
                ),
                argv=[python, "-m", "tapscribe.cuda_torch"],
            )
        )

    return steps


def run_steps(steps: list[Step], *, run: Callable[[list[str]], int] | None = None) -> int:
    """Execute a plan. Returns 0 unless a `fatal` step failed.

    Non-fatal failures are printed with the step's `reason` so the operator
    learns which capability just degraded — in a Bundle this is the only trace,
    since the Bundle's tray pipes this to a log file rather than a terminal.

    Steps run with `cwd=REPO_ROOT`, matching `install_picker.run_install`. That
    is load-bearing for the checkout topology, where the pip target is the
    RELATIVE `-e .`: `start.sh` / `start.ps1` used to pin it by `cd`-ing to the
    script's directory before inlining these commands, but this module is a
    standalone entry point (`python -m tapscribe.preflight`) that can be run
    from anywhere. Without it, `-e .` resolves against an arbitrary cwd and
    either fails opaquely or installs an unrelated local project.
    """
    runner = run or _call_in_repo_root
    rc = 0
    for step in steps:
        print(f"[preflight] {step.name}: {step.reason}", flush=True)
        print(f"[preflight] running: {' '.join(step.argv)}", flush=True)
        status = runner(step.argv)
        if status != 0:
            print(f"[preflight] {step.name} failed (exit {status}).", file=sys.stderr, flush=True)
            if step.fatal:
                rc = status
    return rc


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m tapscribe.preflight",
        description="Run TapScribe's bring-up repairs (onnxruntime, [summarize], CUDA torch).",
    )
    p.add_argument(
        "--install-spec",
        default=None,
        help="What pip installs TapScribe from: omitted (a dev checkout), a path "
        "to the Bundle's shipped .whl, or a pinned 'tapscribe==X.Y.Z'. See ADR-0015.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the planned steps and exit without running them.",
    )
    args = p.parse_args(argv)

    # Validated here for the same reason the picker does it: a CLI value that
    # flows into a pip argv is external input by CodeQL's reckoning (CLAUDE.md).
    try:
        install_target.resolve_install_spec(args.install_spec)
    except install_target.InstallSpecError as exc:
        print(f"[preflight] {exc}", file=sys.stderr, flush=True)
        return 2

    steps = plan_steps(install_spec=args.install_spec)
    if not steps:
        print("[preflight] nothing to do.", flush=True)
        return 0
    if args.dry_run:
        for step in steps:
            print(f"[preflight] would run: {' '.join(step.argv)}", flush=True)
        return 0
    return run_steps(steps)


if __name__ == "__main__":
    raise SystemExit(main())
