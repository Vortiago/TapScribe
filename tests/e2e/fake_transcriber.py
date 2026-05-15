"""A drop-in `Transcriber` that returns scripted text per speaker.

Real Whisper backends aren't installed in CI (and would be slow even if
they were). For the default E2E test path we register this fake via
monkeypatch on `tapscribe.transcribers.load_transcriber` so the
`/api/transcribe-session` endpoint exercises the *whole* pipeline —
WAV-cache sidecar writes, per-segment timestamps, merge ordering, plain
text rendering — with deterministic content the test can assert against.

The text we return is keyed by the speaker slug parsed from the WAV
filename (the same parsing the merge layer does). That way two different
identities streaming in parallel produce distinguishable segments in the
merged transcript.
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from tapscribe.audio import wav_duration_s
from tapscribe.text import parse_wav_speaker_slug
from tapscribe.transcribers.base import TranscriptionResult, TranscriptionSegment


class FakeTranscriber:
    """Returns a single segment per WAV whose text comes from
    `text_by_speaker` keyed by the WAV's speaker slug.

    The slug parsing comes straight from `tapscribe.text.parse_wav_speaker_slug`
    so this mirrors the real merge path's view of who said what.
    """

    name: ClassVar[str] = "fake-whisper"

    def __init__(self, *, text_by_speaker: dict[str, str], model_name: str = "fake-small.en"):
        self.text_by_speaker = text_by_speaker
        self.model_name = model_name
        self.device = "fake-cpu"
        self.calls: list[Path] = []

    def transcribe(
        self,
        path: Path,
        *,
        initial_prompt: str | None = None,
        hotwords: str | None = None,
    ) -> TranscriptionResult:
        self.calls.append(path)
        slug = parse_wav_speaker_slug(path.name)
        text = self.text_by_speaker.get(slug, f"[no scripted text for slug={slug!r}]")
        duration = wav_duration_s(path) or 1.0
        segment = TranscriptionSegment(
            start=0.0,
            end=round(duration, 2),
            text=text,
            avg_logprob=-0.1,
        )
        return TranscriptionResult(
            transcriber=self.name,
            device=self.device,
            model=self.model_name,
            language="en",
            language_probability=1.0,
            duration=round(duration, 2),
            text=text,
            segments=(segment,),
            initial_prompt_used=initial_prompt or "",
            hotwords_used=hotwords or "",
            quality_settings={"fake": True},
        )
