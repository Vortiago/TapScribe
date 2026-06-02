"""Ensure a CUDA-enabled PyTorch in the venv when an NVIDIA GPU is present.

pip's default `torch` wheel is **CPU-only on Windows** (the Linux wheel
bundles CUDA), so on a Windows + NVIDIA box nothing uses the GPU:
TapScribe's backend probe (`torch.cuda.is_available()` in
`tapscribe.transcribers.catalog`) reports `Available backends: ['cpu']`,
and the live channel's whisperlivekit warmup can't load `cublas64_12.dll`.
The separately-installed `cuda-libs` extra (nvidia-cublas/cudnn) helps
CTranslate2 but can do nothing for a *CPU* torch.

`start.ps1` calls this right after the install picker. It is a **no-op**
when:

  - ``$TAPSCRIBE_NO_CUDA_TORCH`` is set,
  - ``nvidia-smi`` isn't on PATH (no NVIDIA GPU),
  - torch isn't installed yet, or
  - torch is already a CUDA build (``torch.version.cuda`` set — e.g. on
    Linux, whose default wheel bundles CUDA, or after a previous run).

Otherwise it reinstalls the **same** torch version from PyTorch's CUDA
wheel index with ``--force-reinstall --no-deps`` — so the resolved
dependency graph is untouched and the self-contained Windows CUDA wheel
(which bundles cuBLAS/cuDNN) simply swaps in for the CPU one. The CUDA
channel defaults to ``cu124`` and is overridable via
``$TAPSCRIBE_TORCH_CUDA`` (one of the allow-listed channels below).

Stdlib-only: it runs at bring-up and probes only the torch the picker has
already installed. Failure is non-fatal — a GPU optimisation must never
block the recorder from booting on CPU.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess  # nosec B404 — fixed argv list, never shell=True.
import sys

DEFAULT_CHANNEL = "cu124"
# Allow-list the channel so an operator-supplied env var can't inject
# arbitrary text into the pip `--index-url`. Covers the CUDA 12.1–12.8
# wheel lines PyTorch currently publishes.
ALLOWED_CHANNELS = ("cu121", "cu124", "cu126", "cu128")
PYTORCH_INDEX = "https://download.pytorch.org/whl/"
# torch.__version__ without the local build tag, e.g. "2.6.0".
_VERSION_RE = re.compile(r"\A[0-9]+(?:\.[0-9]+)*\Z")


def torch_build() -> tuple[str, str | None] | None:
    """Return ``(version, cuda_tag)`` for the installed torch, or ``None``
    if torch isn't importable. ``cuda_tag`` is ``torch.version.cuda``
    (``None`` on a CPU build); ``version`` is stripped of the local
    ``+cpu`` / ``+cu126`` suffix."""
    try:
        import torch  # type: ignore  # noqa: PLC0415 — probed lazily; not a module-level dep.
    except Exception:  # noqa: BLE001 — a broken/absent torch fails import many ways; treat as "no torch".
        return None
    return (torch.__version__.split("+")[0], torch.version.cuda or None)


def resolve_channel(raw: str | None) -> str:
    """Map an operator-supplied channel to a safe, allow-listed value,
    falling back to the default for unset/unknown input."""
    if raw and raw in ALLOWED_CHANNELS:
        return raw
    if raw:
        print(
            f"[ensure-cuda-torch] ignoring TAPSCRIBE_TORCH_CUDA={raw!r} "
            f"(not one of {', '.join(ALLOWED_CHANNELS)}); using {DEFAULT_CHANNEL}.",
            file=sys.stderr,
            flush=True,
        )
    return DEFAULT_CHANNEL


def pip_argv(version: str, channel: str, *, python: str = sys.executable) -> list[str]:
    """``pip install --force-reinstall --no-deps torch==<v> --index-url <cuda>``.

    ``--no-deps`` keeps the picker-resolved graph intact; the Windows CUDA
    wheel is self-contained (bundles cuBLAS/cuDNN), so no nvidia-* deps are
    needed for the swap."""
    return [
        python,
        "-m",
        "pip",
        "install",
        "--force-reinstall",
        "--no-deps",
        f"torch=={version}",
        "--index-url",
        f"{PYTORCH_INDEX}{channel}",
    ]


def main() -> int:
    if os.environ.get("TAPSCRIBE_NO_CUDA_TORCH"):
        return 0
    # Cheap GPU probe before importing torch (~1-2s) — most boxes bail here.
    if shutil.which("nvidia-smi") is None:
        return 0
    build = torch_build()
    if build is None:
        return 0  # torch not installed (picker installed nothing / failed) — nothing to swap.
    version, cuda_tag = build
    if cuda_tag:
        return 0  # already a CUDA build (Linux wheel, or a prior run swapped it).
    if not _VERSION_RE.match(version):
        print(
            f"[ensure-cuda-torch] unexpected torch version {version!r}; leaving it alone.",
            file=sys.stderr,
            flush=True,
        )
        return 0

    channel = resolve_channel(os.environ.get("TAPSCRIBE_TORCH_CUDA"))
    print(
        f"[ensure-cuda-torch] NVIDIA GPU detected but torch {version} is the CPU build — "
        f"reinstalling torch=={version} from the CUDA index ({channel}). "
        f"Override the channel with TAPSCRIBE_TORCH_CUDA ({'/'.join(ALLOWED_CHANNELS)}).",
        flush=True,
    )
    rc = subprocess.call(pip_argv(version, channel))  # nosec B603 — fixed argv, no shell.
    if rc != 0:
        print(
            f"[ensure-cuda-torch] reinstall failed (rc={rc}). Live + batch will run on CPU. "
            f"If torch {version} isn't published for {channel}, set TAPSCRIBE_TORCH_CUDA to a "
            "channel that has it (try cu126 or cu128), or install torch manually from "
            "https://pytorch.org/get-started/locally/.",
            file=sys.stderr,
            flush=True,
        )
        return 0  # non-fatal: never block bring-up over a GPU optimisation.
    print(
        "[ensure-cuda-torch] CUDA torch installed — the GPU should now be available "
        "to both the live channel and batch transcription.",
        flush=True,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover — exercised via start.ps1 / start.sh
    raise SystemExit(main())
