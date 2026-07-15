"""Config-store: the operator-config persistence layer.

Keeps text.py the dependency-free pure-helpers module its docstring claims.
This module owns all catalog-dependent validation and the shared config-store
helpers (CONFIG_KEYS, atomic_write_text, read_config, write_config, the
summarizer config reader/writer, languages config).

Dependency order: config_store is a leaf relative to catalog packages.
text.py re-exports these symbols so existing callers (from .text import X)
keep working without change.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import config

# ---------------------------------------------------------------------------
# Pure helpers — in config_store so it stays a leaf module (no back-reference
# to text.py), and text.py can import from here without circular imports.
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


def file_stat_sig(path: Path, *, include_path: bool = False) -> tuple | None:
    """A cheap change-detection signature for `path`: `(mtime_ns, size, ino)`,
    or `(str(path), mtime_ns, size, ino)` when `include_path` is set — for a
    single-slot cache that must tell different files apart. None when the file
    is missing/unreadable. Shared by the /api/state poll caches so they
    recompute only when a file actually changes. The inode is part of the
    signature because mtime alone is NOT enough: on a coarse-mtime filesystem
    (FAT/exFAT ~2s, some NFS) a same-size rewrite inside one granularity
    bucket keeps (mtime_ns, size) identical — but writes go through an atomic
    tempfile + os.replace, which lands a new inode, so the signature still
    moves. Callers comparing against PERSISTED (mtime, size) pairs must slice
    (see sessions.read_strip_meta's caller) — the inode is only stable within
    a process's view of the live file, never something to store."""
    try:
        st = path.stat()
    except OSError:
        return None
    if include_path:
        return (str(path), st.st_mtime_ns, st.st_size, st.st_ino)
    return (st.st_mtime_ns, st.st_size, st.st_ino)


# ---------------------------------------------------------------------------
# Shared config-store helpers
# ---------------------------------------------------------------------------

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


@dataclass(frozen=True)
class _ConfigSpec:
    """One editable config text file. `attr` names the config-module Path
    (resolved at call time, so tests can repoint the paths); `strip` marks
    single-token values (model ids), stored and returned stripped; `check`
    runs at WRITE time after the shared oversize cap."""

    attr: str
    strip: bool = False
    check: Callable[[str], None] | None = None


# WRITE-time check functions for the catalog-validated config keys. Defined
# before CONFIG_KEYS so the dict literal can reference them directly; each
# imports its catalog dependency lazily in-body, so this module stays a leaf
# (no module-level catalog import) for every other caller.


def _check_hallucinations(content: str) -> None:
    """WRITE-time check for the "hallucinations" key: iterate lines, skip
    blank/comment/non-`re:` lines, and for `re:` lines validate the pattern via
    the shared `hallucinations.regex_rule_ok` authority (empty / ReDoS-shape /
    oversize / uncompilable). When it returns False raise
    `ValueError(f"invalid rule on line {n}: {line!r}")`.

    Non-`re:` lines (substr, `exact:`) pass through — always valid at write time.
    STRICT at write, LENIENT at runtime: the runtime parser
    (`hallucinations._parse_rules_uncached`) calls the SAME `regex_rule_ok`
    but SKIPS a bad rule instead of raising, so a legacy hand-edited file can't
    wedge a transcribe job. The hallucinations import is lazy to keep this
    module free of that dependency for every other caller."""
    from . import hallucinations

    for n, line in enumerate(content.splitlines(), start=1):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if s.lower().startswith("re:"):
            pat = s[3:].strip()
            if not hallucinations.regex_rule_ok(pat):
                raise ValueError(f"invalid rule on line {n}: {line!r}")


def _check_batch_model(model_id: str) -> None:
    """WRITE-time check for the "batch-model" key: the batch default feeds
    the end-of-meeting pipeline's model loader with no operator in the loop,
    so an unknown id must never land on disk (`ValueError` → the config PUT's
    400). Empty clears the override (back to the bundled default). The
    catalog import is lazy to keep this module free of the transcribers
    dependency for every other caller.

    Deliberately NOT applied to "live-model": there an unknown id surfaces
    as a clear error at /api/live/start time, not silently."""
    if model_id:
        from .transcribers.catalog import REGISTRY

        if REGISTRY.get(model_id) is None:
            raise ValueError(f"unknown batch model id: {model_id!r} (not in the catalog)")


def _check_idle_ttl(content: str) -> None:
    """WRITE-time check for the "model-idle-ttl" key: value must parse as float
    within _IDLE_TTL_BOUNDS. Empty clears the override."""
    if not content:
        return
    try:
        v = float(content)
    except ValueError:
        raise ValueError(f"idle TTL must be a number, got {content!r}") from None
    from .transcribers import _IDLE_TTL_BOUNDS  # lazy — avoids cycle

    if not (_IDLE_TTL_BOUNDS[0] <= v <= _IDLE_TTL_BOUNDS[1]):
        lo, hi = _IDLE_TTL_BOUNDS
        raise ValueError(f"idle TTL must be between {lo} and {hi}, got {v}")


# The editable config files behind read_config / write_config, keyed by the
# same key the dashboard PUTs to /api/config/{key}. The richer shapes
# (languages.txt's catalog-validated set, summarizer.json) keep their own
# accessors below — this table is only the plain text files.
CONFIG_KEYS: dict[str, _ConfigSpec] = {
    # Whisper `initial_prompt` for batch runs (prompt.txt).
    "prompt": _ConfigSpec("PROMPT_FILE"),
    # WhisperLiveKit `--init-prompt` (live-prompt.txt). Independent from
    # "prompt" — an empty live-prompt does NOT fall back to prompt.txt; the
    # dashboard exposes two separate editors and operators set each
    # explicitly (live and batch typically run different cadences and
    # sometimes different model families).
    "live-prompt": _ConfigSpec("LIVE_PROMPT_FILE"),
    # Operator's DEFAULT live-channel model id (live-model.txt). Separate
    # from the running channel's model (`live_info.model`): the live channel
    # only adopts it on (re)start, so the UI can flag "restart to apply"
    # while the two differ.
    "live-model": _ConfigSpec("LIVE_MODEL_FILE", strip=True),
    # The live-model's batch twin (batch-model.txt): the end-of-meeting
    # pipeline resolves its transcribe stage from it — the tap trigger
    # carries no model field by design (operator defaults only).
    "batch-model": _ConfigSpec("BATCH_MODEL_FILE", strip=True, check=_check_batch_model),
    # faster-whisper `hotwords` (hotwords.txt) — comma- or space-separated
    # proper nouns / tricky vocabulary.
    "hotwords": _ConfigSpec("HOTWORDS_FILE"),
    # Hallucination filter rules (hallucinations.txt) — one rule per line:
    # plain substring, `exact:...`, or `re:...`. Write-time check validates
    # regex safety so a bad rule never lands on disk.
    "hallucinations": _ConfigSpec("HALLUCINATIONS_FILE", check=_check_hallucinations),
    # Model idle-TTL knob (model-idle-ttl.txt): seconds of idle before
    # eviction. Dashboard writes via config-store; _idle_ttl_s() reads at
    # use-time when env var is unset.
    "model-idle-ttl": _ConfigSpec("MODEL_IDLE_TTL_FILE", strip=True, check=_check_idle_ttl),
}

# A candidate-language set is a small comma/space-separated bag of ISO codes.
_LANG_SPLIT_RE = re.compile(r"[,\s]+")


def parse_language_codes(raw: str) -> list[str]:
    """Split a comma/space-separated language string into lowercased ISO codes,
    in order, dropping blanks. Shared by the global-config reader/writer and the
    per-session override validator so both normalise identically."""
    return [c.strip().lower() for c in _LANG_SPLIT_RE.split(raw or "") if c.strip()]


def read_config(key: str) -> str:
    """Return the current value of an editable config file (see CONFIG_KEYS).
    Re-read whenever the file changes (stat-signature cache) so edits take
    effect without restarting the recorder; empty when unset."""
    spec = CONFIG_KEYS[key]
    value = _read_config_text_cached(getattr(config, spec.attr))
    return value.strip() if spec.strip else value


def write_config(key: str, content: str) -> None:
    """Validate + persist one editable config file (see CONFIG_KEYS). Atomic
    via tempfile + os.replace; oversize input rejected (MAX_CONFIG_TEXT_LEN);
    single-token keys stored stripped; the per-key WRITE-time `check` runs
    before anything lands on disk (see `_check_batch_model`)."""
    spec = CONFIG_KEYS[key]
    if spec.strip:
        content = content.strip()
    if spec.check is not None:
        spec.check(content)
    path = getattr(config, spec.attr)
    _write_text_file_atomic(path, validate_config_text(content))
    # Structural invalidation: our own write must never be served stale, even
    # if the filesystem's stat signature failed to move — the next read
    # re-reads what we just wrote.
    _CONFIG_TEXT_CACHE.pop(str(path), None)


# Cap pasted prompts/hotwords at 4000 chars. Whisper's init_prompt is
# capped around 224 tokens (~1k chars), so anything bigger is almost
# certainly a paste mistake (transcript dump, log) — fail at the API
# boundary rather than silently truncating downstream.
MAX_CONFIG_TEXT_LEN: int = 4000


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
    the Whisper CLI doesn't see literal `\r` in the prompt."""
    normalised = content.replace("\r\n", "\n").replace("\r", "\n")
    atomic_write_text(path, normalised)


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

    Like the "batch-model" config key, validation happens at WRITE time — the value
    feeds the end-of-meeting pipeline's summarizer with no operator in the
    loop, and a model id arriving from the dashboard is external input that
    must never reach a Hub download (`ValueError` → the PUT's 400):

    - `source`: "" (no default) | "local" | "command" | "api".
    - `model`: "" (catalog default) or a member of the local backend's
      catalog allowlist / env-override model (`is_allowed_local_model`).
    - `prompt` / `command` / `base_url`: free text under the MAX_CONFIG_TEXT_LEN
      cap. `base_url` must start with http:// or https:// if non-empty.
    - `max_tokens`: None (env default) or an int within the catalog bounds.
    - `api_key`: write-only; omit to preserve, empty string to clear.

    The catalog import is lazy (_check_batch_model's pattern) so this module
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
            is_allowed_local_model,
            resolve_local_backend,
            unknown_model_message,
        )

        backend = resolve_local_backend()
        if not is_allowed_local_model(backend, model):
            raise ValueError(unknown_model_message(backend, model))
    max_tokens = cfg.get("max_tokens")
    if max_tokens is not None:
        if not isinstance(max_tokens, int) or isinstance(max_tokens, bool):
            raise ValueError(f"max_tokens must be an integer, got {max_tokens!r}")
        from .summarizers.catalog import MAX_TOKENS_BOUNDS

        lo, hi = MAX_TOKENS_BOUNDS
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


def read_languages() -> tuple[str, ...]:
    """Return the operator's DEFAULT candidate-language set from languages.txt
    (ADR-0010) as a code tuple, e.g. ("da", "no", "en"). Non-catalog codes are
    dropped; an empty or all-invalid file falls back to the bundled catch-all
    default so the feature works with no configuration."""
    from .transcribers.catalog import DEFAULT_CANDIDATE_LANGUAGES, is_candidate_language

    codes = parse_language_codes(_read_config_text_cached(config.LANGUAGES_FILE))
    valid = tuple(dict.fromkeys(c for c in codes if is_candidate_language(c)))
    return valid or DEFAULT_CANDIDATE_LANGUAGES


def write_languages(content: str) -> None:
    """Persist the default candidate-language set to languages.txt, stored as
    deduped lowercased comma-joined codes. Empty clears the override (back to
    the bundled default).

    Like the "batch-model" config key, validation happens at WRITE time: the set feeds the
    end-of-meeting pipeline's per-region language run with no operator in the
    loop, so a code outside the catalog must never land on disk (`ValueError`
    → the config PUT's 400). The catalog import is lazy to keep this module
    dependency-free for every other caller."""
    from .transcribers.catalog import is_candidate_language

    codes = parse_language_codes(content)
    for c in codes:
        if not is_candidate_language(c):
            raise ValueError(f"unknown language code: {c!r} (not in the catalog)")
    deduped = list(dict.fromkeys(codes))
    _write_text_file_atomic(config.LANGUAGES_FILE, validate_config_text(",".join(deduped)))
