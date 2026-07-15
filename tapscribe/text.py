"""Pure text helpers — slug parsing, sanitizers, parse_iso.

Everything in here is side-effect-free and depends on nothing in TapScribe.

The operator-config persistence layer (CONFIG_KEYS, read_config, write_config,
summarizer config, languages) moved to `config_store` so text.py stays
dependency-free and catalog-pure.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from uuid import uuid4

# Re-exports — the config-store layer moved to tapscribe.config_store so
# text.py stays catalog-free. Existing callers (from .text import X) keep
# working without change.
from .config_store import (  # noqa: F401
    _CONFIG_TEXT_CACHE,
    CONFIG_KEYS,
    MAX_CONFIG_TEXT_LEN,
    SUMMARY_SOURCES,
    atomic_write_text,
    file_stat_sig,
    read_config,
    read_languages,
    read_summarizer_config,
    read_text_file,
    summarizer_default_public,
    validate_config_text,
    write_config,
    write_languages,
    write_summarizer_config,
)

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
