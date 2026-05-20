"""Tests for tapscribe.text — pure helpers, no FastAPI / no audio deps."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from tapscribe import text


def test_safe_name_keeps_alnum_and_dash():
    assert text.safe_name("Alice-01") == "Alice-01"


def test_safe_name_replaces_other_chars():
    assert text.safe_name("Alice Smith!") == "Alice_Smith_"


def test_safe_name_truncates_at_64():
    long = "a" * 80
    assert text.safe_name(long) == "a" * 64


def test_safe_name_handles_none_and_empty():
    assert text.safe_name(None) == ""
    assert text.safe_name("") == ""


def test_normalise_for_exact_strips_punct_and_lowercases():
    assert text.normalise_for_exact("  Thank you for watching!.  ") == "thank you for watching"


def test_normalise_for_exact_keeps_inner_punct():
    # Only leading/trailing punctuation should be stripped.
    assert text.normalise_for_exact("hello, world") == "hello, world"


def test_clean_meta_tokens_strips_blank_audio():
    raw = "Hello [BLANK_AUDIO] world"
    assert text.clean_meta_tokens(raw) == "Hello world"


def test_clean_meta_tokens_strips_truncated_token():
    # Both [BLANK_AUDIO] and the truncated [BLANK_ get stripped; whitespace
    # collapses; trailing space is .strip()ped — leaving just the period.
    raw = ". [BLANK_AUDIO] [BLANK_"
    assert text.clean_meta_tokens(raw) == "."


def test_clean_meta_tokens_collapses_whitespace():
    raw = "Hello    world   "
    assert text.clean_meta_tokens(raw) == "Hello world"


def test_clean_meta_tokens_keeps_brackets_word():
    # [blanket] is real bracketed speech, NOT a meta token. Don't strip it.
    raw = "she wore a [blanket]"
    assert text.clean_meta_tokens(raw) == "she wore a [blanket]"


def test_parse_wav_start_round_trips_iso():
    name = "2026-05-12T09-19-55Z_alice_ident01_abcdef01.wav"
    parsed = text.parse_wav_start(name)
    assert parsed == datetime(2026, 5, 12, 9, 19, 55, tzinfo=UTC)


def test_parse_wav_start_returns_none_for_garbage():
    assert text.parse_wav_start("nonsense.wav") is None
    assert text.parse_wav_start("") is None


def test_parse_wav_speaker_slug_extracts_middle_chunks():
    name = "2026-05-12T09-19-55Z_alice_smith_ident01_abcdef01.wav"
    assert text.parse_wav_speaker_slug(name) == "alice_smith"


def test_parse_wav_speaker_slug_returns_empty_for_short_names():
    assert text.parse_wav_speaker_slug("only_three_parts.wav") == ""


def test_build_recorder_wav_name_round_trips_through_parsers():
    """The helper's output must satisfy both parse_wav_start and
    parse_wav_speaker_slug — that's the contract that lets the
    strip-silence splitter and the live recorder share filename
    conventions."""
    when = datetime(2026, 5, 12, 9, 19, 55, tzinfo=UTC)
    name = text.build_recorder_wav_name(when, "alice_smith", "ident01")
    assert text.parse_wav_start(name) == when
    assert text.parse_wav_speaker_slug(name) == "alice_smith"
    assert name.endswith(".wav")


def test_build_recorder_wav_name_strips_path_separators_from_slugs():
    """`/` and `\\` would let a slug escape its directory if interpolated
    raw. The helper passes both slugs through `safe_name`, so the
    resulting filename is always a single path component."""
    when = datetime(2026, 5, 12, 9, 19, 55, tzinfo=UTC)
    name = text.build_recorder_wav_name(when, "../etc/passwd", "id/with/slash")
    assert "/" not in name
    assert "\\" not in name


def test_build_recorder_wav_name_rejects_naive_datetime():
    """strftime would emit a misleading `Z` on a naive datetime — guard
    at the contract boundary so downstream parsers don't lie."""
    naive = datetime(2026, 5, 12, 9, 19, 55)
    with pytest.raises(ValueError):
        text.build_recorder_wav_name(naive, "alice", "ident01")


def test_build_recorder_wav_name_rejects_non_utc_offset():
    plus_one = datetime(2026, 5, 12, 9, 19, 55, tzinfo=timezone(timedelta(hours=1)))
    with pytest.raises(ValueError):
        text.build_recorder_wav_name(plus_one, "alice", "ident01")


def test_read_prompt_returns_empty_when_missing(tmp_config_dir):
    assert text.read_prompt() == ""


def test_read_prompt_returns_file_contents(tmp_config_dir):
    (tmp_config_dir / "prompt.txt").write_text("hello world", encoding="utf-8")
    assert text.read_prompt() == "hello world"


def test_read_hotwords_strips_whitespace(tmp_config_dir):
    (tmp_config_dir / "hotwords.txt").write_text("\n  Acme, Patricia  \n", encoding="utf-8")
    assert text.read_hotwords() == "Acme, Patricia"
