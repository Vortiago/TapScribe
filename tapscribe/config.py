"""Boot-time constants — paths, security flags, tuned thresholds.

Runtime mutable state (current session, recording enabled, MLX preference,
auth password, live channel handle, active streams, in-flight jobs, live
transcripts feed) lives on the `Recorder` instance — see
`tapscribe.recorder`. This module only holds values that are read across
the codebase and don't change after boot.
"""

from __future__ import annotations

import os
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

# BASE_DIR defaults to the repo root (one level up from the tapscribe/
# package) for local dev. Operators running from a pip-installed package
# (e.g. Docker, systemd) override it via TAPSCRIBE_BASE_DIR so recordings,
# config, and auth/tls files land in a writable persistent location
# instead of inside site-packages.
_DEFAULT_BASE_DIR = Path(__file__).resolve().parent.parent
BASE_DIR: Path = Path(os.environ.get("TAPSCRIBE_BASE_DIR") or _DEFAULT_BASE_DIR)
RECORDINGS_DIR: Path = BASE_DIR / "recordings"
CONFIG_DIR: Path = BASE_DIR / "config"
# WEB_DIR is always inside the package — it ships with the wheel and is
# never operator-mutable, so it doesn't follow BASE_DIR.
WEB_DIR: Path = Path(__file__).resolve().parent / "web"

# Config files. These can be edited at runtime; the recorder re-reads each
# one per transcribe job so changes take effect without a restart.
PROMPT_FILE: Path = CONFIG_DIR / "prompt.txt"
# Independent prompt for the live channel (whisperlivekit-server).
# Operators set this separately from the batch prompt because live and
# batch typically run different models and/or different cadences (a
# terse always-on prompt for live vs a meeting-specific one for batch).
# Empty/missing = the live channel runs without an --init-prompt.
LIVE_PROMPT_FILE: Path = CONFIG_DIR / "live-prompt.txt"
# Operator's DEFAULT live-channel model id (a single model_id, e.g. "tiny.en").
# Separate from the running channel's model: the dashboard's Live engine card
# persists the default here, and the live channel only picks it up on (re)start
# — so the UI can show "restart to apply" when this differs from what's running.
# Empty/missing = no saved default (the live channel uses its boot/auto model).
LIVE_MODEL_FILE: Path = CONFIG_DIR / "live-model.txt"
# Operator's DEFAULT batch model id (a single model_id, e.g. "small.en") — the
# live-model's batch twin. The dashboard's Default engine card persists it
# here; the end-of-meeting pipeline resolves its transcribe stage from it so
# the tap-token trigger never carries a model field. Empty/missing = the
# bundled default (transcribers.catalog.DEFAULT_BATCH_MODEL).
BATCH_MODEL_FILE: Path = CONFIG_DIR / "batch-model.txt"
# Operator's DEFAULT summarizer config (#84) — ONE structured JSON object
# {source, prompt, command, model, max_tokens}, unlike the single-value text
# configs above. The Summary view and the end-of-meeting pipeline's summarize
# stage both resolve from it (under any per-session override). Missing or
# unparseable = all-empty (built-in defaults apply downstream).
SUMMARIZER_CONFIG_FILE: Path = CONFIG_DIR / "summarizer.json"
HOTWORDS_FILE: Path = CONFIG_DIR / "hotwords.txt"
HALLUCINATIONS_FILE: Path = CONFIG_DIR / "hallucinations.txt"
# Operator's DEFAULT candidate-language set (ADR-0009) — comma-separated ISO
# codes (e.g. "da,no,en"). The end-of-meeting pipeline and transcribe_session
# resolve the per-region language run from it (under any per-session override).
# Empty/missing = the bundled catch-all default
# (transcribers.catalog.DEFAULT_CANDIDATE_LANGUAGES).
LANGUAGES_FILE: Path = CONFIG_DIR / "languages.txt"

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
# (`recorder.auth.value`) and is generated/persisted under
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

# PUBLIC scheme — exact (method, path) routes that bypass auth entirely.
# Health probes for monitors (/healthz is the richer probe shape); no
# credential. The Bridge's /api/tap/* control plane is NOT listed here —
# it is the TAP-BEARER scheme (TAP_PREFIX below), enforced by the auth
# middleware, not exempt from auth.
AUTH_EXEMPT_ROUTES = frozenset(
    {
        ("GET", "/health"),
        ("GET", "/healthz"),
    }
)

# TAP-BEARER scheme — the prefix of the Bridge's HTTP control plane
# (POST /api/tap/new-session, POST/GET /api/tap/sessions/{session}/pipeline).
# The auth middleware routes every path under TAP_PREFIX past dashboard Basic
# auth AND enforces the tap bearer (auth.check_tap_bearer) in ONE predicate,
# so "exempt from Basic" and "requires the bearer" cannot drift apart — a
# bearer-less /api/tap/* route is impossible by construction and handlers
# carry no gate of their own. See CONTEXT.md "HTTP auth gate · auth schemes"
# and ADR-0008.
TAP_PREFIX: str = "/api/tap"

# Whether the FastAPI app's lifespan should auto-start the live channel.
# Flipped off by --no-auto-live.
AUTO_START_LIVE: bool = True


# ---------------------------------------------------------------------------
# Env helpers — shared parsers for operator-tunable numeric knobs
# ---------------------------------------------------------------------------


def env_float(
    name: str,
    default: float,
    *,
    min_value: float | None = None,
    max_value: float | None = None,
) -> float:
    """Read `name` as a float from the environment, falling back to
    `default` when unset, unparseable, or out of the optional
    `(min_value, max_value)` range. Every reject case logs once with
    the actual value and the bound that tripped — typo-tolerant rather
    than fatal so one bad env var doesn't take down the recorder, but
    loud enough that the operator sees the bound on next boot.

    Callers should always pass bounds for operator-tunable knobs.
    Unbounded values are an attack surface: a `TAPSCRIBE_*_CHUNK_S=-5`
    would silently land at the consumer, where downstream `int()` /
    `max(1, ...)` clamps may convert it into a pathological setting
    instead of an obvious mistake.
    """
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        v = float(raw)
    except ValueError:
        print(f"[tapscribe] ignoring unparseable {name}={raw!r}; using default {default}", flush=True)
        return default
    if min_value is not None and v < min_value:
        print(
            f"[tapscribe] ignoring {name}={v} < min {min_value}; using default {default}",
            flush=True,
        )
        return default
    if max_value is not None and v > max_value:
        print(
            f"[tapscribe] ignoring {name}={v} > max {max_value}; using default {default}",
            flush=True,
        )
        return default
    return v


def env_int(
    name: str,
    default: int,
    *,
    min_value: int | None = None,
    max_value: int | None = None,
) -> int:
    """Integer counterpart to `env_float`."""
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        v = int(raw)
    except ValueError:
        print(f"[tapscribe] ignoring unparseable {name}={raw!r}; using default {default}", flush=True)
        return default
    if min_value is not None and v < min_value:
        print(
            f"[tapscribe] ignoring {name}={v} < min {min_value}; using default {default}",
            flush=True,
        )
        return default
    if max_value is not None and v > max_value:
        print(
            f"[tapscribe] ignoring {name}={v} > max {max_value}; using default {default}",
            flush=True,
        )
        return default
    return v
