"""Boot-time constants — paths, security flags, tuned thresholds.

Runtime mutable state (current session, recording enabled, MLX preference,
auth password, live channel handle, active streams, in-flight jobs, live
transcripts feed) lives on the `Recorder` instance — see
`tapscribe.recorder`. This module only holds values that are read across
the codebase and don't change after boot.
"""

from __future__ import annotations

import math
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

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
# Operator's DEFAULT candidate-language set (ADR-0010) — comma-separated ISO
# codes (e.g. "da,no,en"). The end-of-meeting pipeline and transcribe_session
# resolve the per-region language run from it (under any per-session override).
# Empty/missing = the bundled catch-all default
# (transcribers.catalog.DEFAULT_CANDIDATE_LANGUAGES).
LANGUAGES_FILE: Path = CONFIG_DIR / "languages.txt"
# Operator's model idle-TTL knob: seconds of idle before eviction.
# Dashboard writes here via config-store key `model-idle-ttl`;
# `_idle_ttl_s()` reads it at use-time when the env var is unset.
MODEL_IDLE_TTL_FILE: Path = CONFIG_DIR / "model-idle-ttl.txt"

# The idle-TTL knob's four siblings (#210), same shape: the dashboard writes the
# file via config-store, the use-time resolver reads it under the matching env
# var (`resolve_knob`).
PARAKEET_CHUNK_S_FILE: Path = CONFIG_DIR / "parakeet-chunk-s.txt"
PARAKEET_OVERLAP_S_FILE: Path = CONFIG_DIR / "parakeet-overlap-s.txt"
SUMMARIZE_TIMEOUT_S_FILE: Path = CONFIG_DIR / "summarize-timeout-s.txt"
SUMMARIZE_GGUF_CTX_FILE: Path = CONFIG_DIR / "summarize-gguf-ctx.txt"
DIARIZE_THRESHOLD_FILE: Path = CONFIG_DIR / "diarize-threshold.txt"
DIARIZE_MAX_SPEAKERS_FILE: Path = CONFIG_DIR / "diarize-max-speakers.txt"

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

# Sidecar the install picker writes when it had to SKIP a model family whose
# saved backend left the catalog. The filename is MIRRORED from
# `install_picker.WARNINGS_FILENAME` rather than imported: the app must never
# import the dependency-free picker (it runs before the package's deps exist).
# `test_setup_state.test_warnings_filename_matches_the_picker` pins the mirror.
INSTALL_WARNINGS_FILE: Path = BASE_DIR / ".tapscribe-install-warnings.json"
AUTH_ENABLED: bool = True

# What the Recorder is actually bound to, stamped once by `__main__.main()` from
# the parsed CLI args. Both exist because the dashboard SESSION COOKIE needs them
# and nothing else could answer:
#   - the cookie's NAME carries the port, because cookies are scoped by host but
#     NOT by port, so a checkout on 8001 and a second Recorder on 8002 would
#     otherwise clobber each other's session. `request.url.port` would also
#     answer, but it comes from the Host header; the bound port is the honest
#     source.
#   - `Secure` may only be set when the connection actually is TLS, and `--tls`
#     otherwise resolves to a local in `main()` that reaches nothing importable.
PORT: int = 8001
TLS_ENABLED: bool = False


def session_cookie_name() -> str:
    """The dashboard session cookie's name, which carries the port.

    Here rather than beside the route that sets it, for two reasons. It is read on
    the hottest path in the app — `auth.basic_auth_middleware`, crossed by the
    dashboard's 500 ms poll — and core reaching into `routes/` for it meant a
    deferred import re-run on every request. And this module is the leaf both
    sides already import, so one owner costs no new dependency edge: a middleware
    that spelled the name differently from the route would silently stop accepting
    the sessions that route mints, which reads exactly like "the login link is
    broken".

    A function, not a constant: `PORT` is stamped by `__main__.main()` after this
    module is imported.
    """
    return f"tapscribe_session_{PORT}"


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
# `GET /login` is here because it AUTHENTICATES BY SPENDING its token: the
# credential is in the URL, so demanding a second one first would defeat the
# link (ADR-0023). Exact (method, path), like the health probes — a query-param
# carve-out on `/` would muddy the one predicate ADR-0008 exists to protect.
AUTH_EXEMPT_ROUTES = frozenset(
    {
        ("GET", "/health"),
        ("GET", "/healthz"),
        ("GET", "/login"),
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
# Opt-in via --auto-live (default is off to avoid loading a live model
# and spending memory when nobody wants captions).
AUTO_START_LIVE: bool = False

# Canonical repo slug used to compose GitHub Release download URLs for the
# dashboard's "Get a bridge" card (`GET /api/bridges` builds
# https://github.com/{GITHUB_REPO}/releases/latest/download/<asset>). ONE
# source of truth so the UI stays dumb — see ADR-0012.
GITHUB_REPO: str = "Vortiago/TapScribe"


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
    if not math.isfinite(v):
        # `float("nan")` parses fine and then passes BOTH bounds below, because
        # `nan < min` and `nan > max` are each False — so it reaches the
        # consumer, where it is no longer typo-tolerant: chunk_windows dies on
        # `int(nan * 16000)` at transcribe time, and `subprocess.run(timeout=nan)`
        # never fires, wedging a summarize job forever. Same reject as
        # `_parse_bounded_ttl` below, and for the same reason.
        print(f"[tapscribe] ignoring non-finite {name}={raw!r}; using default {default}", flush=True)
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


# Bounds for the model idle-TTL knob (MODEL_IDLE_TTL_FILE / env ENV_IDLE_TTL_S,
# both resolved in tapscribe.transcribers). Negative is the "never evict"
# sentinel (floored at -1); the upper bound is a day, far past any sane
# keep-warm window. Out-of-range and non-finite values are rejected by
# `_parse_bounded_ttl` below.
_IDLE_TTL_BOUNDS = (-1.0, 86_400.0)


def _parse_bounded_ttl(raw: str) -> float | None:
    """Parse an idle-TTL string to a finite float within `_IDLE_TTL_BOUNDS`,
    or None when it is empty, unparseable, non-finite (NaN/inf), or out of
    range. The single source of truth shared by the config-file branch of
    `transcribers._idle_ttl_s()` (read-time) and `config_store._check_idle_ttl`
    (write-time) so the two can never diverge on the same input. Lives here
    beside the sibling numeric-knob parsers (`env_float`/`env_int`) and the
    knob's file path (`MODEL_IDLE_TTL_FILE`) so both callers import it plainly
    instead of reaching into the heavy transcribers package. The idle-TTL
    specialisation of `_parse_bounded_knob` — every knob parses through that one
    body, which carries the reasoning for the explicit NaN/inf reject."""
    return _parse_bounded_knob(raw, *_IDLE_TTL_BOUNDS)


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


# ---------------------------------------------------------------------------
# Shared bounded-knob parser — one implementation, one thin caller per knob
# ---------------------------------------------------------------------------


def _parse_bounded_knob(raw: str, lo: float, hi: float, *, cast: Callable[[str], Any] = float) -> Any:
    """Parse `raw` with `cast`, returning None when it is empty, unparseable,
    non-finite (NaN/inf), or outside `[lo, hi]` — the shape every operator knob
    wants: a bad value is not fatal, it just isn't an override. The single
    source of truth shared by the config-store validators (write-time) and the
    use-time resolvers (`resolve_knob`), so write acceptance and
    resolution can never diverge on the same input. Returns `cast`'s type.

    The explicit `isfinite` makes the NaN/inf reject intent unmistakable —
    `lo <= NaN <= hi` is silently False, so a range check alone would drop NaN
    for the wrong-looking reason, and NaN reaching a consumer is where
    typo-tolerance ends (`int(nan * 16000)` dies at transcribe time,
    `subprocess.run(timeout=nan)` never fires)."""
    raw = raw.strip()
    if not raw:
        return None
    try:
        v = cast(raw)
    except ValueError:
        return None
    if cast is float and not math.isfinite(v):
        return None
    return v if lo <= v <= hi else None


# Windows longer than 600 s build the giant activation tensor the chunking
# exists to avoid; an overlap over a minute is a re-transcribe, not a stitch
# seam. The joint `overlap <= 0.9 × chunk` rule these two can still violate as a
# PAIR is enforced separately, at adapter construction (`_chunked.clamp_overlap`).
_PARAKEET_CHUNK_S_BOUNDS = (1.0, 600.0)
_PARAKEET_OVERLAP_S_BOUNDS = (0.0, 60.0)
# A summarize is one short subprocess call; bound the timeout between 1 s and an
# hour so a typo can't wedge a job forever or fail a slow local model instantly.
_SUMMARIZE_TIMEOUT_S_BOUNDS = (1.0, 3600.0)
# GGUF n_ctx: under 512 tokens no transcript fits, and the ceiling is bounded by
# host RAM long before it is by the format.
_SUMMARIZE_GGUF_CTX_BOUNDS = (512, 131_072)
# Voice-clustering cut, a cosine DISTANCE: larger merges more, so it errs
# towards too few Voices. Below 0.3 a speaker splits on her own loudest and
# quietest window; at 1.0 every pair is closer than the cut and a tap is one
# Voice. The measured plateau is 0.6-0.85 (tapscribe/diarizers/standalone.py).
_DIARIZE_THRESHOLD_BOUNDS = (0.3, 1.0)
# A hard cap on Voices per tap. One is "don't split this tap"; past a couple of
# dozen the operator is mapping noise, not people.
_DIARIZE_MAX_SPEAKERS_BOUNDS = (1, 24)


def _parse_parakeet_chunk(raw: str) -> float | None:
    return _parse_bounded_knob(raw, *_PARAKEET_CHUNK_S_BOUNDS)


def _parse_parakeet_overlap(raw: str) -> float | None:
    return _parse_bounded_knob(raw, *_PARAKEET_OVERLAP_S_BOUNDS)


def _parse_summarize_timeout(raw: str) -> float | None:
    return _parse_bounded_knob(raw, *_SUMMARIZE_TIMEOUT_S_BOUNDS)


def _parse_summarize_gguf_ctx(raw: str) -> int | None:
    return _parse_bounded_knob(raw, *_SUMMARIZE_GGUF_CTX_BOUNDS, cast=int)


def _parse_diarize_threshold(raw: str) -> float | None:
    return _parse_bounded_knob(raw, *_DIARIZE_THRESHOLD_BOUNDS)


def _parse_diarize_max_speakers(raw: str) -> int | None:
    return _parse_bounded_knob(raw, *_DIARIZE_MAX_SPEAKERS_BOUNDS, cast=int)


# ---------------------------------------------------------------------------
# Operator-knob resolution (use-time). Lives HERE, in the leaf, next to the
# bounds and parsers it belongs to: every knob consumer (transcribers/_chunked,
# summarizers/catalog, summarizers/command) needs it, and `config_store` imports
# summarizers.catalog back for its validators — so hosting the resolver there
# made those consumers close a real import cycle (CodeQL py/cyclic-import).
# `config_store` re-exports both names, so its public surface is unchanged.
# ---------------------------------------------------------------------------


def read_text_file(path: Path) -> str:
    """Read a small text config file. Returns "" on any failure so callers
    can treat "missing" and "unreadable" identically.

    UnicodeDecodeError is treated the same way: a config file written in
    a non-UTF-8 encoding (e.g. Windows-1252 from Notepad) reads as empty
    rather than raising into every transcribe job. The operator's file
    is effectively "no rules" until they re-save it as UTF-8.
    """
    try:
        return path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""
    except (OSError, UnicodeDecodeError):
        return ""


_WARNED_ENV: dict[str, str] = {}


def resolve_knob(env_name: str, file_path: Path, parse: Callable[[str], Any], default: Any) -> Any:
    """Resolve one operator-tunable knob: a set-AND-VALID env var wins, else the
    dashboard-written config file, else `default`.

    "set AND valid" is the load-bearing half. A set-but-INVALID env var — empty
    (a systemd `EnvironmentFile` leaving `TAPSCRIBE_…=`), non-numeric, or out of
    bounds — is not a real override and falls through to the file exactly like
    an unset one; keying the env branch on mere presence would silently discard
    what the operator chose in the dashboard. It is still an operator MISTAKE,
    so it prints the same one-line `[tapscribe] ignoring …` notice `env_float`
    emits — the resolvers replaced that helper, and losing its notice would make
    a typo'd env var invisible.

    The file is read FRESH (uncached) at use-time: a dashboard edit must apply
    without a restart, and the stat-signature cache behind `read_config` would
    miss a same-size in-place rewrite ("300" -> "120") landing inside one
    coarse-mtime tick, stranding the old value at the consumer.
    """
    raw_env = os.environ.get(env_name)
    if raw_env:
        v = parse(raw_env)
        if v is not None:
            _WARNED_ENV.pop(env_name, None)
            return v
        if _WARNED_ENV.get(env_name) != raw_env:
            _WARNED_ENV[env_name] = raw_env
            print(
                f"[tapscribe] ignoring {env_name}={raw_env!r} (unparseable or out of bounds); "
                "using the config file / default",
                flush=True,
            )
    v = parse(read_text_file(file_path))
    return v if v is not None else default
