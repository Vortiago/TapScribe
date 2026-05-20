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
# operator for which model families (Whisper / Voxtral / Parakeet /
# Canary) to install, pre-checks the saved selection so re-runs are one
# keystroke, then runs `pip install -e ".[…]"` for the resolved extras.
$PickerArgs = @()
if ($NoMlx)           { $PickerArgs += "--no-mlx" }
if ($NonInteractive)  { $PickerArgs += "--non-interactive" }
& python tools\install_picker.py @PickerArgs
if ($LASTEXITCODE -ne 0) {
    Write-Error "[start] install picker failed; aborting."
    exit 1
}

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
