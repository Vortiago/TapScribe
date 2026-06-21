# TapScribe — Windows / PowerShell bring-up script.
#
# Usage (from the repo root):
#   .\start.ps1                          # localhost only
#   .\start.ps1 -Lan                     # bind to 0.0.0.0 (LAN access)
#   .\start.ps1 -NoMlx                   # skip MLX (irrelevant on Windows but accepted for parity)
#   .\start.ps1 -NoAutoLive              # boot without starting the live channel
#   .\start.ps1 -NoAuth                  # disable dashboard auth + /tap token gate (DEV ONLY)
#   .\start.ps1 -Tls                     # serve https:// + wss:// (auto self-signed)
#   .\start.ps1 -NonInteractive          # skip the install picker prompt; use the saved selection

[CmdletBinding()]
param(
    [switch]$Lan,
    [switch]$NoMlx,
    [switch]$NoAutoLive,
    [switch]$NoAuth,
    [switch]$Tls,
    [switch]$NonInteractive
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$Py = "python"
try {
    $ver = & $Py -c "import sys; print('%d.%d' % sys.version_info[:2])"
} catch {
    Write-Error "No 'python' on PATH. Install Python 3.10+ from python.org and try again."
    exit 1
}

# --- venv -------------------------------------------------------------------
if (-not (Test-Path ".venv\Scripts\Activate.ps1")) {
    if (Test-Path ".venv") {
        Write-Host "[start] Existing .venv is incomplete; removing it."
        Remove-Item -Recurse -Force .venv
    }
    Write-Host "[start] Creating venv at .venv"
    & $Py -m venv .venv
}
. .\.venv\Scripts\Activate.ps1

# Force unbuffered stdout/stderr from every child Python we spawn — without
# this, PowerShell's non-TTY stdout makes Python block-buffer, so the recorder
# appears to "hang" while torch/transformers are importing on first launch.
$env:PYTHONUNBUFFERED = "1"

Write-Host "[start] Upgrading pip…"
& python -m pip install --upgrade pip

# --- Install picker ---------------------------------------------------------
# Hands the install decision to tools/install_picker.py: prompts the
# operator for which model families (Whisper / Voxtral / Parakeet) to
# install, pre-checks the saved selection so re-runs are one keystroke,
# then runs `pip install -e ".[…]"` for the resolved extras —
# skipping pip entirely when the selection and pyproject.toml are
# unchanged since the last install (no more uninstall/reinstall churn).
$PickerArgs = @()
if ($NoMlx)           { $PickerArgs += "--no-mlx" }
if ($NonInteractive)  { $PickerArgs += "--non-interactive" }
& python tools\install_picker.py @PickerArgs
if ($LASTEXITCODE -ne 0) {
    Write-Error "[start] install picker failed; aborting."
    exit 1
}

# --- Runtime python deps ----------------------------------------------------
# The TapScribe per-tap silence gate (gate_kind="tapscribe", which is the
# default) imports silero_vad lazily on the first /tap WS. Missing → the tap
# falls back to passthrough mode ("gate construction failed … falling back to
# passthrough"), which silently disables the gate the operator picked. Install
# the [vad] extra so the dependency is satisfied alongside the model install.
# No-op on re-runs once installed. (No ffmpeg branch here: the array-accepting
# backends — mlx-whisper, parakeet-mlx, and the transformers Parakeet path —
# pre-decode the recorder's WAV via tapscribe/wav_predecode.py and skip the
# ffmpeg-shelling audio loaders the upstream packages would otherwise use. See
# CLAUDE.md.)
#
# `find_spec` instead of `import silero_vad` so we don't pay the ~1-2s torch
# import on every recorder bring-up just to probe whether silero-vad is on
# the import path.
& python -c "import importlib.util,sys; sys.exit(0 if importlib.util.find_spec('silero_vad') else 1)" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "[start] silero-vad missing — installing the [vad] extra so the TapScribe gate works…"
    # NOT --quiet: torch is ~700MB and wheel resolution sometimes fails; visible
    # pip output gives the operator something to act on instead of a mute warning.
    & python -m pip install -e ".[vad]"
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "[start] 'pip install -e .[vad]' failed. The recorder will still boot, but the TapScribe silence gate will fall back to passthrough on every /tap."
    }
}

# The Local summarizer source (Summary stage, #86) runs a bundled offline model
# — an MLX backend on Apple Silicon, a GGUF/llama.cpp backend on CPU/CUDA. The
# adapter lazy-imports the routed backend on the first Generate; missing → the
# Local source reports "needs the [summarize] extra" instead of summarizing.
# Pull the [summarize] extra here so the first Generate just works. No-op on
# re-runs once installed. (Like [vad] above, NOT via the install picker, which
# covers transcription model extras only.)
#
# Probe the module the extra installs on THIS platform (mirrors
# LocalSummarizer's _resolve_local_backend): mlx_lm on Apple Silicon, else
# llama_cpp. find_spec, not import, to skip the heavy backend import on bring-up.
$SummarizeProbe = "llama_cpp"
if ($IsMacOS) {
    if ((uname -m) -eq "arm64") { $SummarizeProbe = "mlx_lm" }
}
& python -c "import importlib.util,sys; sys.exit(0 if importlib.util.find_spec('$SummarizeProbe') else 1)" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "[start] $SummarizeProbe missing — installing the [summarize] extra (bundled offline summarizer)…"
    # NOT --quiet: pulls a text-gen backend (and, on first Generate, a multi-GB
    # model), so visible output makes a failure recoverable.
    $SummarizePipArgs = @()
    if ($SummarizeProbe -eq "llama_cpp") {
        # llama-cpp-python builds from source by default (needs cmake + MSVC).
        # Use the maintainer's prebuilt CPU-wheel index so a box without a C++
        # toolchain still installs. (CUDA-accelerated summary inference is
        # opt-in: swap in a cuXXX wheel index from
        # https://abetlen.github.io/llama-cpp-python/whl/ if you want it.)
        $SummarizePipArgs = @("--extra-index-url", "https://abetlen.github.io/llama-cpp-python/whl/cpu")
    }
    & python -m pip install -e ".[summarize]" @SummarizePipArgs
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "[start] 'pip install -e .[summarize]' failed. The recorder will still boot, but the Local summarizer source will report the [summarize] extra is missing."
    }
}

# --- CUDA Torch (Windows) ---------------------------------------------------
# pip's default `torch` wheel is CPU-only on Windows (the Linux wheel bundles
# CUDA), so on an NVIDIA box the GPU goes unused — TapScribe's probe reports
# "Available backends: ['cpu']" and the live channel's whisperlivekit warmup
# can't load cublas64_12.dll. When a GPU is present and the venv's torch is the
# CPU build, install the newest CUDA torch from PyTorch's index (searching
# newest→oldest CUDA channels). No-op without nvidia-smi, or when torch is
# already a CUDA build. Force a single channel with $env:TAPSCRIBE_TORCH_CUDA
# (e.g. cu128); skip entirely with $env:TAPSCRIBE_NO_CUDA_TORCH=1. Non-fatal:
# CPU fallback on failure.
& python tools\ensure_cuda_torch.py

# --- Configuration ----------------------------------------------------------
$Model = if ($env:SX_MODEL) { $env:SX_MODEL } else { "tiny.en" }
$LangCode = if ($env:SX_LANG) { $env:SX_LANG } else { "en" }
$PortRec = if ($env:SX_PORT_REC) { $env:SX_PORT_REC } else { "8001" }
$PortWlk = if ($env:SX_PORT_WLK) { $env:SX_PORT_WLK } else { "" }
if ($env:SX_HOST) {
    $BindHost = $env:SX_HOST
} elseif ($Lan) {
    $BindHost = "0.0.0.0"
} else {
    $BindHost = "localhost"
}

$ExtraArgs = @()
if ($NoMlx)      { $ExtraArgs += "--no-mlx" }
if ($NoAutoLive) { $ExtraArgs += "--no-auto-live" }
if ($NoAuth)     { $ExtraArgs += "--no-auth" }
if ($Tls)        { $ExtraArgs += "--tls" }
if ($PortWlk) {
    $ExtraArgs += "--live-port"
    $ExtraArgs += $PortWlk
    $LiveLabel = "ws://${BindHost}:${PortWlk}/asr"
} else {
    $LiveLabel = "ephemeral (internal; recorder chooses a free port each start)"
}

Write-Host ""
Write-Host "[start] Launching TapScribe…"
Write-Host "        Dashboard       http://${BindHost}:${PortRec}/"
Write-Host "        Live channel    $LiveLabel"
Write-Host "        (first launch can take 10–30s while torch/transformers import — be patient)"
Write-Host ""

& python -u -m tapscribe `
    --host $BindHost `
    --port $PortRec `
    --live-model $Model `
    --live-language $LangCode `
    @ExtraArgs
