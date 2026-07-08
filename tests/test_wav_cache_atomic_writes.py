"""RED contract for issue #241 — the multi-transcript cache's sidecar and
`_primary` pointer writes (`_write_entry`, `set_primary_transcript`,
`_migrate_legacy_if_needed` in tapscribe/wav_cache.py) go straight through
`Path.write_text`, which truncates the target the instant it opens it in
write mode. A crash between that open and the write completing (disk-full,
power loss, OOM-kill) leaves the file either empty or holding a
half-written fragment — corrupting the sidecar/pointer, and, read on the
`/api/state` poll's worker thread, either making a transcript transiently
"vanish" from the dashboard or permanently misreading a WAV as
never-transcribed (see `tapscribe/wav_cache.py::_read_entry`, which
swallows the resulting `json.JSONDecodeError` and returns `None`).

The fix is to route these writes through `tapscribe.text.atomic_write_text`
(tempfile + `os.replace`), which never opens the real target path directly
— a crash mid-write leaves it completely untouched. See
`test_batch_transcribe_atomic_writes.py` for the batch_transcribe.py half
of this issue, sharing the same fault-injection shape.

Each test below simulates that crash — a hard fault raised the instant the
target path is opened in write mode, after only half the intended content
has hit disk — and asserts the target is never left holding that torn
fragment. A write that never opens the target directly (the fix) never
trips the fault at all, and is left with the full, valid content.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from wav_builders import seed_wav  # type: ignore[import-not-found]

from tapscribe.transcribers.base import TranscriptionResult, TranscriptionSegment
from tapscribe.wav_cache import cached_transcribe, set_primary_transcript

_PRIMARY_POINTER = "_primary"


class _StubByKey:
    """A stub Transcriber parameterized by (backend, model) — same shape as
    test_wav_cache.py's helper of the same name."""

    name = "fake"
    device = "test-device"

    def __init__(self, *, backend: str, model: str, text: str = "hello") -> None:
        self.backend = backend
        self.model_name = model
        self.text = text

    def transcribe(self, path, *, initial_prompt=None, hotwords=None, source_lang=None):  # noqa: ARG002
        return TranscriptionResult(
            transcriber=self.name,
            backend=self.backend,
            device=self.device,
            model=self.model_name,
            language="en",
            language_probability=1.0,
            duration=1.0,
            text=self.text,
            segments=(TranscriptionSegment(start=0.0, end=1.0, text=self.text),),
            initial_prompt_used="",
            hotwords_used="",
            quality_settings={},
        )


def _transcripts_dir(wav_path: Path) -> Path:
    return wav_path.with_suffix(".transcripts")


def _entry_key(backend: str, model: str) -> str:
    return f"{backend}__{model}"


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


def test_sidecar_json_survives_a_crash_mid_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """_write_entry's per-(backend, model) sidecar JSON write (the write
    named in issue #241) must not end up truncated by a crash."""
    wav = seed_wav(tmp_path / "x.wav")
    target = _transcripts_dir(wav) / f"{_entry_key('faster-whisper', 'small.en')}.json"
    state = _crash_on_direct_open(monkeypatch, target)

    try:
        cached_transcribe(
            wav,
            _StubByKey(backend="faster-whisper", model="small.en"),
            initial_prompt=None,
            hotwords=None,
            hallucination_rules=[],
        )
    except OSError:
        # Expected on the still-buggy code (the fault fired); the fixed
        # code never opens `target` directly, so no exception is also a
        # valid outcome here — _assert_never_torn is the real assertion.
        pass

    _assert_never_torn(target, state)


def test_primary_pointer_survives_a_crash_mid_write_on_first_transcribe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """_write_entry's `_primary` pointer write must not end up truncated by
    a crash — a torn pointer can misdirect (or blank) the merge layer."""
    wav = seed_wav(tmp_path / "x.wav")
    target = _transcripts_dir(wav) / _PRIMARY_POINTER
    state = _crash_on_direct_open(monkeypatch, target)

    try:
        cached_transcribe(
            wav,
            _StubByKey(backend="faster-whisper", model="small.en"),
            initial_prompt=None,
            hotwords=None,
            hallucination_rules=[],
        )
    except OSError:
        # Expected on the still-buggy code (the fault fired); the fixed
        # code never opens `target` directly, so no exception is also a
        # valid outcome here — _assert_never_torn is the real assertion.
        pass

    _assert_never_torn(target, state)


def test_set_primary_transcript_pointer_survives_a_crash_mid_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The operator-triggered `set_primary_transcript` repoint
    (wav_cache.py:263) writes the same `_primary` pointer with the same
    crash-unsafe call — it must not end up truncated either."""
    wav = seed_wav(tmp_path / "x.wav")
    cached_transcribe(
        wav,
        _StubByKey(backend="faster-whisper", model="small.en"),
        initial_prompt=None,
        hotwords=None,
        hallucination_rules=[],
    )
    cached_transcribe(
        wav,
        _StubByKey(backend="mlx-voxtral", model="voxtral-mini"),
        initial_prompt=None,
        hotwords=None,
        hallucination_rules=[],
    )

    target = _transcripts_dir(wav) / _PRIMARY_POINTER
    state = _crash_on_direct_open(monkeypatch, target)

    try:
        set_primary_transcript(wav, backend="faster-whisper", model="small.en")
    except OSError:
        # Expected on the still-buggy code (the fault fired); the fixed
        # code never opens `target` directly, so no exception is also a
        # valid outcome here — _assert_never_torn is the real assertion.
        pass

    _assert_never_torn(target, state)


def test_legacy_migration_primary_pointer_survives_a_crash_mid_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The legacy-sidecar migration (wav_cache.py:504, inside
    `_migrate_legacy_if_needed`) writes the freshly-created `_primary`
    pointer with the same crash-unsafe call — it must not end up truncated
    either, even though the migrated sidecar itself (a plain `Path.replace`
    rename, already atomic) completes fine first."""
    wav = seed_wav(tmp_path / "x.wav")
    st = wav.stat()
    legacy = {
        "transcriber": "fake",
        "backend": "faster-whisper",
        "device": "cpu",
        "model": "small.en",
        "language": "en",
        "language_probability": 1.0,
        "duration": 1.0,
        "segments": [],
        "text": "from legacy file",
        "initial_prompt_used": "",
        "hotwords_used": "",
        "quality_settings": {},
        "suppressed_hallucinations": [],
        "transcribed_at": "2026-05-01T00:00:00+00:00",
        "transcribe_ms": 10,
        "source": "original",
        "speaker_name": "",
        "wav_size": st.st_size,
        "wav_mtime_ns": st.st_mtime_ns,
    }
    wav.with_suffix(".json").write_text(json.dumps(legacy), encoding="utf-8")

    target = _transcripts_dir(wav) / _PRIMARY_POINTER
    state = _crash_on_direct_open(monkeypatch, target)

    try:
        cached_transcribe(
            wav,
            _StubByKey(backend="mlx-voxtral", model="voxtral-mini"),
            initial_prompt=None,
            hotwords=None,
            hallucination_rules=[],
        )
    except OSError:
        # Expected on the still-buggy code (the fault fired); the fixed
        # code never opens `target` directly, so no exception is also a
        # valid outcome here — _assert_never_torn is the real assertion.
        pass

    _assert_never_torn(target, state)
