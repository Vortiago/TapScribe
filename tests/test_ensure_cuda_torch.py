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

# ---- pure helpers ----------------------------------------------------------


def test_pip_argv_is_force_reinstall_no_deps_from_cuda_index():
    argv = ect.pip_argv("2.6.0", "cu124", python="/venv/bin/python")
    assert argv == [
        "/venv/bin/python",
        "-m",
        "pip",
        "install",
        "--force-reinstall",
        "--no-deps",
        "torch==2.6.0",
        "--index-url",
        "https://download.pytorch.org/whl/cu124",
    ]


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, "cu124"),
        ("", "cu124"),
        ("cu128", "cu128"),
        ("cu121", "cu121"),
        ("cu999", "cu124"),  # unknown → default
        ("; rm -rf /", "cu124"),  # injection attempt → default
    ],
)
def test_resolve_channel_allowlists(raw, expected):
    assert ect.resolve_channel(raw) == expected


# ---- main() decision matrix ------------------------------------------------


def _patch(monkeypatch, *, nvidia, build):
    """Wire shutil.which + torch_build, and capture any pip invocation."""
    monkeypatch.delenv("TAPSCRIBE_NO_CUDA_TORCH", raising=False)
    monkeypatch.delenv("TAPSCRIBE_TORCH_CUDA", raising=False)
    monkeypatch.setattr(ect.shutil, "which", lambda _cmd: "/usr/bin/nvidia-smi" if nvidia else None)
    monkeypatch.setattr(ect, "torch_build", lambda: build)
    calls: list[list[str]] = []
    monkeypatch.setattr(ect.subprocess, "call", lambda argv, *a, **k: calls.append(argv) or 0)
    return calls


def test_noop_without_nvidia(monkeypatch):
    calls = _patch(monkeypatch, nvidia=False, build=("2.6.0", None))
    assert ect.main() == 0
    assert calls == []


def test_noop_when_torch_missing(monkeypatch):
    calls = _patch(monkeypatch, nvidia=True, build=None)
    assert ect.main() == 0
    assert calls == []


def test_noop_when_already_cuda_build(monkeypatch):
    calls = _patch(monkeypatch, nvidia=True, build=("2.6.0", "12.4"))
    assert ect.main() == 0
    assert calls == []


def test_noop_when_skip_env_set(monkeypatch):
    calls = _patch(monkeypatch, nvidia=True, build=("2.6.0", None))
    monkeypatch.setenv("TAPSCRIBE_NO_CUDA_TORCH", "1")
    assert ect.main() == 0
    assert calls == []


def test_reinstalls_cpu_build_on_nvidia(monkeypatch):
    calls = _patch(monkeypatch, nvidia=True, build=("2.6.0", None))
    assert ect.main() == 0
    assert len(calls) == 1
    argv = calls[0]
    assert "torch==2.6.0" in argv
    assert argv[-1] == "https://download.pytorch.org/whl/cu124"


def test_channel_override_is_honoured(monkeypatch):
    calls = _patch(monkeypatch, nvidia=True, build=("2.7.0", None))
    monkeypatch.setenv("TAPSCRIBE_TORCH_CUDA", "cu128")
    assert ect.main() == 0
    assert calls[0][-1] == "https://download.pytorch.org/whl/cu128"


def test_weird_version_is_left_alone(monkeypatch):
    calls = _patch(monkeypatch, nvidia=True, build=("2.6.0a1git", None))
    assert ect.main() == 0
    assert calls == []


def test_pip_failure_is_non_fatal(monkeypatch):
    _patch(monkeypatch, nvidia=True, build=("2.6.0", None))
    monkeypatch.setattr(ect.subprocess, "call", lambda *a, **k: 1)  # pip fails
    assert ect.main() == 0  # still returns 0 — never blocks bring-up
