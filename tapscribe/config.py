"""Boot-time constants — paths, security flags, tuned thresholds.

Runtime mutable state (current session, recording enabled, MLX preference,
auth password, live channel handle, active streams, in-flight jobs, live
transcripts feed) lives on the `Recorder` instance — see
`tapscribe.recorder`. This module only holds values that are read across
the codebase and don't change after boot.
"""

from __future__ import annotations

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

# Top-level dirs are created lazily on first use rather than at import
# time, so unit tests and offline tooling don't litter the worktree
# with empty `recordings/` / `config/` folders. The /record WebSocket
# handler mkdirs session_dir with parents=True, which creates
# RECORDINGS_DIR if missing.

# ---------------------------------------------------------------------------
# Security thresholds + flags (boot-time)
# ---------------------------------------------------------------------------

# Whole-file RMS below this dBFS is treated as "no sustained signal"; the
# transcribe path refuses to run Whisper on it (Whisper otherwise
# hallucinates YouTube-subtitle text on near-silent audio).
SILENT_RMS_DBFS_FLOOR: float = -50.0

# HTTP Basic auth. Username is fixed; password lives on the Recorder
# (`recorder.auth.password`) and is generated/persisted under
# AUTH_PASSWORD_FILE on first run.
AUTH_USER: str = "admin"
AUTH_PASSWORD_FILE: Path = BASE_DIR / ".auth-password"
AUTH_ENABLED: bool = True

# Bearer token bridges send on the /tap WebSocket (carried via
# Sec-WebSocket-Protocol). Distinct from the dashboard password so the
# operator can hand a tap-token to browser extensions without exposing
# the dashboard. Generated/persisted under TAP_TOKEN_FILE on first run.
TAP_TOKEN_FILE: Path = BASE_DIR / ".tap-token"

# TLS files for the dashboard + /tap (when --tls is set). Auto-generated
# as self-signed if missing.
TLS_CERT_FILE: Path = BASE_DIR / ".tapscribe-cert.pem"
TLS_KEY_FILE: Path = BASE_DIR / ".tapscribe-key.pem"

# Method-aware routes that bypass auth. /health is for monitors; the
# live-transcript ingest is exempt because the browser bridge can't
# easily inject Basic credentials on a fire-and-forget POST.
AUTH_EXEMPT_ROUTES = frozenset({
    ("GET", "/health"),
    ("POST", "/api/live-transcript"),
})

# Whether the FastAPI app's lifespan should auto-start the live channel.
# Flipped off by --no-auto-live.
AUTO_START_LIVE: bool = True
