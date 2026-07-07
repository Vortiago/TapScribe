"""RED contract for issue #241 — `transcribe_session_locked` writes the
merged `session-transcript.json` / `.txt` (tapscribe/batch_transcribe.py)
with plain `Path.write_text`, which truncates the target the instant it
opens it in write mode. A crash between that open and the write completing
corrupts the merged session transcript on disk — and, read on the
`/api/state` poll's worker thread, can transiently (or, if the
stat-signature cache memoises the failed read, permanently) make the
dashboard think the session was never transcribed.

The fix is to route these writes through `tapscribe.text.atomic_write_text`
(tempfile + `os.replace`), which never opens the real target path directly
— a crash mid-write leaves it completely untouched. See
`test_wav_cache_atomic_writes.py` for the wav_cache.py half of this issue
and the fault-injection helper this file duplicates (kept self-contained
per test file, matching this suite's existing per-file stub convention).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import TranscriberStub  # type: ignore[import-not-found]
from wav_builders import seed_session  # type: ignore[import-not-found]

from tapscribe.batch_transcribe import BatchSessionRequest, transcribe_session_locked
from tapscribe.session_merge import select_session_wavs
from tapscribe.session_paths import FILENAME_TRANSCRIPT_JSON, FILENAME_TRANSCRIPT_TXT
from tapscribe.text import write_languages

SESSION_WAVS = [
    "2026-01-01T01-00-00Z__alice__a.wav",
    "2026-01-01T01-00-05Z__alice__b.wav",
]


def _crash_on_direct_open(monkeypatch: pytest.MonkeyPatch, target: Path) -> dict:
    """Patch `Path.open` so the FIRST direct write-mode open of `target`
    writes half its content to disk, then raises `OSError` — the exact
    failure shape of a crash mid `Path.write_text`. A write instead routed
    through tempfile + `os.replace` never opens `target` directly (it opens
    a sibling tempfile via `os.fdopen`), so it never trips this fault and
    `state["fired"]` stays False."""
    state: dict = {"fired": False, "half": None}
    real_open = Path.open

    def patched_open(self, mode="r", *a, **kw):
        fh = real_open(self, mode, *a, **kw)
        if not state["fired"] and self == target and "w" in mode:
            real_write = fh.write

            def crash_write(s, *aa, **kw2):  # noqa: ARG001
                half = s[: len(s) // 2]
                real_write(half)
                fh.flush()
                state["fired"] = True
                state["half"] = half
                raise OSError("simulated crash mid-write (disk full)")

            fh.write = crash_write
        return fh

    monkeypatch.setattr(Path, "open", patched_open)
    return state


def _assert_never_torn(target: Path, state: dict) -> None:
    if target.exists() and state["half"] is not None:
        content = target.read_text(encoding="utf-8")
        assert content != state["half"], (
            f"{target} was left holding a torn/truncated write after a simulated crash mid-write"
        )


def _session_request() -> BatchSessionRequest:
    return BatchSessionRequest(
        session="s",
        source="original",
        model="fake-m",
        backend="cpu",
        from_iso=None,
        to_iso=None,
        force=False,
        source_lang=None,
        target_lang=None,
    )


async def test_session_transcript_json_survives_a_crash_mid_write(recorder_under_test, monkeypatch):
    """The merged session-transcript.json write (the write named in issue
    #241) must not end up truncated by a crash."""
    write_languages("en")
    monkeypatch.setattr(
        "tapscribe.batch_transcribe.load_transcriber",
        lambda *a, **kw: TranscriberStub(backend="fake-be", model="fake-m", text="merged"),  # noqa: ARG005
    )
    sd = seed_session(recorder_under_test.recordings_dir, "s", SESSION_WAVS)
    selection = select_session_wavs(sd, from_iso=None, to_iso=None, source="original")

    target = sd / FILENAME_TRANSCRIPT_JSON
    state = _crash_on_direct_open(monkeypatch, target)

    async with recorder_under_test.jobs.run("s", kind="transcribe", total=len(SESSION_WAVS)) as handle:
        try:
            await transcribe_session_locked(_session_request(), selection=selection, job=handle)
        except OSError:
            pass

    _assert_never_torn(target, state)


async def test_session_transcript_txt_survives_a_crash_mid_write(recorder_under_test, monkeypatch):
    """The companion plain-text session-transcript.txt write must not end
    up truncated by a crash either."""
    write_languages("en")
    monkeypatch.setattr(
        "tapscribe.batch_transcribe.load_transcriber",
        lambda *a, **kw: TranscriberStub(backend="fake-be", model="fake-m", text="merged"),  # noqa: ARG005
    )
    sd = seed_session(recorder_under_test.recordings_dir, "s", SESSION_WAVS)
    selection = select_session_wavs(sd, from_iso=None, to_iso=None, source="original")

    target = sd / FILENAME_TRANSCRIPT_TXT
    state = _crash_on_direct_open(monkeypatch, target)

    async with recorder_under_test.jobs.run("s", kind="transcribe", total=len(SESSION_WAVS)) as handle:
        try:
            await transcribe_session_locked(_session_request(), selection=selection, job=handle)
        except OSError:
            pass

    _assert_never_torn(target, state)
