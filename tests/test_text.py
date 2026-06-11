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


# ---------------------------------------------------------------------------
# read_live_prompt — independent from read_prompt; empty live-prompt.txt
# does NOT fall back to prompt.txt (the dashboard exposes the two as two
# separate editors, so silently merging them would mislead operators).
# ---------------------------------------------------------------------------


def test_read_live_prompt_returns_empty_when_missing(tmp_config_dir):
    assert text.read_live_prompt() == ""


def test_read_live_prompt_returns_file_contents(tmp_config_dir):
    (tmp_config_dir / "live-prompt.txt").write_text("standup notes", encoding="utf-8")
    assert text.read_live_prompt() == "standup notes"


def test_read_live_prompt_does_not_fall_back_to_prompt_file(tmp_config_dir):
    """Independent storage: even with prompt.txt populated, an empty
    live-prompt.txt resolves to empty. Operators set live and batch
    explicitly."""
    (tmp_config_dir / "prompt.txt").write_text("batch context", encoding="utf-8")
    assert text.read_live_prompt() == ""


# ---------------------------------------------------------------------------
# Atomic writers — tempfile + rename so a crashed write never leaves a
# truncated file on disk. UTF-8 only; CRLF is normalised to LF so the
# Whisper CLI doesn't see literal `\r` in the prompt.
# ---------------------------------------------------------------------------


def test_write_prompt_creates_and_reads_back(tmp_config_dir):
    text.write_prompt("weekly planning · roadmap")
    assert (tmp_config_dir / "prompt.txt").read_text(encoding="utf-8") == "weekly planning · roadmap"
    assert text.read_prompt() == "weekly planning · roadmap"


def test_write_prompt_overwrites_existing(tmp_config_dir):
    (tmp_config_dir / "prompt.txt").write_text("old", encoding="utf-8")
    text.write_prompt("new")
    assert text.read_prompt() == "new"


def test_write_prompt_normalises_crlf_to_lf(tmp_config_dir):
    text.write_prompt("line one\r\nline two\r\n")
    on_disk = (tmp_config_dir / "prompt.txt").read_text(encoding="utf-8")
    assert "\r" not in on_disk
    assert on_disk.rstrip() == "line one\nline two"


def test_write_live_prompt_creates_and_reads_back(tmp_config_dir):
    text.write_live_prompt("standup")
    assert (tmp_config_dir / "live-prompt.txt").read_text(encoding="utf-8") == "standup"
    assert text.read_live_prompt() == "standup"


def test_write_hotwords_creates_and_reads_back(tmp_config_dir):
    text.write_hotwords("Acme, Patricia Lin")
    assert (tmp_config_dir / "hotwords.txt").read_text(encoding="utf-8") == "Acme, Patricia Lin"
    assert text.read_hotwords() == "Acme, Patricia Lin"


def test_write_prompt_empty_string_clears_file(tmp_config_dir):
    (tmp_config_dir / "prompt.txt").write_text("existing", encoding="utf-8")
    text.write_prompt("")
    assert text.read_prompt() == ""


def test_write_prompt_rejects_oversize(tmp_config_dir):
    """Whisper's init_prompt is capped at ~224 tokens (≈1k chars). Allow
    a generous 4000-char budget so operators can paste meeting context,
    but reject obvious mistakes (pasted transcripts, log dumps) so they
    fail loudly at the boundary instead of silently truncated downstream."""
    too_big = "x" * 5000
    with pytest.raises(ValueError):
        text.write_prompt(too_big)


def test_write_batch_model_accepts_catalog_id_and_reads_back(tmp_config_dir):
    text.write_batch_model("  small.en \n")
    assert (tmp_config_dir / "batch-model.txt").read_text(encoding="utf-8") == "small.en"
    assert text.read_batch_model() == "small.en"


def test_write_batch_model_rejects_unknown_model_id(tmp_config_dir):
    """Unlike write_live_model (where an unknown id surfaces loudly at
    /api/live/start), the batch default feeds the end-of-meeting pipeline's
    model loader with no operator in the loop — validate at WRITE time so a
    bad id never lands on disk in the first place."""
    with pytest.raises(ValueError):
        text.write_batch_model("evil/repo")
    assert text.read_batch_model() == ""


def test_write_batch_model_empty_clears_back_to_default(tmp_config_dir):
    text.write_batch_model("small.en")
    text.write_batch_model("")
    assert text.read_batch_model() == ""


def test_read_batch_model_empty_when_unset(tmp_config_dir):
    assert text.read_batch_model() == ""


# ---------------------------------------------------------------------------
# Summarizer default (config/summarizer.json) — the structured operator
# default (#84): source + prompt + per-source fields. Unlike the text
# configs this is one JSON object; the writer validates at WRITE time
# (write_batch_model's rationale: the value feeds the end-of-meeting
# pipeline's summarizer with no operator in the loop).
# ---------------------------------------------------------------------------


def test_read_summarizer_config_returns_defaults_when_missing(tmp_config_dir):
    assert text.read_summarizer_config() == {
        "source": "",
        "prompt": "",
        "command": "",
        "model": "",
        "max_tokens": None,
    }


def test_write_summarizer_config_round_trips(tmp_config_dir):
    stored = text.write_summarizer_config(
        {
            "source": "command",
            "prompt": "Summarize into action items.",
            "command": "claude -p",
            "model": "",
            "max_tokens": 2048,
        }
    )
    assert stored == text.read_summarizer_config()
    assert text.read_summarizer_config()["source"] == "command"
    assert text.read_summarizer_config()["max_tokens"] == 2048


def test_write_summarizer_config_missing_keys_clear_fields(tmp_config_dir):
    """Full-object semantics: the PUT sends the whole object, so a key left
    out clears that field back to its built-in-empty default."""
    text.write_summarizer_config({"source": "command", "command": "claude -p", "prompt": "P"})
    text.write_summarizer_config({})
    assert text.read_summarizer_config() == {
        "source": "",
        "prompt": "",
        "command": "",
        "model": "",
        "max_tokens": None,
    }


def test_write_summarizer_config_rejects_unknown_and_unwired_sources(tmp_config_dir):
    with pytest.raises(ValueError):
        text.write_summarizer_config({"source": "bogus"})
    # "api" exists in the UI as a disabled button but is unwired (#85): an
    # unwired DEFAULT would break the end-of-meeting pipeline with no
    # operator in the loop, so reject it at write time until it lands.
    with pytest.raises(ValueError):
        text.write_summarizer_config({"source": "api"})
    assert text.read_summarizer_config()["source"] == ""


def test_write_summarizer_config_rejects_non_catalog_model(tmp_config_dir, monkeypatch):
    from tapscribe.summarizers import catalog

    monkeypatch.setattr(catalog, "_resolve_local_backend", lambda: "gguf")
    with pytest.raises(ValueError):
        text.write_summarizer_config({"model": "evil/not-in-catalog"})
    assert text.read_summarizer_config()["model"] == ""


def test_write_summarizer_config_accepts_catalog_and_env_override_model(tmp_config_dir, monkeypatch):
    from tapscribe.summarizers import catalog
    from tapscribe.summarizers.catalog import ENV_LOCAL_GGUF_MODEL, LOCAL_GGUF_MODEL

    monkeypatch.setattr(catalog, "_resolve_local_backend", lambda: "gguf")
    assert text.write_summarizer_config({"model": LOCAL_GGUF_MODEL})["model"] == LOCAL_GGUF_MODEL
    # The operator's env override is operator-controlled, not external input.
    monkeypatch.setenv(ENV_LOCAL_GGUF_MODEL, "me/custom-gguf")
    assert text.write_summarizer_config({"model": "me/custom-gguf"})["model"] == "me/custom-gguf"
    assert text.write_summarizer_config({"model": ""})["model"] == ""


def test_write_summarizer_config_rejects_oversize_prompt_and_command(tmp_config_dir):
    too_big = "x" * 5000
    with pytest.raises(ValueError):
        text.write_summarizer_config({"prompt": too_big})
    with pytest.raises(ValueError):
        text.write_summarizer_config({"command": too_big})


def test_write_summarizer_config_bounds_max_tokens(tmp_config_dir):
    with pytest.raises(ValueError):
        text.write_summarizer_config({"max_tokens": 9})
    with pytest.raises(ValueError):
        text.write_summarizer_config({"max_tokens": 9000})
    with pytest.raises(ValueError):
        text.write_summarizer_config({"max_tokens": "lots"})
    assert text.write_summarizer_config({"max_tokens": None})["max_tokens"] is None
    assert text.write_summarizer_config({"max_tokens": 2048})["max_tokens"] == 2048


def test_read_summarizer_config_garbage_json_reads_as_defaults(tmp_config_dir):
    (tmp_config_dir / "summarizer.json").write_text("{not json", encoding="utf-8")
    assert text.read_summarizer_config()["source"] == ""
    (tmp_config_dir / "summarizer.json").write_text('["a", "list"]', encoding="utf-8")
    assert text.read_summarizer_config() == {
        "source": "",
        "prompt": "",
        "command": "",
        "model": "",
        "max_tokens": None,
    }


def test_summarizer_default_public_exposes_exactly_the_public_fields(tmp_config_dir):
    """The state-poll filter is the redaction seam: when #85 adds API-key
    fields to summarizer.json they must NOT appear here. Pin the exact key
    set so adding a field to the blob is a deliberate act."""
    text.write_summarizer_config({"source": "local", "prompt": "P"})
    blob = text.summarizer_default_public(text.read_summarizer_config())
    assert set(blob.keys()) == {"source", "prompt", "command", "model", "max_tokens"}
    assert blob["source"] == "local"
    # A future secret-ish key on the stored dict is dropped, not forwarded.
    assert "api_key" not in text.summarizer_default_public({**blob, "api_key": "s3cret"})
