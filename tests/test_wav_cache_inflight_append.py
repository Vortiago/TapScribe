"""RED contract for issue #197 — cached_transcribe must not certify a truncated
transcript when the WAV is appended-to DURING the transcribe.

cached_transcribe fingerprints the WAV before the cache check, runs the model,
then (buggy) re-stats AFTER transcribe and stores THAT later fingerprint. If the
WAV grew while the model was reading it (an in-flight /tap utterance append), the
sidecar records the FINAL size/mtime for a transcript that only covers audio up
to the model's read point — every later `force=False` call then matches the
fingerprint and serves the truncated transcript indefinitely.

The fix fingerprints stat-before-read and stores THAT (mirroring the
`strip_one_wav` pattern in batch_strip.py:81-88), optionally re-statting after
and forcing a mismatch (store zeros) when the two differ. Both admissible fixes
produce the SAME harm-layer behaviour, and that behaviour is what these tests
pin — asserted at the `cached_transcribe` boundary (does the NEXT `force=False`
call re-transcribe or serve cache?), NEVER the stored fingerprint value, so both
fixes pass.
"""

from __future__ import annotations

from pathlib import Path

from wav_builders import seed_wav  # type: ignore[import-not-found]

from tapscribe.transcribers.base import TranscriptionResult, TranscriptionSegment
from tapscribe.wav_cache import cached_transcribe, read_cached


class _CountingTranscriber:
    """Counts its calls and returns per-call text (`transcript-<n>`). When
    `append_bytes` is set, each transcribe appends raw bytes to the WAV as a
    side effect — simulating a live /tap utterance growing the file WHILE the
    model reads it (the interleaving issue #197 describes). Nothing on the
    `cached_transcribe` path parses the WAV (only `stat()` in `_wav_fingerprint`),
    so raw bytes are a faithful, deterministic stand-in for appended audio."""

    name = "fake"
    backend = "fake-backend"
    device = "test-device"
    model_name = "fake-model"

    def __init__(self, *, append_bytes: int = 0) -> None:
        self.call_count = 0
        self._append_bytes = append_bytes

    def transcribe(self, path, *, initial_prompt=None, hotwords=None, source_lang=None):  # noqa: ARG002
        self.call_count += 1
        if self._append_bytes:
            with Path(path).open("ab") as fh:
                fh.write(b"\x00" * self._append_bytes)
        text = f"transcript-{self.call_count}"
        return TranscriptionResult(
            transcriber=self.name,
            backend=self.backend,
            device=self.device,
            model=self.model_name,
            language="en",
            language_probability=1.0,
            duration=1.0,
            text=text,
            segments=(TranscriptionSegment(start=0.0, end=1.0, text=text),),
            initial_prompt_used=initial_prompt or "",
            hotwords_used=hotwords or "",
            quality_settings={},
        )


def test_inflight_append_during_transcribe_is_not_certified(tmp_path: Path):
    """Harm case: the WAV grows DURING the first transcribe (an in-flight
    append), so the transcript only covers audio up to the model's read point.
    A later `force=False` call MUST re-transcribe (cache miss) rather than serve
    the truncated transcript — otherwise the stale result is certified forever.
    Asserted at the boundary (call_count / returned text), not the fingerprint,
    so both the stat-before-read fix and the force-mismatch fix pass."""
    wav = seed_wav(tmp_path / "utterance.wav")
    transcriber = _CountingTranscriber(append_bytes=4096)

    first = cached_transcribe(wav, transcriber, initial_prompt=None, hotwords=None, hallucination_rules=[])
    assert transcriber.call_count == 1
    assert first.result.text == "transcript-1"

    # The file is now larger than what the model actually transcribed. A cached
    # read keyed off the pre-read fingerprint (or a forced mismatch) must MISS,
    # so this call re-runs the model instead of certifying the truncated text.
    second = cached_transcribe(wav, transcriber, initial_prompt=None, hotwords=None, hallucination_rules=[])
    assert transcriber.call_count == 2, (
        "cached_transcribe served the truncated transcript for a WAV that grew "
        "during transcribe — the post-read fingerprint certified stale audio"
    )
    assert second.result.text == "transcript-2"
    re_read = read_cached(wav)
    assert re_read is not None
    assert re_read.result.text == "transcript-2"


def test_stable_wav_still_hits_cache_on_second_call(tmp_path: Path):
    """Guardrail case: with NO in-flight append, the second `force=False` call
    MUST hit the cache (transcriber not re-run). Without this pin, a degenerate
    'fix' that just disables caching (always store zeros, never match) would
    pass the harm case while breaking the cache entirely — this distinguishes a
    real fix from a broken one."""
    wav = seed_wav(tmp_path / "utterance.wav")
    transcriber = _CountingTranscriber()

    first = cached_transcribe(wav, transcriber, initial_prompt=None, hotwords=None, hallucination_rules=[])
    assert transcriber.call_count == 1
    assert first.result.text == "transcript-1"

    second = cached_transcribe(wav, transcriber, initial_prompt=None, hotwords=None, hallucination_rules=[])
    assert transcriber.call_count == 1, (
        "second call re-transcribed a byte-identical WAV — the cache no longer hits"
    )
    assert second.result.text == "transcript-1"
