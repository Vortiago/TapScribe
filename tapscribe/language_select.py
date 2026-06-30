"""Per-region transcript selection — the ADR-0010 selector seam.

When the cover (`catalog.cover_models`) runs ≥2 models on a region, each writes
its own `(backend, model)` sidecar into the per-WAV cache and a
`LanguageSelector` picks the winner; the orchestrator points that WAV's
`_primary` at it and `merge_session` stitches the mixed-language transcript.

The default selector is **acoustic confidence** — the duration-weighted mean of
each transcript's per-segment `avg_logprob`. It is valid because the v1 cover
pair (a Whisper generalist + the NB-Whisper Norwegian specialist) is the same
model family, so their log-probabilities are comparable on the same audio. The
selector is a deliberate seam: swapping in a **text-LID** selector — which is
what unlocks a cross-architecture pair like Parakeet + nb-whisper, whose raw
log-probs are NOT comparable — is a one-line change to `default_language_selector`
with no pipeline edit. The choice of heuristic is empirical (ADR-0010), which is
exactly why it lives behind this interface and not as a hardcoded `max`.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from .wav_cache import CachedTranscription

# A transcript that carries no usable confidence sorts below every scored one,
# so a scored rival always wins; when NOTHING is scored, all candidates tie at
# this floor and the first (generalist) is kept.
_NO_CONFIDENCE = float("-inf")


@runtime_checkable
class LanguageSelector(Protocol):
    """Picks the winning transcript for one region from the cover's candidates.

    Implementations must be stable on ties (return the FIRST candidate), because
    the pipeline orders candidates generalist-first and relies on the generalist
    being the tie-break default. `candidate_languages` (the meeting's declared
    set) is carried so a constrained text-LID selector — the ADR-0010 reason this
    is a seam — is a true drop-in; the default acoustic selector ignores it."""

    def select(
        self,
        candidates: Sequence[CachedTranscription],
        *,
        candidate_languages: tuple[str, ...] = (),
    ) -> CachedTranscription: ...


class AcousticConfidenceSelector:
    """Default selector: highest duration-weighted mean `avg_logprob` wins.

    Duration-weighting (rather than a plain segment mean) keeps one tiny
    low-confidence segment from sinking a transcript that is confident across
    the rest of the clip, and keeps a model that splits the audio into many
    short segments comparable to one that emits a few long ones.

    Acoustic confidence is only comparable WITHIN the Whisper family. A backend
    that emits no `avg_logprob` (Parakeet, Voxtral) scores at the floor, so when
    ANY candidate is unscored the scores aren't comparable — the selector keeps
    the generalist (first candidate) rather than letting a scored nb-whisper beat
    an unscored Parakeet generalist on every region (incl. the ones the generalist
    got right). Cross-architecture routing is the text-LID selector's job, not
    this one's (ADR-0010)."""

    def select(
        self,
        candidates: Sequence[CachedTranscription],
        *,
        candidate_languages: tuple[str, ...] = (),  # noqa: ARG002 — the acoustic selector ignores it; carried for the seam
    ) -> CachedTranscription:
        if not candidates:
            raise ValueError("select() needs at least one candidate transcript")
        scores = [_confidence_score(c) for c in candidates]
        if any(s == _NO_CONFIDENCE for s in scores):
            return candidates[0]
        # Argmax over the already-computed scores (list.index returns the FIRST
        # max, so generalist-first ordering still wins a tie) — no re-scoring.
        return candidates[scores.index(max(scores))]


def _confidence_score(cached: CachedTranscription) -> float:
    """Duration-weighted mean of the segments' `avg_logprob`. Segments without a
    confidence (some backends omit it) contribute neither weight nor value;
    `_NO_CONFIDENCE` when nothing in the transcript is scored."""
    weighted_sum = 0.0
    weight = 0.0
    for seg in cached.result.segments:
        if seg.avg_logprob is None:
            continue
        # A zero-length scored segment still counts as one observation, so a
        # confidence is never silently dropped just because its span rounded
        # to zero.
        dur = seg.end - seg.start
        w = dur if dur > 0.0 else 1.0
        weighted_sum += seg.avg_logprob * w
        weight += w
    return weighted_sum / weight if weight > 0.0 else _NO_CONFIDENCE


class SpecialistRoutingSelector:
    """Default selector: route each region to the model the specialist table
    names for the language the generalist detected there; fall back to acoustic
    confidence otherwise.

    The specialist table (`catalog.SPECIALIST_MODELS`) is a declaration that a
    purpose-built model is the BEST one for a language (ADR-0010: "languages
    where a specialist beats the generalist"). So when the generalist detects a
    region as such a language — e.g. a Norwegian region — the specialist wins
    even if the generalist's `avg_logprob` happens to be higher (measured: on a
    real Norwegian clip nb-whisper scored recall 0.92 / WER 0.08 vs the
    generalist's 0.77 / 0.12, yet the generalist had the higher confidence). The
    `da/no` benchmark (`tests/e2e/test_pipeline_e2e.py::test_da_no_routing_benchmark`)
    is the evidence and the regression guard.

    Where the detected language has no specialist (e.g. Danish, English) or the
    specialist didn't run, it defers to `AcousticConfidenceSelector` — which also
    carries the cross-architecture guard (an unscored Parakeet/Voxtral generalist
    stays the winner rather than losing to a scored nb-whisper). The routing key
    is `candidates[0]` because the cover lists the generalist first and it is the
    one that does per-region language detection; the specialists are fixed-language."""

    def __init__(self) -> None:
        self._acoustic = AcousticConfidenceSelector()

    def select(
        self,
        candidates: Sequence[CachedTranscription],
        *,
        candidate_languages: tuple[str, ...] = (),
    ) -> CachedTranscription:
        if not candidates:
            raise ValueError("select() needs at least one candidate transcript")
        # Local import avoids a module-load cycle (catalog is heavier) and reads
        # the live table, so a test's monkeypatch.setitem is honoured.
        from .transcribers.catalog import SPECIALIST_MODELS

        detected = (candidates[0].result.language or "").strip().lower()
        specialist_model = SPECIALIST_MODELS.get(detected)
        if specialist_model:
            for cand in candidates:
                if cand.result.model == specialist_model:
                    return cand
        return self._acoustic.select(candidates, candidate_languages=candidate_languages)


def default_language_selector() -> LanguageSelector:
    """The selector the batch pipeline uses. One swap point for the whole
    feature — repoint it to swap the strategy everywhere (e.g. a text-LID or
    LLM-judge selector for cross-architecture pairs) without touching
    `transcribe_session_locked`."""
    return SpecialistRoutingSelector()
