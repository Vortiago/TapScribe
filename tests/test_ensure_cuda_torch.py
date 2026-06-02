"""Tests for tools/ensure_cuda_torch.py.

Like the install picker, this is a standalone stdlib-only bring-up script
that runs before/around TapScribe's install, so it's imported via path
manipulation rather than as a package module. The GPU probe, the torch
import, and pip itself are all monkeypatched — these tests never touch a
real GPU or run pip.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# tools/ isn't a package — make ensure_cuda_torch importable by name.
TOOLS_DIR = Path(__file__).resolve().parent.parent / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import ensure_cuda_torch as ect  # noqa: E402

CU = "https://download.pytorch.org/whl/"


# ---- pure helpers ----------------------------------------------------------


def test_pip_argv_is_force_reinstall_no_deps_no_version_pin():
    argv = ect.pip_argv("cu128", python="/venv/bin/python")
    assert argv == [
        "/venv/bin/python",
        "-m",
        "pip",
        "install",
        "--force-reinstall",
        "--no-deps",
        "torch",
        "--index-url",
        f"{CU}cu128",
    ]
    # No version pin — pip takes the channel's newest torch.
    assert not any("==" in token for token in argv)


@pytest.mark.parametrize(
    "override,expected",
    [
        (None, list(ect.AUTO_CHANNELS)),
        ("", list(ect.AUTO_CHANNELS)),
        ("cu128", ["cu128"]),
        ("cu130", ["cu130"]),  # CUDA-13 opt-in is accepted (well-formed channel)
        ("cu121", ["cu121"]),
        ("; rm -rf /", list(ect.AUTO_CHANNELS)),  # injection attempt → auto-search
        ("cu1", list(ect.AUTO_CHANNELS)),  # too short → auto-search
        ("nightly", list(ect.AUTO_CHANNELS)),  # not a channel → auto-search
    ],
)
def test_channels_to_try(override, expected):
    assert ect.channels_to_try(override) == expected


def test_auto_channels_are_newest_first_and_cuda12():
    order = ect.AUTO_CHANNELS
    assert order[0] == "cu128"  # newest CUDA-12.x is the default
    assert order.index("cu128") < order.index("cu126") < order.index("cu124")
    assert "cu130" not in order  # CUDA-13 is override-only, never auto-tried


# ---- main() decision matrix ------------------------------------------------


def _patch(monkeypatch, *, nvidia, build, rcs=(0,)):
    """Wire shutil.which + torch_build, and capture pip invocations. `rcs`
    is the sequence of return codes subprocess.call yields (last repeats)."""
    monkeypatch.delenv("TAPSCRIBE_NO_CUDA_TORCH", raising=False)
    monkeypatch.delenv("TAPSCRIBE_TORCH_CUDA", raising=False)
    monkeypatch.setattr(ect.shutil, "which", lambda _cmd: "/usr/bin/nvidia-smi" if nvidia else None)
    monkeypatch.setattr(ect, "torch_build", lambda: build)
    calls: list[list[str]] = []
    rc_iter = iter(rcs)

    def fake_call(argv, *a, **k):
        calls.append(argv)
        try:
            return next(rc_iter)
        except StopIteration:
            return rcs[-1]

    monkeypatch.setattr(ect.subprocess, "call", fake_call)
    return calls


def test_noop_without_nvidia(monkeypatch):
    calls = _patch(monkeypatch, nvidia=False, build=("2.12.0", None))
    assert ect.main() == 0
    assert calls == []


def test_noop_when_torch_missing(monkeypatch):
    calls = _patch(monkeypatch, nvidia=True, build=None)
    assert ect.main() == 0
    assert calls == []


def test_noop_when_already_cuda_build(monkeypatch):
    calls = _patch(monkeypatch, nvidia=True, build=("2.12.0", "12.8"))
    assert ect.main() == 0
    assert calls == []


def test_noop_when_skip_env_set(monkeypatch):
    calls = _patch(monkeypatch, nvidia=True, build=("2.12.0", None))
    monkeypatch.setenv("TAPSCRIBE_NO_CUDA_TORCH", "1")
    assert ect.main() == 0
    assert calls == []


def test_installs_newest_from_first_channel(monkeypatch):
    calls = _patch(monkeypatch, nvidia=True, build=("2.12.0", None), rcs=(0,))
    assert ect.main() == 0
    assert len(calls) == 1
    assert calls[0][-1] == f"{CU}cu128"  # newest CUDA-12.x first
    assert "torch" in calls[0] and not any("==" in t for t in calls[0])


def test_falls_through_to_next_channel_on_failure(monkeypatch):
    calls = _patch(monkeypatch, nvidia=True, build=("2.12.0", None), rcs=(1, 0))
    assert ect.main() == 0
    assert len(calls) == 2
    assert calls[0][-1] == f"{CU}cu128"
    assert calls[1][-1] == f"{CU}cu126"


def test_override_pins_single_channel(monkeypatch):
    calls = _patch(monkeypatch, nvidia=True, build=("2.12.0", None), rcs=(0,))
    monkeypatch.setenv("TAPSCRIBE_TORCH_CUDA", "cu130")  # CUDA-13 opt-in
    assert ect.main() == 0
    assert len(calls) == 1
    assert calls[0][-1] == f"{CU}cu130"


def test_all_channels_fail_is_non_fatal(monkeypatch):
    calls = _patch(monkeypatch, nvidia=True, build=("2.12.0", None), rcs=(1,))
    assert ect.main() == 0  # never blocks bring-up
    assert len(calls) == len(ect.AUTO_CHANNELS)
