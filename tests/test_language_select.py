"""Tests for `tapscribe.language_select` — the per-region selector seam.

ADR-0010 slice 2: when the cover runs ≥2 models on a region, a pluggable
`LanguageSelector` picks the winning transcript, which becomes that WAV's
`_primary` sidecar. The default is `SpecialistRoutingSelector` (route by the
generalist's detected language); `AcousticConfidenceSelector` is a non-default
same-family seam alternative. These tests pin the selection LOGIC
deterministically; the real-model wiring is proven in the e2e suite.
"""

from __future__ import annotations

from datetime import UTC, datetime

from tapscribe.language_select import (
    AcousticConfidenceSelector,
    LanguageSelector,
    SpecialistRoutingSelector,
    default_language_selector,
)
from tapscribe.transcribers.base import TranscriptionResult, TranscriptionSegment
from tapscribe.wav_cache import CachedTranscription


def _candidate(
    model: str, segs: list[tuple[float, float, float | None]], *, language: str = "no"
) -> CachedTranscription:
    """A CachedTranscription carrying `model`, the per-region detected `language`,
    and the given (start, end, avg_logprob) segments — the fields the selectors read."""
    segments = tuple(TranscriptionSegment(start=s, end=e, text="x", avg_logprob=lp) for s, e, lp in segs)
    result = TranscriptionResult(
        transcriber="fake",
        backend="faster-whisper",
        device="CPU",
        model=model,
        language=language,
        language_probability=1.0,
        duration=segs[-1][1] if segs else 0.0,
        text="x",
        segments=segments,
        initial_prompt_used="",
        hotwords_used="",
        quality_settings={},
    )
    return CachedTranscription(
        result=result,
        transcribed_at=datetime(2026, 1, 1, tzinfo=UTC),
        transcribe_ms=0,
        source="stripped",
        wav_start=None,
        speaker_name="",
    )


def test_default_selector_is_specialist_routing():
    sel = default_language_selector()
    assert isinstance(sel, SpecialistRoutingSelector)
    assert isinstance(sel, LanguageSelector)


# ---------------------------------------------------------------------------
# SpecialistRoutingSelector (the default): route a region to the model the
# specialist table names for the generalist's detected language; else acoustic.
# ---------------------------------------------------------------------------


def test_routes_to_the_specialist_for_the_detected_language_over_a_more_confident_generalist():
    """A region the generalist detected as Norwegian goes to nb-whisper (the "no"
    specialist) even though the generalist is acoustically MORE confident — the
    specialist table declares nb-whisper is the best model for Norwegian."""
    from tapscribe.transcribers.catalog import SPECIALIST_MODELS

    nb = SPECIALIST_MODELS["no"]
    generalist = _candidate("large-v3-turbo", [(0.0, 5.0, -0.10)], language="no")  # higher confidence
    specialist = _candidate(nb, [(0.0, 5.0, -0.40)], language="no")  # lower confidence
    winner = SpecialistRoutingSelector().select([generalist, specialist])
    assert winner.result.model == nb


def test_keeps_the_generalist_when_a_more_confident_wrong_language_specialist_runs():
    """The bug the real-audio tests caught: an English region (detected 'en', no
    English specialist) where nb-whisper transcribed the audio as Norwegian with
    HIGHER avg_logprob than the generalist's correct English. The selector must
    keep the generalist — a specialist may win ONLY a region that IS its language,
    never out-confidence its way onto English/Danish audio."""
    from tapscribe.transcribers.catalog import SPECIALIST_MODELS

    nb = SPECIALIST_MODELS["no"]
    generalist = _candidate("tiny.en", [(0.0, 5.0, -0.50)], language="en")  # correct English, less confident
    specialist = _candidate(nb, [(0.0, 5.0, -0.10)], language="no")  # confident Norwegian on English audio
    winner = SpecialistRoutingSelector().select([generalist, specialist])
    assert winner.result.model == "tiny.en"


def test_specialist_routing_keeps_an_unscored_generalist_on_a_no_specialist_language():
    """Cross-architecture: an unscored Parakeet/Voxtral generalist on a
    no-specialist language (English) stays the winner — keeping the generalist
    (rather than acoustic-comparing) subsumes the cross-arch guard."""
    from tapscribe.transcribers.catalog import SPECIALIST_MODELS

    nb = SPECIALIST_MODELS["no"]
    generalist = _candidate("parakeet-tdt-0.6b-v3", [(0.0, 6.0, None)], language="en")  # unscored
    specialist = _candidate(nb, [(0.0, 6.0, -0.30)], language="no")  # scored
    winner = SpecialistRoutingSelector().select([generalist, specialist])
    assert winner.result.model == "parakeet-tdt-0.6b-v3"


def test_picks_the_more_confident_transcript():
    """Higher (closer to zero) avg_logprob wins — the specialist here is more
    confident, so it is selected over the generalist."""
    generalist = _candidate("large-v3-turbo", [(0.0, 5.0, -0.80)])
    specialist = _candidate("nb-whisper-large", [(0.0, 5.0, -0.20)])
    winner = AcousticConfidenceSelector().select([generalist, specialist])
    assert winner.result.model == "nb-whisper-large"


def test_generalist_first_wins_a_tie():
    """When two transcripts score equally (here neither carries avg_logprob),
    the FIRST candidate wins. The pipeline passes the generalist first, so the
    generalist is the safe tie-break default (ADR-0010)."""
    generalist = _candidate("large-v3-turbo", [(0.0, 5.0, None)])
    specialist = _candidate("nb-whisper-large", [(0.0, 5.0, None)])
    winner = AcousticConfidenceSelector().select([generalist, specialist])
    assert winner.result.model == "large-v3-turbo"


def test_score_is_duration_weighted_not_a_plain_segment_mean():
    """A transcript that is confident across almost all of its duration must
    beat a uniformly-mediocre one, even if a single tiny segment drags its
    PLAIN-mean logprob down. `specialist` is great for 9.8 s and terrible for
    0.2 s; `generalist` is uniformly -0.4. Plain-mean would pick the generalist
    (specialist mean ≈ -2.6); duration-weighting picks the specialist
    (≈ -0.3), which is the acoustically-better transcript over the clip."""
    generalist = _candidate("large-v3-turbo", [(0.0, 10.0, -0.40)])
    specialist = _candidate("nb-whisper-large", [(0.0, 9.8, -0.20), (9.8, 10.0, -5.00)])
    winner = AcousticConfidenceSelector().select([generalist, specialist])
    assert winner.result.model == "nb-whisper-large"


def test_segment_without_logprob_does_not_sink_an_otherwise_confident_transcript():
    """Segments lacking avg_logprob are skipped, not treated as zero/-inf: a
    transcript with one scored confident segment and one unscored segment is
    still ranked on the score it does have."""
    scored = _candidate("nb-whisper-large", [(0.0, 5.0, -0.10), (5.0, 10.0, None)])
    weak = _candidate("large-v3-turbo", [(0.0, 10.0, -0.90)])
    winner = AcousticConfidenceSelector().select([weak, scored])
    assert winner.result.model == "nb-whisper-large"


def test_all_unscored_keeps_the_first_candidate():
    """No transcript carries any avg_logprob → every score is equal, so the
    first (generalist) candidate is kept. Selection never raises just because
    a backend didn't emit confidences."""
    a = _candidate("large-v3-turbo", [(0.0, 5.0, None)])
    b = _candidate("nb-whisper-large", [])
    winner = AcousticConfidenceSelector().select([a, b])
    assert winner.result.model == "large-v3-turbo"


def test_unscored_generalist_is_kept_over_a_scored_specialist():
    """Cross-architecture safety: a generalist whose backend emits NO avg_logprob
    (Parakeet / Voxtral) scores at the floor on every region. Comparing it against
    a fully-scored nb-whisper would hand EVERY region to nb-whisper — including the
    English / Danish ones the generalist transcribed correctly, which nb-whisper
    re-renders as Norwegian. When any candidate is unscored the scores aren't
    comparable, so the generalist (first) is kept; cross-arch routing is the
    text-LID selector's job (ADR-0010)."""
    generalist = _candidate("parakeet-tdt-0.6b-v3", [(0.0, 6.0, None), (6.0, 12.0, None)])
    specialist = _candidate("nb-whisper-large", [(0.0, 12.0, -0.30)])
    winner = AcousticConfidenceSelector().select([generalist, specialist])
    assert winner.result.model == "parakeet-tdt-0.6b-v3"


def test_select_carries_candidate_languages_for_the_seam():
    """The selector signature carries the meeting's declared set so a future
    constrained text-LID selector is a true drop-in (ADR-0010 'swap with no
    pipeline change'); the default acoustic selector accepts and ignores it."""
    generalist = _candidate("large-v3-turbo", [(0.0, 5.0, -0.50)])
    specialist = _candidate("nb-whisper-large", [(0.0, 5.0, -0.10)])
    winner = AcousticConfidenceSelector().select([generalist, specialist], candidate_languages=("no", "en"))
    assert winner.result.model == "nb-whisper-large"
