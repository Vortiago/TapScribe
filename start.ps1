# TapScribe — Windows / PowerShell bring-up script.
#
# Usage (from the repo root):
#   .\start.ps1                          # localhost only
#   .\start.ps1 -Lan                     # bind to 0.0.0.0 (LAN access)
#   .\start.ps1 -NoMlx                   # skip MLX (irrelevant on Windows but accepted for parity)
#   .\start.ps1 -NoAutoLive              # boot without starting the live channel
#   .\start.ps1 -NoAuth                  # disable dashboard auth + /tap token gate (DEV ONLY)
#   .\start.ps1 -Tls                     # serve https:// + wss:// (auto self-signed)

[CmdletBinding()]
param(
    [switch]$Lan,
    [switch]$NoMlx,
    [switch]$NoAutoLive,
    [switch]$NoAuth,
    [switch]$Tls
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

# --- Dependencies -----------------------------------------------------------
Write-Host "[start] Checking installed dependencies…"
$needInstall = $false
foreach ($pkg in @("whisperlivekit", "multipart", "fastapi", "uvicorn", "transformers", "cryptography")) {
    & python -c "import $pkg" 2>$null
    if ($LASTEXITCODE -ne 0) { $needInstall = $true }
}

if ($needInstall) {
    Write-Host "[start] Installing dependencies — first run pulls PyTorch (several hundred MB)…"
    & pip install whisperlivekit python-multipart "transformers>=4.46" uvicorn "cryptography>=42"
}

# --- Configuration ----------------------------------------------------------
$Model = if ($env:SX_MODEL) { $env:SX_MODEL } else { "tiny.en" }
$LangCode = if ($env:SX_LANG) { $env:SX_LANG } else { "en" }
$PortRec = if ($env:SX_PORT_REC) { $env:SX_PORT_REC } else { "8001" }
$PortWlk = if ($env:SX_PORT_WLK) { $env:SX_PORT_WLK } else { "8000" }
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

Write-Host ""
Write-Host "[start] Launching TapScribe…"
Write-Host "        Dashboard       http://${BindHost}:${PortRec}/"
Write-Host "        Live channel    ws://${BindHost}:${PortWlk}/asr"
Write-Host "        (first launch can take 10–30s while torch/transformers import — be patient)"
Write-Host ""

& python -u -m tapscribe `
    --host $BindHost `
    --port $PortRec `
    --live-model $Model `
    --live-language $LangCode `
    --live-port $PortWlk `
    @ExtraArgs
