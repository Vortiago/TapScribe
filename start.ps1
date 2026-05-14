# TapScribe — Windows / PowerShell bring-up script.
#
# Usage (from the repo root):
#   .\start.ps1                          # localhost only
#   .\start.ps1 -Lan                     # bind to 0.0.0.0 (LAN access)
#   .\start.ps1 -NoMlx                   # skip MLX (irrelevant on Windows but accepted for parity)
#   .\start.ps1 -NoAutoLive              # boot without starting the live channel
#   .\start.ps1 -NoAuth                  # disable dashboard auth (DEV ONLY)

[CmdletBinding()]
param(
    [switch]$Lan,
    [switch]$NoMlx,
    [switch]$NoAutoLive,
    [switch]$NoAuth
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
python -m pip install --quiet --upgrade pip

# --- Dependencies -----------------------------------------------------------
$needInstall = $false
foreach ($pkg in @("whisperlivekit", "multipart", "fastapi", "uvicorn")) {
    & python -c "import $pkg" *> $null
    if ($LASTEXITCODE -ne 0) { $needInstall = $true }
}
& python -c "from transformers import VoxtralForConditionalGeneration" *> $null
if ($LASTEXITCODE -ne 0) { $needInstall = $true }

if ($needInstall) {
    Write-Host "[start] Installing dependencies — first run pulls PyTorch (several hundred MB)…"
    & pip install whisperlivekit python-multipart "transformers>=4.46" uvicorn
}

# --- Configuration ----------------------------------------------------------
$Model = if ($env:SX_MODEL) { $env:SX_MODEL } else { "tiny.en" }
$LangCode = if ($env:SX_LANG) { $env:SX_LANG } else { "en" }
$PortRec = if ($env:SX_PORT_REC) { $env:SX_PORT_REC } else { "8001" }
$PortWlk = if ($env:SX_PORT_WLK) { $env:SX_PORT_WLK } else { "8000" }
$BindHost = if ($Lan) { (if ($env:SX_HOST) { $env:SX_HOST } else { "0.0.0.0" }) } else { (if ($env:SX_HOST) { $env:SX_HOST } else { "localhost" }) }

$ExtraArgs = @()
if ($NoMlx)      { $ExtraArgs += "--no-mlx" }
if ($NoAutoLive) { $ExtraArgs += "--no-auto-live" }
if ($NoAuth)     { $ExtraArgs += "--no-auth" }

Write-Host ""
Write-Host "[start] Launching TapScribe…"
Write-Host "        Dashboard       http://${BindHost}:${PortRec}/"
Write-Host "        Live channel    ws://${BindHost}:${PortWlk}/asr"
Write-Host ""

& python -m tapscribe `
    --host $BindHost `
    --port $PortRec `
    --live-model $Model `
    --live-language $LangCode `
    --live-port $PortWlk `
    @ExtraArgs
