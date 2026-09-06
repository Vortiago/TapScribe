#!/usr/bin/env bash
# TapScribe — one-shot bring-up script.
#
# Usage (from the repo root):
#   bash start.sh                         # localhost only
#   bash start.sh --lan                   # bind to 0.0.0.0 so other machines can connect
#   bash start.sh --no-mlx                # skip MLX even on Apple Silicon (live AND batch)
#   bash start.sh --auto-live         # boot the recorder with live channel auto-started
#   bash start.sh --no-auth               # disable dashboard auth + /tap token gate (DEV ONLY; insecure on LAN)
#   bash start.sh --tls                   # serve https:// + wss:// (auto self-signed if no cert provided)
#   bash start.sh --non-interactive       # install the saved/default selection in-terminal (no browser)
#   SX_MODEL=small.en bash start.sh       # initial live model; changeable from the dashboard
#
# Dashboard auth: a password is generated on first run, persisted to
# .auth-password, and printed to this terminal. Copy it into the browser's
# auth prompt. Use --no-auth only for trusted localhost development.
#
# It will:
#   1. Find a Python 3.12+
#   2. Create a venv at ./.venv if missing
#   3. Pick transcription models in the BROWSER: the recorder serves a setup
#      page at /setup (GET / redirects there until a model is installed) —
#      choose Whisper / Voxtral / Parakeet there. First run base-installs the
#      package only.
#   4. Re-runs re-apply the saved selection (.tapscribe-install.json) via
#      `python -m tapscribe.install_picker --non-interactive`, running pip only when the
#      selection or pyproject.toml changed. On Apple Silicon MLX-flavoured
#      extras are added automatically (`--no-mlx` opts out).
#   5. Launch the TapScribe recorder (port 8001) — which then spawns
#      whisperlivekit-server (port 8000) as a child you can stop/start/reconfigure
#      from the dashboard.
#   6. Stream all logs to this terminal; Ctrl+C stops everything cleanly.
#
# Configurable via env vars:
#   SX_HOST       bind address (default localhost, overridden by --lan to 0.0.0.0)
#   SX_PORT_WLK   WhisperLiveKit port (default: ephemeral — WLK is internal,
#                 only the recorder talks to it; pin only if you have a reason)
#   SX_PORT_REC   recorder port (default 8001)
#   SX_MODEL      Initial live Whisper model (default tiny.en; switch live from dashboard)
#   SX_LANG       language hint (default en)

# `set -u` is intentionally NOT enabled: macOS still ships bash 3.2 by
# default, which treats expansion of an empty array (`"${arr[@]}"`) as an
# unbound variable error.
set -eo pipefail

cd "$(dirname "$0")"

# --- Argument parsing -------------------------------------------------------
LAN=0
NO_MLX=0
AUTO_LIVE=0
NO_AUTH=0
TLS=0
NON_INTERACTIVE=0
for a in "$@"; do
    case "$a" in
        --lan) LAN=1 ;;
        --no-mlx) NO_MLX=1 ;;
        --auto-live) AUTO_LIVE=1 ;;
        --no-auto-live) ;;  # deprecated — off is the default
        --no-auth) NO_AUTH=1 ;;
        --tls) TLS=1 ;;
        --non-interactive) NON_INTERACTIVE=1 ;;
        -h|--help)
            sed -n '2,32p' "$0"
            exit 0
            ;;
        *) echo "[start] Unknown argument: $a"; exit 1 ;;
    esac
done

# --- Apple Silicon / MLX detection ------------------------------------------
USE_MLX=0
OS_NAME=$(uname -s 2>/dev/null || echo unknown)
ARCH=$(uname -m 2>/dev/null || echo unknown)
if [ "$OS_NAME" = "Darwin" ] && [ "$ARCH" = "arm64" ] && [ "$NO_MLX" -eq 0 ]; then
    USE_MLX=1
    echo "[start] Apple Silicon detected ($OS_NAME/$ARCH). MLX will be used for"
    echo "        BOTH the live channel (~50% faster than CPU faster-whisper)"
    echo "        AND batch Transcribe jobs (~3-5x faster than CPU faster-whisper)."
elif [ "$OS_NAME" = "Darwin" ] && [ "$ARCH" = "arm64" ] && [ "$NO_MLX" -eq 1 ]; then
    echo "[start] --no-mlx specified; using CPU backend (live + batch) on Apple Silicon."
elif [ "$OS_NAME" = "Darwin" ] && [ "$ARCH" = "x86_64" ]; then
    echo "[start] Intel Mac detected; MLX is Apple Silicon only, using CPU backend."
fi

# --- Python detection -------------------------------------------------------
# TapScribe requires Python 3.12+. 3.10/3.11 were dropped after 3.10's
# EOL window closed; 3.12 matches Ubuntu 24.04 LTS's default `python3`.
PY=""
for cand in python3.13 python3.12 python3; do
    if command -v "$cand" >/dev/null 2>&1; then
        ver=$("$cand" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo "0.0")
        major=${ver%.*}
        minor=${ver#*.}
        if [ "$major" -ge 4 ] 2>/dev/null || { [ "$major" -eq 3 ] && [ "$minor" -ge 12 ]; } 2>/dev/null; then
            PY="$cand"
            break
        fi
    fi
done

if [ -z "$PY" ]; then
    cat >&2 <<EOF
[start] No Python 3.12+ found on PATH.

On macOS the easiest fix is Homebrew:
  brew install python@3.13

On Ubuntu 22.04 (which ships 3.10):
  sudo apt install python3.12 python3.12-venv

On Ubuntu 24.04+ python3 is already 3.12.

Then re-run this script.
EOF
    exit 1
fi

echo "[start] Using $PY ($("$PY" --version 2>&1))"

# --- Virtual env ------------------------------------------------------------
if [ ! -f .venv/bin/activate ]; then
    if [ -d .venv ]; then
        echo "[start] Existing .venv is incomplete or from a different OS; removing it."
        rm -rf .venv
    fi
    echo "[start] Creating venv at .venv"
    if ! "$PY" -m venv .venv; then
        cat >&2 <<EOF
[start] '$PY -m venv .venv' failed. On macOS the usual fix is:
          brew install python@3.13
        then re-run this script.
EOF
        exit 1
    fi
fi
# shellcheck disable=SC1091
source .venv/bin/activate

python -m pip install --quiet --upgrade pip

# --- Model install ----------------------------------------------------------
# Models are chosen in the BROWSER at /setup (the recorder redirects there until
# a model is installed). This script only makes the package importable so the
# recorder can boot:
#   * First run, interactive: base-install the package; pick models at /setup.
#   * Re-run, or --non-interactive: re-apply the saved selection via
#     `install_picker --non-interactive` (pip runs only on a changed
#     selection / pyproject). To pick models in the terminal, run
#     `python -m tapscribe.install_picker` directly.
BROWSER_SETUP=0
if [ ! -f .tapscribe-install.json ] && [ "$NON_INTERACTIVE" -eq 0 ]; then
    echo "[start] First run — installing the base package; you'll choose models in the browser."
    if ! python -m pip install -e .; then
        echo "[start] base 'pip install -e .' failed; aborting." >&2
        exit 1
    fi
    BROWSER_SETUP=1
else
    # Always non-interactive: re-apply the saved selection with no prompt.
    # `--no-mlx` propagates so the picker also skips the MLX-flavoured extras.
    PICKER_ARGS=(--non-interactive)
    if [ "$NO_MLX" -eq 1 ]; then
        PICKER_ARGS+=(--no-mlx)
    fi
    if ! python -m tapscribe.install_picker "${PICKER_ARGS[@]}"; then
        echo "[start] install picker failed; aborting." >&2
        exit 1
    fi
fi

# --- Runtime python deps ----------------------------------------------------
# Probe-then-repair for the runtime deps the install picker does NOT cover:
# onnxruntime (the backend for the vendored Silero model behind `tapscribe.vad`,
# i.e. the per-tap silence gate; a core dependency, so this only repairs an
# incomplete venv) and the `[summarize]` extra (the Local
# summarizer's offline backend — mlx_lm on Apple Silicon, llama_cpp elsewhere,
# the latter needing the maintainer's prebuilt wheel index because it builds
# from source by default).
#
# These used to be inlined here and, near-identically, in start.ps1. They now
# live in `tapscribe.preflight` so the Windows Bundle's tray — which has no
# start.ps1 to inherit them from — runs the SAME steps rather than a C#
# reimplementation that drifts (ADR-0015). `plan_steps` is pure and unit-tested;
# `--dry-run` prints what it would do.
#
# `-m` works even in a venv where tapscribe isn't installed yet: we cd'd to the
# repo root above, and `-m` puts the cwd on sys.path. Non-fatal by design —
# every step here degrades a feature, so a failure warns and the recorder still
# boots.
#
# (No ffmpeg branch: the array-accepting backends — mlx-whisper, parakeet-mlx,
# and the transformers Parakeet path — pre-decode the recorder's WAV via
# `tapscribe.wav_predecode.load_recorder_wav_as_pcm` and hand the model a numpy
# array, skipping the ffmpeg-shelling loaders upstream would otherwise use.
# There is no ffmpeg fallback; non-recorder WAVs raise a clear "convert the
# file" error at request time.)
python -m tapscribe.preflight || true

# --- Configuration ----------------------------------------------------------
MODEL="${SX_MODEL:-tiny.en}"
LANG="${SX_LANG:-en}"
PORT_WLK="${SX_PORT_WLK:-}"
PORT_REC="${SX_PORT_REC:-8001}"

if [ "$LAN" -eq 1 ]; then
    HOST="${SX_HOST:-0.0.0.0}"
else
    HOST="${SX_HOST:-localhost}"
fi

LAN_IP=""
if command -v ipconfig >/dev/null 2>&1; then
    LAN_IP=$(ipconfig getifaddr en0 2>/dev/null || true)
    if [ -z "$LAN_IP" ]; then
        LAN_IP=$(ipconfig getifaddr en1 2>/dev/null || true)
    fi
fi
if [ -z "$LAN_IP" ] && command -v hostname >/dev/null 2>&1; then
    LAN_IP=$(hostname 2>/dev/null || true)
fi

# --- Recorder CLI flags -----------------------------------------------------
EXTRA_ARGS=()
if [ "$NO_MLX" -eq 1 ]; then
    EXTRA_ARGS+=(--no-mlx)
fi
if [ "$AUTO_LIVE" -eq 1 ]; then
    EXTRA_ARGS+=(--auto-live)
fi
if [ "$NO_AUTH" -eq 1 ]; then
    EXTRA_ARGS+=(--no-auth)
fi
if [ "$TLS" -eq 1 ]; then
    EXTRA_ARGS+=(--tls)
fi

BACKEND_LABEL="faster-whisper (CPU)"
if [ "$USE_MLX" -eq 1 ]; then
    BACKEND_LABEL="mlx-whisper (Apple Silicon GPU)"
fi

# --- Launch -----------------------------------------------------------------
echo ""
if [ -n "$PORT_WLK" ]; then
    EXTRA_ARGS+=(--live-port "$PORT_WLK")
    LIVE_LABEL="ws://$HOST:$PORT_WLK/asr  (managed from dashboard)"
else
    LIVE_LABEL="ephemeral (internal; recorder chooses a free port each start)"
fi

echo "[start] Launching TapScribe (which will spawn whisperlivekit-server as a child)..."
echo "        Dashboard       http://$HOST:$PORT_REC/"
if [ "$BROWSER_SETUP" -eq 1 ]; then
    echo "        Choose models   http://$HOST:$PORT_REC/setup  (open this first — no models installed yet)"
fi
echo "        Live channel    $LIVE_LABEL"
echo "        Backend         $BACKEND_LABEL"
echo "        Initial model   $MODEL  (lang=$LANG; change from the dashboard or via SX_MODEL=…)"
echo ""

python -m tapscribe \
    --host "$HOST" \
    --port "$PORT_REC" \
    --live-model "$MODEL" \
    --live-language "$LANG" \
    "${EXTRA_ARGS[@]}" &
REC_PID=$!

cleanup() {
    echo ""
    echo "[start] Stopping recorder (its lifespan shutdown kills the whisperlivekit-server child)..."
    kill "$REC_PID" 2>/dev/null || true
    wait 2>/dev/null || true
    echo "[start] Done."
}
trap cleanup INT TERM EXIT

echo "[start] Recorder running. Press Ctrl+C to stop."
if [ "$LAN" -eq 1 ]; then
    echo ""
    echo "[start] LAN mode is ON. Point the spacialchat-bridge extension's"
    echo "        \"Backend host\" field at this machine. Likely value:"
    if [ -n "$LAN_IP" ]; then
        echo "          $LAN_IP"
    else
        echo "          (could not auto-detect; check System Settings → Network)"
    fi
    echo ""
    echo "[start] Make sure port $PORT_REC is reachable through your firewall."
    echo "        (WhisperLiveKit binds an internal-only loopback port; bridges"
    echo "        and dashboard clients only ever talk to the recorder.)"
fi
echo ""

# Block until Ctrl+C or until the recorder exits.
while kill -0 "$REC_PID" 2>/dev/null; do
    sleep 1
done

echo "[start] Recorder exited. Scroll up for its last log lines (and any [wlk] lines from its whisperlivekit-server child)."
