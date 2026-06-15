"""Pure text helpers — prompt/hotwords reading, slug parsing, sanitisers.

Everything in here is side-effect-free apart from disk reads, and depends
on nothing in TapScribe besides config paths. Easy to unit-test.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from . import config

# Whisper's internal special tokens occasionally leak through as literal text
# ("[BLANK_AUDIO]", "[BLANK_AUDIO/BLANK_AUDIO", "[BLANK_") on near-silent or
# ambiguous frames. WhisperLiveKit forwards them unfiltered. They're never
# real speech and clutter the live feed badly — strip at ingest.
#
# Pattern requires `[BLANK` immediately followed by `_` or `]` so we don't
# strip a real bracketed word like `[blanket]` (Whisper sometimes brackets
# uncertain words). After that, any mix of word chars / slash / dash, with
# an optional closing bracket (token may be truncated).
WHISPER_META_TOKEN_RE = re.compile(r"\[BLANK[_\]][\w/\-]*\]?", re.IGNORECASE)

_TERMINAL_PUNCT_RE = re.compile(r"[\s.,;:!?\"'‘’“”…\-]+$")
_LEADING_PUNCT_RE = re.compile(r"^[\s.,;:!?\"'‘’“”…\-]+")


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


def file_stat_sig(path: Path, *, include_path: bool = False) -> tuple | None:
    """A cheap change-detection signature for `path`: `(mtime_ns, size)`, or
    `(str(path), mtime_ns, size)` when `include_path` is set — for a single-slot
    cache that must tell different files apart. None when the file is
    missing/unreadable. Shared by the /api/state poll caches so they recompute
    only when a file actually changes (writes go through an atomic replace,
    which always moves the signature)."""
    try:
        st = path.stat()
    except OSError:
        return None
    if include_path:
        return (str(path), st.st_mtime_ns, st.st_size)
    return (st.st_mtime_ns, st.st_size)


# Memoise the editable config files on their stat signature so the
# once-per-second /api/state poll doesn't re-read prompt / live-prompt /
# hotwords every tick. Keyed by path string so a test that repoints these
# config paths can't get a stale hit. `read_text_file` itself stays uncached —
# it's the primitive other callers (and parse_rules, which has its own cache)
# rely on for an always-fresh read.
_CONFIG_TEXT_CACHE: dict[str, tuple[tuple | None, str]] = {}


def _read_config_text_cached(path: Path) -> str:
    pathkey = str(path)
    sig = file_stat_sig(path)
    if sig is None:
        # Missing/unreadable: don't cache (re-reading a missing file is a
        # single cheap stat that returns "" fast), and drop any stale entry.
        _CONFIG_TEXT_CACHE.pop(pathkey, None)
        return read_text_file(path)
    hit = _CONFIG_TEXT_CACHE.get(pathkey)
    if hit is not None and hit[0] == sig:
        return hit[1]
    value = read_text_file(path)
    _CONFIG_TEXT_CACHE[pathkey] = (sig, value)
    return value


def read_prompt() -> str:
    """Return the Whisper `initial_prompt` from prompt.txt. Re-read whenever
    the file changes (stat-signature cache) so edits take effect without
    restarting the recorder."""
    return _read_config_text_cached(config.PROMPT_FILE)


def read_live_prompt() -> str:
    """Return the WhisperLiveKit `--init-prompt` from live-prompt.txt.

    Independent from `read_prompt()` — an empty live-prompt.txt does NOT
    fall back to prompt.txt, since the dashboard exposes the two as two
    separate editors and operators are expected to set each explicitly
    (live and batch typically run different cadences and sometimes
    different model families)."""
    return _read_config_text_cached(config.LIVE_PROMPT_FILE)


def read_live_model() -> str:
    """Return the operator's DEFAULT live-channel model id from live-model.txt
    (a single model_id, e.g. "tiny.en"), stripped. Empty when unset.

    Separate from the running channel's model (`live_info.model`): the
    dashboard's Live engine card persists the default here, and the live
    channel only adopts it on (re)start — so the UI can flag "restart to
    apply" while the two differ."""
    return _read_config_text_cached(config.LIVE_MODEL_FILE).strip()


def read_batch_model() -> str:
    """Return the operator's DEFAULT batch model id from batch-model.txt
    (a single model_id, e.g. "small.en"), stripped. Empty when unset.

    The live-model's batch twin: the dashboard's Default engine card
    persists the default here, and the end-of-meeting pipeline resolves its
    transcribe stage from it — the tap trigger carries no model field by
    design (operator defaults only)."""
    return _read_config_text_cached(config.BATCH_MODEL_FILE).strip()


def read_summarizer_config() -> dict:
    """Return the operator's DEFAULT summarizer config from summarizer.json
    (#84), normalised to the full shape — {source, prompt, command, model,
    max_tokens} — with all-empty defaults when the file is missing or
    unparseable. Stat-signature cached like the other config reads (the JSON
    parse of ~200 bytes per poll tick is negligible on top of that)."""
    raw = _read_config_text_cached(config.SUMMARIZER_CONFIG_FILE)
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    out = {
        k: data[k] if isinstance(data.get(k), str) else ""
        for k in ("source", "prompt", "command", "model", "base_url", "api_key")
    }
    mt = data.get("max_tokens")
    out["max_tokens"] = mt if isinstance(mt, int) and not isinstance(mt, bool) else None
    return out


def summarizer_default_public(cfg: dict) -> dict:
    """The /api/state projection of the stored summarizer default — an
    explicit field ALLOWLIST, not a passthrough. This is the redaction seam
    for #85: the API source's `base_url` is non-secret and included; `api_key`
    is NEVER exposed, only `key_set` (a boolean). Pinned by a test asserting
    the exact key set."""
    return {
        "source": cfg.get("source"),
        "prompt": cfg.get("prompt"),
        "command": cfg.get("command"),
        "model": cfg.get("model"),
        "max_tokens": cfg.get("max_tokens"),
        "base_url": cfg.get("base_url"),
        "key_set": bool((cfg.get("api_key") or "").strip()),
    }


# The wired summarizer sources — ONE allowlist shared by the global-default
# writer below and the per-session override validator
# (`tapscribe.sessions.write_session_meta`), so wiring the API source (#85)
# is a single-tuple change that covers both write paths. "" means unset (no
# global default) / cleared (no per-session override).
SUMMARY_SOURCES: tuple[str, ...] = ("", "local", "command", "api")


def write_summarizer_config(cfg: dict) -> dict:
    """Validate + persist the operator's DEFAULT summarizer config to
    summarizer.json (#84). Full-object semantics: the PUT always sends the
    whole structured object, so a missing key clears that field — EXCEPT for
    `api_key` which uses preserve-on-omit semantics (the browser never
    receives the key, only `key_set`, so it cannot echo it back on a partial
    update; omitting it preserves the stored value). Returns the normalised
    stored dict.

    Like `write_batch_model`, validation happens at WRITE time — the value
    feeds the end-of-meeting pipeline's summarizer with no operator in the
    loop, and a model id arriving from the dashboard is external input that
    must never reach a Hub download (`ValueError` → the PUT's 400):

    - `source`: "" (no default) | "local" | "command" | "api".
    - `model`: "" (catalog default) or a member of the local backend's
      catalog allowlist / env-override model (`_is_allowed_local_model`).
    - `prompt` / `command` / `base_url`: free text under the MAX_CONFIG_TEXT_LEN
      cap. `base_url` must start with http:// or https:// if non-empty.
    - `max_tokens`: None (env default) or an int within the catalog bounds.
    - `api_key`: write-only; omit to preserve, empty string to clear.

    The catalog import is lazy (write_batch_model's pattern) so this module
    stays free of the summarizers dependency for every other caller."""
    source = str(cfg.get("source") or "").strip()
    if source not in SUMMARY_SOURCES:
        raise ValueError(
            f"unknown summarizer source: {source!r} (expected one of: {', '.join(s for s in SUMMARY_SOURCES if s)})"
        )
    prompt = validate_config_text(str(cfg.get("prompt") or ""))
    command = validate_config_text(str(cfg.get("command") or ""))
    model = str(cfg.get("model") or "").strip()
    if model:
        from .summarizers.catalog import (
            _is_allowed_local_model,
            _resolve_local_backend,
            _unknown_model_message,
        )

        backend = _resolve_local_backend()
        if not _is_allowed_local_model(backend, model):
            raise ValueError(_unknown_model_message(backend, model))
    max_tokens = cfg.get("max_tokens")
    if max_tokens is not None:
        if not isinstance(max_tokens, int) or isinstance(max_tokens, bool):
            raise ValueError(f"max_tokens must be an integer, got {max_tokens!r}")
        from .summarizers.catalog import _MAX_TOKENS_BOUNDS

        lo, hi = _MAX_TOKENS_BOUNDS
        if not (lo <= max_tokens <= hi):
            raise ValueError(f"max_tokens must be within {lo}–{hi}, got {max_tokens}")
    # base_url: validate text cap, then scheme guard.
    base_url = validate_config_text(str(cfg.get("base_url") or ""))
    if base_url and not base_url.startswith(("http://", "https://")):
        raise ValueError(f"base_url must be an http(s) URL, got {base_url!r}")
    # api_key: preserve-on-omit — the browser never receives the key (only
    # key_set), so it cannot echo it back; a PUT editing base_url must NOT
    # wipe the stored key. Present + non-empty → set; present + "" → clear.
    if "api_key" in cfg:
        api_key = validate_config_text(str(cfg.get("api_key") or ""))
    else:
        api_key = read_summarizer_config()["api_key"]  # preserve — write-only field
    stored = {
        "source": source,
        "prompt": prompt,
        "command": command,
        "model": model,
        "max_tokens": max_tokens,
        "base_url": base_url,
        "api_key": api_key,
    }
    atomic_write_text(config.SUMMARIZER_CONFIG_FILE, json.dumps(stored, indent=2) + "\n")
    return stored


def read_hotwords() -> str:
    """Return the faster-whisper `hotwords` string from hotwords.txt — a
    comma- or space-separated list of proper nouns / tricky vocabulary."""
    return _read_config_text_cached(config.HOTWORDS_FILE)


# Cap pasted prompts/hotwords at 4000 chars. Whisper's init_prompt is
# capped around 224 tokens (~1k chars), so anything bigger is almost
# certainly a paste mistake (transcript dump, log) — fail at the API
# boundary rather than silently truncating downstream.
MAX_CONFIG_TEXT_LEN: int = 4000


def atomic_write_text(path: Path, content: str) -> None:
    """Write `content` to `path` via tempfile + os.replace so a crashed
    write never leaves a half-written file on disk. Caller is responsible
    for whatever serialisation produced `content` (raw text, JSON, …).

    Shared by `_write_text_file_atomic` (prompt/hotwords files, with CRLF
    normalisation) and `tapscribe.sessions.write_session_meta` (JSON, no
    normalisation needed because json.dumps escapes any literal CR)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
        os.replace(tmp, path)
    except Exception:
        # Best-effort cleanup of the half-written tempfile, then re-raise
        # the real error. See the inner OSError handler for why ignoring
        # an unlink failure here is safe.
        try:
            os.unlink(tmp)
        except OSError:
            # Tempfile already gone (e.g. the write raised before the fd
            # was flushed, or the dir was removed) — nothing to clean up,
            # and the outer `raise` below propagates the original error.
            pass
        raise


def _write_text_file_atomic(path: Path, content: str) -> None:
    """Atomic write of a config text file. CRLF is normalised to LF so
    the Whisper CLI doesn't see literal `\\r` in the prompt."""
    normalised = content.replace("\r\n", "\n").replace("\r", "\n")
    atomic_write_text(path, normalised)


def validate_config_text(content: str) -> str:
    """Reject oversize input (see MAX_CONFIG_TEXT_LEN). Returns the
    content unchanged on success so callers can chain.

    Shared by the global config writers AND by
    `tapscribe.sessions.write_session_meta` so per-session prompt /
    hotwords overrides can't bypass the cap by persisting through the
    session-meta API instead of /api/config/{key}."""
    if len(content) > MAX_CONFIG_TEXT_LEN:
        raise ValueError(f"config text exceeds {MAX_CONFIG_TEXT_LEN}-char cap (got {len(content)} chars)")
    return content


def write_prompt(content: str) -> None:
    """Persist the batch initial prompt to prompt.txt. Atomic; oversize input rejected."""
    _write_text_file_atomic(config.PROMPT_FILE, validate_config_text(content))


def write_live_prompt(content: str) -> None:
    """Persist the live-channel init prompt to live-prompt.txt. Atomic;
    oversize input rejected."""
    _write_text_file_atomic(config.LIVE_PROMPT_FILE, validate_config_text(content))


def write_live_model(content: str) -> None:
    """Persist the default live-channel model id to live-model.txt. Stored
    stripped (it's a single model_id token, not free text). Atomic; oversize
    input rejected. The value isn't validated against the registry here — an
    unknown id surfaces as a clear error at /api/live/start time, not silently."""
    _write_text_file_atomic(config.LIVE_MODEL_FILE, validate_config_text(content.strip()))


def write_batch_model(content: str) -> None:
    """Persist the default batch model id to batch-model.txt. Stored stripped
    (a single model_id token). Atomic; oversize input rejected.

    Unlike `write_live_model`, the value IS validated against the transcriber
    catalog here: the batch default feeds the end-of-meeting pipeline's model
    loader with no operator in the loop, so an unknown id must never land on
    disk (`ValueError` → the config PUT's 400). Empty clears the override
    (back to the bundled default). The catalog import is lazy to keep this
    module free of the transcribers dependency for every other caller."""
    model_id = content.strip()
    if model_id:
        from .transcribers.catalog import REGISTRY

        if REGISTRY.get(model_id) is None:
            raise ValueError(f"unknown batch model id: {model_id!r} (not in the catalog)")
    _write_text_file_atomic(config.BATCH_MODEL_FILE, validate_config_text(model_id))


def write_hotwords(content: str) -> None:
    """Persist the hotwords list to hotwords.txt. Atomic; oversize input rejected."""
    _write_text_file_atomic(config.HOTWORDS_FILE, validate_config_text(content))


def normalise_for_exact(text: str) -> str:
    """Lowercase and strip leading/trailing whitespace and punctuation. Used
    by the `exact:` matcher in the hallucination filter."""
    t = text.lower()
    t = _LEADING_PUNCT_RE.sub("", t)
    t = _TERMINAL_PUNCT_RE.sub("", t)
    return t.strip()


def clean_meta_tokens(raw: str) -> str:
    """Strip Whisper meta-tokens ([BLANK_AUDIO] etc.) and collapse runs of
    whitespace. Used on inbound live-transcript ingest."""
    cleaned = WHISPER_META_TOKEN_RE.sub("", raw)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def safe_name(s: str | None) -> str:
    """Sanitise a freeform name for use inside a recording filename. Keeps
    alnum / dash / underscore / dot; everything else becomes underscore.
    Capped at 64 chars."""
    if not s:
        return ""
    return "".join(c if (c.isalnum() or c in "-_.") else "_" for c in s)[:64]


def parse_iso(s: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp into a tz-aware UTC datetime. Accepts
    a trailing `Z`, treats naive timestamps as UTC, and returns None for
    blank/missing input. Used by both the per-WAV sidecar reader and
    the session merger."""
    if not s:
        return None
    s = s.strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def parse_wav_start(name: str) -> datetime | None:
    """Extract the UTC start time from a recording filename.

    Filenames follow: <YYYY-MM-DDTHH-MM-SSZ>_<name_slug>_<short_id>_<utt>.wav
    """
    try:
        head = name.split("_", 1)[0]
        # strptime can't read the trailing Z in this format, so peel it.
        if head.endswith("Z"):
            head = head[:-1]
        return datetime.strptime(head, "%Y-%m-%dT%H-%M-%S").replace(tzinfo=UTC)
    except (ValueError, IndexError):
        return None


def parse_wav_speaker_slug(name: str) -> str:
    """Best-effort recovery of the speaker name from a recording filename.
    We can't perfectly invert safe_name (spaces/dots became underscores) but
    the slug is human-readable. Returns the middle chunk between the
    timestamp and the trailing short_id/utt suffix."""
    base = name.rsplit(".", 1)[0]
    parts = base.split("_")
    if len(parts) < 4:
        return ""
    return "_".join(parts[1:-2])


def parse_wav_speaker_ident(name: str) -> tuple[str, str]:
    """Pull `(speaker_slug, ident)` out of a recorder filename so callers can
    stitch them back into per-region output names (strip-silence) or bucket
    regions under their source original (the session listing).

    Falls back to safe defaults (`"anon"`, `"unknown"`) when the input doesn't
    follow the `<iso>_<speaker_slug>_<ident>_<utt>.wav` convention, so a
    hand-dropped WAV still produces workable output. Sits with the other
    recorder-filename parsers (`parse_wav_start`, `parse_wav_speaker_slug`) as
    the single source of truth for the format `build_recorder_wav_name` mints."""
    speaker = parse_wav_speaker_slug(name) or "anon"
    stem = name.rsplit(".", 1)[0]
    parts = stem.split("_")
    ident = parts[-2] if len(parts) >= 4 else "unknown"
    return speaker, ident


def build_recorder_wav_name(start: datetime, speaker_slug: str, ident: str) -> str:
    """Mint the canonical recorder filename:
    `<YYYY-MM-DDTHH-MM-SSZ>_<speaker_slug>_<ident>_<uuid8>.wav`.

    Single source of truth for the format `parse_wav_start` and
    `parse_wav_speaker_slug` parse — both the live `/tap` recorder and
    the strip-silence splitter mint names through here so they can't
    drift apart. The trailing 8-char uuid is a tiebreaker for filenames
    that share a wall-clock second.

    Defensive: both slugs run through `safe_name` so anything containing
    a path separator (or other filename-hostile char) is reduced to
    underscores before the components are interpolated. Callers can
    trust the return value contains no `/`, `\\`, or `..`."""
    # strftime("%Y-…Z") would happily emit a "Z" on a naive datetime,
    # producing a filename that lies about being UTC. Catch that at the
    # contract boundary so downstream parse_wav_start always recovers a
    # true UTC instant.
    if start.tzinfo is None or start.utcoffset() != timedelta(0):
        raise ValueError(f"build_recorder_wav_name requires a UTC-aware datetime; got {start!r}")
    safe_speaker = safe_name(speaker_slug) or "anon"
    safe_ident = safe_name(ident) or "unknown"
    stamp = start.strftime("%Y-%m-%dT%H-%M-%SZ")
    return f"{stamp}_{safe_speaker}_{safe_ident}_{uuid4().hex[:8]}.wav"
