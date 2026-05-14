"""Module-level constants, paths, and feature detection for TapScribe.

Everything here is import-time state; mutating from elsewhere is intentional
for a handful of operator-controlled toggles (RECORDING_ENABLED, USE_MLX,
AUTH_ENABLED, AUTO_START_LIVE) flipped from CLI flags in `__main__.py`.
"""

from __future__ import annotations

import os
import platform
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

# BASE_DIR is the repo root (one level up from the tapscribe/ package).
BASE_DIR: Path = Path(__file__).resolve().parent.parent
RECORDINGS_DIR: Path = BASE_DIR / "recordings"
CONFIG_DIR: Path = BASE_DIR / "config"
WEB_DIR: Path = Path(__file__).resolve().parent / "web"

# Config files. These can be edited at runtime; the recorder re-reads each
# one per transcribe job so changes take effect without a restart.
PROMPT_FILE: Path = CONFIG_DIR / "prompt.txt"
HOTWORDS_FILE: Path = CONFIG_DIR / "hotwords.txt"
HALLUCINATIONS_FILE: Path = CONFIG_DIR / "hallucinations.txt"

# Top-level dirs are created lazily on first use rather than at import time,
# so unit tests and offline tooling don't litter the worktree with empty
# `recordings/` / `config/` folders. The /record WebSocket handler mkdirs
# SESSION_DIR with parents=True, which creates RECORDINGS_DIR if missing.

# ---------------------------------------------------------------------------
# Session — fixed at boot; rotated via /api/new-session
# ---------------------------------------------------------------------------

SESSION_START: str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
SESSION_DIR: Path = RECORDINGS_DIR / SESSION_START


def rotate_session() -> tuple[str, str]:
    """Rotate the current session folder. Returns (previous, current).
    Module-level SESSION_START / SESSION_DIR are updated in place; readers
    elsewhere in the package should always access via the `config` module
    so they pick up the new value."""
    global SESSION_START, SESSION_DIR
    prev = SESSION_START
    SESSION_START = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    SESSION_DIR = RECORDINGS_DIR / SESSION_START
    return prev, SESSION_START

# ---------------------------------------------------------------------------
# Operator toggles
# ---------------------------------------------------------------------------

# When False, /record WSes are accepted then immediately closed. Live
# transcripts still flow via WhisperLiveKit + the bridge extension's
# POST /api/live-transcript independently of this flag.
RECORDING_ENABLED: bool = True

# Whole-file RMS below this dBFS is treated as no sustained signal; the
# transcribe path refuses to run Whisper on it (Whisper otherwise
# hallucinates YouTube-subtitle text on near-silent audio).
SILENT_RMS_DBFS_FLOOR: float = -50.0

# Auth — see tapscribe.auth for details.
AUTH_USER: str = "admin"
AUTH_PASSWORD_FILE: Path = BASE_DIR / ".auth-password"
AUTH_ENABLED: bool = True

# Method-aware routes that bypass auth. /health is for monitors; the live-
# transcript ingest is exempt because the browser extension can't easily
# inject Basic credentials on a fire-and-forget POST.
AUTH_EXEMPT_ROUTES = frozenset({
    ("GET", "/health"),
    ("POST", "/api/live-transcript"),
})

# Auto-start the live channel (whisperlivekit-server) on boot. Flipped off
# by --no-auto-live.
AUTO_START_LIVE: bool = True


def _detect_mlx() -> bool:
    """True on Apple Silicon with mlx-whisper importable, unless explicitly
    disabled via the SX_NO_MLX=1 env var. Applies to BOTH the batch transcribe
    path AND the live channel (WhisperLiveKit is spawned with
    --backend mlx-whisper when this is True)."""
    if os.environ.get("SX_NO_MLX") == "1":
        return False
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        return False
    try:
        import mlx_whisper  # noqa: F401
        return True
    except ImportError:
        return False


USE_MLX: bool = _detect_mlx()
