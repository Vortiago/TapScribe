"""Direct tests for tapscribe.batch_summarize.

Same shape as test_batch_strip / test_batch_transcribe: the orchestration
(merged-transcript read, JobTracker claim/release, domain errors) is testable
WITHOUT an HTTP TestClient — `summarize_session` is called straight against a
tmpdir-rooted Recorder. The summarizer is a real, deterministic `python -c`
subprocess (cross-platform), so no model or endpoint is touched.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from conftest import py_cmd, seed_merged_transcript  # type: ignore[import-not-found]

from tapscribe import text
from tapscribe.batch_summarize import (
    NoMergedTranscript,
    SummarizeSessionRequest,
    effective_summarizer_config,
    summarize_session,
    summarize_session_locked,
)
from tapscribe.recorder import JobState, SessionBusy
from tapscribe.sessions import read_session_summary, read_session_transcript, write_session_meta
from tapscribe.summarizers import DEFAULT_SUMMARY_PROMPT, SummarizerFailed, load_summarizer

# stdin → stdout: the summary is the merged transcript text echoed back, so we
# can assert the orchestrator handed the right text to the summarizer.
_CAT = py_cmd("import sys; sys.stdout.write(sys.stdin.read())")


async def test_summarize_session_returns_summary_and_releases_slot(recorder_under_test):
    """Happy path: the merged transcript reaches the command on stdin, stdout
    becomes the summary, and the JobTracker slot is free again afterwards."""
    seed_merged_transcript(recorder_under_test.recordings_dir, "s", plain_text="decided to ship the thing")

    out = await summarize_session(
        recorder_under_test,
        SummarizeSessionRequest(session="s", source="command", command=_CAT, prompt=""),
    )

    assert out["ok"] is True
    assert out["session"] == "s"
    assert out["source"] == "command"
    assert out["summary"] == "decided to ship the thing"
    assert out["command"] == _CAT
    # Slot released → a second summarize succeeds.
    assert recorder_under_test.jobs.get("s") is None
    out2 = await summarize_session(
        recorder_under_test,
        SummarizeSessionRequest(session="s", source="command", command=_CAT, prompt=""),
    )
    assert out2["ok"] is True


async def test_summarize_session_uses_summarize_job_kind(recorder_under_test, monkeypatch):
    """The claimed slot must carry kind='summarize' so the dashboard's shared
    progress bar can label it. We snapshot the live JobState from inside the
    summarizer run (the only window the slot is held)."""
    seed_merged_transcript(recorder_under_test.recordings_dir, "s")
    seen: dict = {}

    real_claim = recorder_under_test.jobs.claim

    async def _spy_claim(state: JobState):
        ok = await real_claim(state)
        if ok:
            seen["kind"] = state.kind
            seen["status"] = state.status
        return ok

    monkeypatch.setattr(recorder_under_test.jobs, "claim", _spy_claim)

    await summarize_session(
        recorder_under_test,
        SummarizeSessionRequest(session="s", source="command", command=_CAT, prompt=""),
    )
    assert seen.get("kind") == "summarize"
    assert seen.get("status") == "summarizing"


async def test_summarize_session_no_merged_transcript_raises(recorder_under_test):
    (recorder_under_test.recordings_dir / "empty").mkdir()
    with pytest.raises(NoMergedTranscript):
        await summarize_session(
            recorder_under_test,
            SummarizeSessionRequest(session="empty", source="command", command=_CAT),
        )
    # Nothing was claimed.
    assert recorder_under_test.jobs.get("empty") is None


async def test_summarize_session_empty_plain_text_raises_no_transcript(recorder_under_test):
    """A merged transcript whose plain_text is blank is as good as none — the
    operator gets the same 'transcribe first' signal, not an empty summary."""
    seed_merged_transcript(recorder_under_test.recordings_dir, "s", plain_text="   \n  ")
    with pytest.raises(NoMergedTranscript):
        await summarize_session(
            recorder_under_test,
            SummarizeSessionRequest(session="s", source="command", command=_CAT),
        )


async def test_summarize_session_raises_busy_and_leaves_foreign_claim_alone(recorder_under_test):
    """When another job holds the session's slot, summarize_session must raise
    SessionBusy WITHOUT releasing the foreign claim on its way out."""
    seed_merged_transcript(recorder_under_test.recordings_dir, "s")
    claimed = await recorder_under_test.jobs.claim(
        JobState(
            session="s", kind="transcribe", current=0, total=1, started_at=datetime.now(UTC), status="running"
        )
    )
    assert claimed

    with pytest.raises(SessionBusy):
        await summarize_session(
            recorder_under_test,
            SummarizeSessionRequest(session="s", source="command", command=_CAT),
        )

    # The pre-existing transcribe claim must still be in place.
    assert recorder_under_test.jobs.snapshot()["s"].kind == "transcribe"


async def test_summarize_session_releases_slot_on_summarizer_failure(recorder_under_test):
    """A summarizer failure (non-zero exit) must still release the slot via the
    finally — a failed summarize can't wedge the session."""
    seed_merged_transcript(recorder_under_test.recordings_dir, "s")
    failing = py_cmd("import sys; sys.exit(1)")
    with pytest.raises(SummarizerFailed):
        await summarize_session(
            recorder_under_test,
            SummarizeSessionRequest(session="s", source="command", command=failing, prompt=""),
        )
    assert recorder_under_test.jobs.get("s") is None


# ---------------------------------------------------------------------------
# Persistence (#83): the summary survives alongside the session
# ---------------------------------------------------------------------------


async def test_summarize_session_persists_summary_file(recorder_under_test):
    """#83: a completed summary is written next to the merged transcript
    (session-summary.json) so it survives a dashboard reload. The persisted
    body is the result mapping plus a `summarized_at` stamp — the same stamp
    the returned dict carries, so the view and the listing marker agree."""
    seed_merged_transcript(recorder_under_test.recordings_dir, "s", plain_text="decided to ship the thing")

    out = await summarize_session(
        recorder_under_test,
        SummarizeSessionRequest(session="s", source="command", command=_CAT, prompt=""),
    )

    assert (recorder_under_test.recordings_dir / "s" / "session-summary.json").is_file()
    stored = read_session_summary("s")
    assert stored is not None
    assert stored["summary"] == "decided to ship the thing"
    assert stored["source"] == "command"
    assert stored["command"] == _CAT
    assert stored["summarized_at"]
    assert out["summarized_at"] == stored["summarized_at"]


async def test_summarize_session_persists_transcript_stamp(recorder_under_test):
    """#94: the persisted summary carries the source transcript's `transcribed_at`
    so the Summary view can detect a summary that predates a later re-transcribe."""
    seed_merged_transcript(recorder_under_test.recordings_dir, "s", plain_text="shipped")

    out = await summarize_session(
        recorder_under_test,
        SummarizeSessionRequest(session="s", source="command", command=_CAT, prompt=""),
    )

    assert out["transcribed_at"] == "2026-01-01T00:00:00+00:00"
    stored = read_session_summary("s")
    assert stored["transcribed_at"] == "2026-01-01T00:00:00+00:00"


async def test_summarize_session_regenerate_replaces_summary(recorder_under_test):
    """#83: one current summary per session — re-generating replaces the
    stored summary, it doesn't accumulate history."""
    seed_merged_transcript(recorder_under_test.recordings_dir, "s", plain_text="first take")
    await summarize_session(
        recorder_under_test,
        SummarizeSessionRequest(session="s", source="command", command=_CAT, prompt=""),
    )
    regenerated = py_cmd("import sys; sys.stdin.read(); sys.stdout.write('REGENERATED')")
    await summarize_session(
        recorder_under_test,
        SummarizeSessionRequest(session="s", source="command", command=regenerated, prompt=""),
    )
    stored = read_session_summary("s")
    assert stored is not None
    assert stored["summary"] == "REGENERATED"


async def test_summarize_session_locked_persists_under_caller_slot(recorder_under_test):
    """The end-of-meeting pipeline drives the summarize core directly under
    its own `kind="pipeline"` claim: the core must run the summarizer and
    persist session-summary.json without claiming or releasing the slot."""
    seed_merged_transcript(recorder_under_test.recordings_dir, "s", plain_text="ship the thing")
    claimed = await recorder_under_test.jobs.claim(
        JobState(
            session="s", kind="pipeline", current=0, total=1, started_at=datetime.now(UTC), status="running"
        )
    )
    assert claimed

    req = SummarizeSessionRequest(session="s", source="command", command=_CAT, prompt="")
    summarizer = load_summarizer(source=req.source, command=req.command, model=req.model, max_tokens=None)
    merged = read_session_transcript("s")
    persisted = await summarize_session_locked(req, summarizer=summarizer, merged=merged)

    assert persisted["summary"] == "ship the thing"
    stored = read_session_summary("s")
    assert stored is not None and stored["summary"] == "ship the thing"
    assert stored["summarized_at"] == persisted["summarized_at"]
    # The caller's pipeline claim is untouched.
    held = recorder_under_test.jobs.get("s")
    assert held is not None and held.kind == "pipeline"


async def test_summarize_session_failure_keeps_previous_summary(recorder_under_test):
    """#83: a failed re-generate must NOT clobber the stored summary — the
    write happens only after the summarizer succeeds."""
    seed_merged_transcript(recorder_under_test.recordings_dir, "s", plain_text="the good take")
    await summarize_session(
        recorder_under_test,
        SummarizeSessionRequest(session="s", source="command", command=_CAT, prompt=""),
    )
    failing = py_cmd("import sys; sys.exit(1)")
    with pytest.raises(SummarizerFailed):
        await summarize_session(
            recorder_under_test,
            SummarizeSessionRequest(session="s", source="command", command=failing, prompt=""),
        )
    stored = read_session_summary("s")
    assert stored is not None
    assert stored["summary"] == "the good take"


# ---------------------------------------------------------------------------
# effective_summarizer_config (#84) — session-meta override (source + prompt)
# over the global summarizer default over built-ins. The same chain shape as
# batch_transcribe's prompt/hotwords resolution.
# ---------------------------------------------------------------------------


def test_effective_summarizer_config_override_beats_global(recorder_under_test):
    text.write_summarizer_config(
        {"source": "local", "prompt": "GLOBAL", "command": "claude -p", "max_tokens": 512}
    )
    write_session_meta("s", {"summary_source": "command", "summary_prompt": "SESSION"})

    cfg = effective_summarizer_config("s")
    assert cfg["source"] == "command"
    assert cfg["prompt"] == "SESSION"
    # Per-source fields stay global-only — the override is source + prompt by design.
    assert cfg["command"] == "claude -p"
    assert cfg["max_tokens"] == 512


def test_effective_summarizer_config_empty_override_falls_back_to_global(recorder_under_test):
    """An empty-string override is "no override" — the acceptance criterion's
    empty-falls-back. (Inherited limitation of the chain shape: a session
    cannot assert "no prompt", same as the prompt/hotwords resolution.)"""
    text.write_summarizer_config({"source": "command", "prompt": "GLOBAL", "command": "claude -p"})
    write_session_meta("s", {"summary_source": "", "summary_prompt": "  "})

    cfg = effective_summarizer_config("s")
    assert cfg["source"] == "command"
    assert cfg["prompt"] == "GLOBAL"


def test_effective_summarizer_config_built_ins_when_nothing_configured(recorder_under_test):
    """No meta, no global file: the bundled offline source + default prompt —
    exactly what the pre-#84 pipeline hardcoded."""
    cfg = effective_summarizer_config("never-seen")
    assert cfg == {
        "source": "local",
        "prompt": DEFAULT_SUMMARY_PROMPT,
        "command": "",
        "model": "",
        "max_tokens": None,
    }


def test_effective_summarizer_config_override_is_source_and_prompt_only(recorder_under_test):
    """A session-meta file can't smuggle per-source fields past the global
    layer — command/model/max_tokens resolve from the global default even
    when stray keys exist in session-meta.json."""
    text.write_summarizer_config({"source": "local", "model": ""})
    (recorder_under_test.recordings_dir / "s").mkdir(exist_ok=True)
    (recorder_under_test.recordings_dir / "s" / "session-meta.json").write_text(
        '{"summary_source": "command", "command": "evil --cmd", "model": "evil/repo"}',
        encoding="utf-8",
    )
    cfg = effective_summarizer_config("s")
    assert cfg["command"] == ""
    assert cfg["model"] == ""
