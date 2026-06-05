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

Otherwise it installs the **newest CUDA-12.x torch** with
``--force-reinstall --no-deps`` — the resolved dependency graph is left
intact and the self-contained Windows CUDA wheel (bundling cuBLAS/cuDNN)
swaps in for the CPU one. We don't pin the version (PyTorch freezes old
CUDA wheel lines at old torch versions, so an exact pin fails wherever it
isn't published); we just take whatever the channel offers.

Why CUDA-12.x only by default: pip will install *any* wheel regardless of
the driver, but a ``cu130`` (CUDA 13) wheel needs a CUDA-13 driver — on a
12.x driver it installs "successfully" yet ``cuda.is_available()`` stays
``False``. Within the 12.x series CUDA minor-version compatibility means a
``cu128`` wheel runs on *any* 12.x driver, so ``cu128`` is the safe newest
default. An operator on a CUDA-13 box can opt in with
``$TAPSCRIBE_TORCH_CUDA=cu130`` (any ``cuNNN`` value is accepted).

Stdlib-only; failure is non-fatal — a GPU optimisation must never block the
recorder from booting on CPU.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess  # nosec B404 — fixed argv list, never shell=True.
import sys

# Auto-search order (no override): newest CUDA-12.x first. All of these run
# on any 12.x driver via CUDA minor-version compatibility, so cu128 is the
# safe "newest". cu130+ is deliberately NOT auto-tried (needs a CUDA-13
# driver) — opt in via the override.
AUTO_CHANNELS = ("cu128", "cu126", "cu124", "cu121")
PYTORCH_INDEX = "https://download.pytorch.org/whl/"
# An operator override must look like a PyTorch CUDA channel ("cu" + 2-3
# digits) — a tight pattern that both documents the shape and stops an env
# var from injecting arbitrary text into pip's --index-url.
_CHANNEL_RE = re.compile(r"\Acu[0-9]{2,3}\Z")


def torch_build() -> tuple[str, str | None] | None:
    """Return ``(version, cuda_tag)`` for the installed torch, or ``None``
    if torch isn't importable. ``cuda_tag`` is ``torch.version.cuda``
    (``None`` on a CPU build); ``version`` is stripped of the local
    ``+cpu`` / ``+cu128`` suffix and is used only for messaging."""
    try:
        import torch  # type: ignore  # noqa: PLC0415 — probed lazily; not a module-level dep.
    except Exception:  # noqa: BLE001 — a broken/absent torch fails import many ways; treat as "no torch".
        return None
    return (torch.__version__.split("+")[0], torch.version.cuda or None)


def channels_to_try(override: str | None) -> list[str]:
    """The channels to attempt, in order. A well-formed operator override
    pins a single channel; anything else (unset or malformed) falls back to
    the newest→oldest CUDA-12.x auto-search."""
    if override and _CHANNEL_RE.match(override):
        return [override]
    return list(AUTO_CHANNELS)


def pip_argv(channel: str, *, python: str = sys.executable) -> list[str]:
    """``pip install --force-reinstall --no-deps torch --index-url <cuda>``.

    No version pin — pip takes the newest torch the channel publishes for
    this interpreter. ``--no-deps`` keeps the picker-resolved graph intact;
    the Windows CUDA wheel is self-contained (bundles cuBLAS/cuDNN), so no
    nvidia-* deps are needed for the swap."""
    return [
        python,
        "-m",
        "pip",
        "install",
        "--force-reinstall",
        "--no-deps",
        "torch",
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

    override = os.environ.get("TAPSCRIBE_TORCH_CUDA") or None
    if override and not _CHANNEL_RE.match(override):
        print(
            f"[ensure-cuda-torch] ignoring malformed TAPSCRIBE_TORCH_CUDA={override!r} "
            "(expected e.g. cu128); auto-searching CUDA-12.x channels instead.",
            file=sys.stderr,
            flush=True,
        )
        override = None
    channels = channels_to_try(override)

    print(
        f"[ensure-cuda-torch] NVIDIA GPU detected but torch {version} is the CPU build. "
        f"Installing the newest CUDA torch — trying channels: {', '.join(channels)}.",
        flush=True,
    )
    for idx, channel in enumerate(channels, start=1):
        print(
            f"[ensure-cuda-torch] [{idx}/{len(channels)}] installing newest torch from {channel} …",
            flush=True,
        )
        rc = subprocess.call(pip_argv(channel))  # nosec B603 — fixed argv, no shell.
        if rc == 0:
            print(
                f"[ensure-cuda-torch] installed a CUDA torch from {channel} — the GPU should now be "
                "available to both the live channel and batch transcription.",
                flush=True,
            )
            return 0

    print(
        f"[ensure-cuda-torch] no reachable channel served a torch wheel (tried "
        f"{', '.join(channels)}). Live + batch will run on CPU. If you're on a CUDA-13 driver try "
        "TAPSCRIBE_TORCH_CUDA=cu130; otherwise pick your channel at "
        "https://download.pytorch.org/whl/torch/ or install torch manually from "
        "https://pytorch.org/get-started/locally/.",
        file=sys.stderr,
        flush=True,
    )
    return 0  # non-fatal: never block bring-up over a GPU optimisation.


if __name__ == "__main__":  # pragma: no cover — exercised via start.ps1 / start.sh
    raise SystemExit(main())
