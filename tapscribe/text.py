"""Pure text helpers — prompt/hotwords reading, slug parsing, sanitisers.

Everything in here is side-effect-free apart from disk reads, and depends
on nothing in TapScribe besides config paths. Easy to unit-test.
"""

from __future__ import annotations

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


def read_prompt() -> str:
    """Return the Whisper `initial_prompt` from prompt.txt. Read on every
    call so edits take effect without restarting the recorder."""
    return read_text_file(config.PROMPT_FILE)


def read_live_prompt() -> str:
    """Return the WhisperLiveKit `--init-prompt` from live-prompt.txt.

    Independent from `read_prompt()` — an empty live-prompt.txt does NOT
    fall back to prompt.txt, since the dashboard exposes the two as two
    separate editors and operators are expected to set each explicitly
    (live and batch typically run different cadences and sometimes
    different model families)."""
    return read_text_file(config.LIVE_PROMPT_FILE)


def read_hotwords() -> str:
    """Return the faster-whisper `hotwords` string from hotwords.txt — a
    comma- or space-separated list of proper nouns / tricky vocabulary."""
    return read_text_file(config.HOTWORDS_FILE)


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
