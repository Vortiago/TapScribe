# TapScribe — Windows / PowerShell bring-up script.
#
# Usage (from the repo root):
#   .\start.ps1                          # localhost only
#   .\start.ps1 -Lan                     # bind to 0.0.0.0 (LAN access)
#   .\start.ps1 -NoMlx                   # skip MLX (irrelevant on Windows but accepted for parity)
#   .\start.ps1 -AutoLive                # boot with live channel auto-started
#   .\start.ps1 -NoAutoLive              # [deprecated — off is the default] accepted for backward-compat
#   .\start.ps1 -NoAuth                  # disable dashboard auth + /tap token gate (DEV ONLY)
#   .\start.ps1 -Tls                     # serve https:// + wss:// (auto self-signed)
#   .\start.ps1 -NonInteractive          # install the saved/default selection in-terminal (no browser)

[CmdletBinding()]
param(
    [switch]$Lan,
    [switch]$NoMlx,
    [switch]$AutoLive,
    [switch]$NoAutoLive,  # deprecated — off is the default; accepted for parity with start.sh, not forwarded
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

# --- Model install ----------------------------------------------------------
# Models are chosen in the BROWSER at /setup (the recorder redirects there until
# a model is installed). This script only makes the package importable:
#   * First run, interactive: base-install the package; pick models at /setup.
#   * Re-run, or -NonInteractive: re-apply the saved selection via
#     `install_picker --non-interactive`. To pick models in the terminal, run
#     `python -m tapscribe.install_picker` directly.
$BrowserSetup = $false
if (-not (Test-Path ".tapscribe-install.json") -and -not $NonInteractive) {
    Write-Host "[start] First run — installing the base package; you'll choose models in the browser."
    & python -m pip install -e .
    if ($LASTEXITCODE -ne 0) {
        Write-Error "[start] base 'pip install -e .' failed; aborting."
        exit 1
    }
    $BrowserSetup = $true
} else {
    # Always non-interactive: re-apply the saved selection with no prompt.
    $PickerArgs = @("--non-interactive")
    if ($NoMlx) { $PickerArgs += "--no-mlx" }
    & python -m tapscribe.install_picker @PickerArgs
    if ($LASTEXITCODE -ne 0) {
        Write-Error "[start] install picker failed; aborting."
        exit 1
    }
}

# --- Runtime python deps + CUDA torch ---------------------------------------
# Probe-then-repair for everything the install picker does NOT cover:
#   * onnxruntime — the backend for the vendored Silero model behind
#     `tapscribe.vad`, i.e. the per-tap silence gate. A CORE dependency, so this
#     only repairs an incomplete venv; missing → every /tap falls back to
#     passthrough, silently disabling the gate the operator picked.
#   * the [summarize] extra — the Local summarizer's offline backend (mlx_lm on
#     Apple Silicon, llama_cpp elsewhere; the latter needs the maintainer's
#     prebuilt wheel index because it builds from source by default).
#   * CUDA torch — pip's default `torch` wheel is CPU-only on Windows (the Linux
#     wheel bundles CUDA), so on an NVIDIA box the GPU goes unused: the probe
#     reports "Available backends: ['cpu']" and whisperlivekit's warmup can't
#     load cublas64_12.dll. No-op without nvidia-smi or when torch is already a
#     CUDA build. Force one channel with $env:TAPSCRIBE_TORCH_CUDA (e.g. cu128);
#     skip with $env:TAPSCRIBE_NO_CUDA_TORCH=1.
#
# These used to be inlined here and, near-identically, in start.sh. They now
# live in `tapscribe.preflight` so the Windows Bundle's Launcher — which has no
# start.ps1 to inherit them from — runs the SAME steps rather than a C#
# reimplementation that drifts (ADR-0015). `plan_steps` is pure and unit-tested;
# `--dry-run` prints what it would do.
#
# `-m` works even in a venv where tapscribe isn't installed yet: we Set-Location
# to $PSScriptRoot above, and `-m` puts the cwd on sys.path. Non-fatal by
# design — every step degrades a feature, so a failure warns and the recorder
# still boots.
& python -m tapscribe.preflight

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
if ($AutoLive) { $ExtraArgs += "--auto-live" }
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
if ($BrowserSetup) {
    Write-Host "        Choose models   http://${BindHost}:${PortRec}/setup  (open this first — no models installed yet)"
}
Write-Host "        Live channel    $LiveLabel"
Write-Host "        (first launch can take 10–30s while torch/transformers import — be patient)"
Write-Host ""

& python -u -m tapscribe `
    --host $BindHost `
    --port $PortRec `
    --live-model $Model `
    --live-language $LangCode `
    @ExtraArgs
