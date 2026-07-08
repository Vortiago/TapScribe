"""Batch transcription — drive a Transcriber across one WAV or a session.

See CONTEXT.md "Batch transcription" for the full vocabulary. This
module is the orchestrator the `/api/transcribe` and
`/api/transcribe-session` route handlers delegate to; it owns the
prompt/hotwords-over-session-meta resolution, the per-call
`TranscriberInvocation` envelope, the cache loop, hallucination rules,
the JobTracker bookkeeping (session form only), and the merged-output
writes — everything between "the route parsed a JSON body" and "the
sidecar landed on disk."

The module is FastAPI-free by design: errors raised here are domain
exceptions (subclasses of `BatchTranscribeError`) and the route handlers
map those to HTTP codes. That keeps the same code path usable from a
future CLI batch, queue worker, or per-region re-transcribe without
re-implementing the chain.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

from . import config
from . import hallucinations as hallucinations_mod
from .audio import wav_duration_s, wav_rms_dbfs
from .language_select import default_language_selector
from .recorder import Recorder
from .session_merge import InvalidRange, NoUsableWavs, merge_session, select_session_wavs
from .session_paths import (
    FILENAME_TRANSCRIPT_JSON,
    FILENAME_TRANSCRIPT_TXT,
    resolve_session_dir,
    resolve_wav,
)
from .sessions import read_session_meta
from .text import atomic_write_text, read_config, read_languages
from .transcribers import lease_transcriber, run_on_model_thread
from .transcribers.catalog import DEFAULT_BATCH_MODEL, REGISTRY, cover_models
from .wav_cache import CachedTranscription, cached_transcribe, read_primary_payload, set_primary_transcript

# ---------------------------------------------------------------------------
# Domain errors — the route layer maps these to HTTP codes
# ---------------------------------------------------------------------------


class BatchTranscribeError(Exception):
    """Base class for the transcription-specific domain errors this module
    raises. Cross-cutting errors live with their concept, not here:
    `SessionBusy` in `tapscribe.recorder` (a JobTracker concept) and
    `NoUsableWavs` / `InvalidRange` in `tapscribe.session_merge` (selection
    verdicts) — both re-exported into this module's namespace would just
    reintroduce the coupling the relocation removed."""


class WavUnreadable(BatchTranscribeError):
    """The target WAV is empty, truncated, or its header won't parse."""


class WavTooQuiet(BatchTranscribeError):
    """The original WAV's RMS is below the silence floor — Whisper would
    hallucinate, so the single-WAV path refuses rather than producing
    junk. The session loop doesn't apply this check; `select_session_wavs`
    surfaces silent WAVs separately."""


# ---------------------------------------------------------------------------
# Value objects — the test surface
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TranscriberInvocation:
    """The per-call envelope handed to `cached_transcribe`. Built once
    from a request via `_build_invocation`; carries the resolved
    prompt/hotwords (session-meta over global config), the optional
    language pair, and the hallucination rules parsed at request time."""

    initial_prompt: str | None
    hotwords: str | None
    source_lang: str | None
    # The meeting's candidate-language set when it stays a constrained
    # auto-detect (a multi-language set with no explicit pin). Empty when the
    # language is already pinned via `source_lang` (an explicit per-job pin or a
    # singleton candidate set). The cache loop snaps it to a concrete per-region
    # language. See ADR-0010.
    candidate_languages: tuple[str, ...]
    hallucination_rules: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class BatchOneRequest:
    """Inputs for transcribing one WAV. `backend` is the already-resolved
    operator preference (the route falls `recorder.backend` in if the
    body omitted it) so the value object is self-contained."""

    session: str
    name: str
    source: Literal["original", "stripped"]
    model: str
    backend: str
    source_lang: str | None


@dataclass(frozen=True)
class BatchSessionRequest:
    """Inputs for transcribing a session-range. `from_iso`/`to_iso` are
    forwarded to `select_session_wavs`; `force` propagates to
    `cached_transcribe` (True re-runs every WAV regardless of cache)."""

    session: str
    source: Literal["original", "stripped"]
    model: str
    backend: str
    from_iso: str | None
    to_iso: str | None
    force: bool
    source_lang: str | None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _effective_prompt_hotwords(session: str) -> tuple[str | None, str | None]:
    """Resolve the per-session override chain: session-meta then global
    config. Returns (initial_prompt, hotwords), each None when both
    layers are empty so adapters receive no value (vs. the empty string,
    which some backends would treat as a real prompt).

    Limitation carried over from the prior route helper: an empty
    session-meta override falls back to the global default — a session
    can't assert "specifically NO prompt, even though a global is set."
    Workaround is to clear the global; a future sentinel value could
    express it explicitly without touching the global."""
    meta = read_session_meta(session)
    prompt = (meta.get("prompt") or "").strip() or (read_config("prompt") or "").strip()
    hotwords = (meta.get("hotwords") or "").strip() or (read_config("hotwords") or "").strip()
    return (prompt or None), (hotwords or None)


def _effective_candidate_languages(session: str) -> tuple[str, ...]:
    """Resolve the meeting's candidate-language set (ADR-0010): a session-meta
    `languages` override (kept to catalog codes) else the global default
    (`read_languages`, itself the bundled {da, no, en} when unset). Always
    returns at least one code."""
    from .transcribers.catalog import is_candidate_language

    raw = read_session_meta(session).get("languages")
    if isinstance(raw, (list, tuple)):
        override = tuple(dict.fromkeys(c for c in raw if isinstance(c, str) and is_candidate_language(c)))
        if override:
            return override
    return read_languages()


def _resolve_language_plan(
    session: str, explicit_source_lang: str | None
) -> tuple[str | None, tuple[str, ...], tuple[str, ...]]:
    """Resolve the meeting's language policy in ONE session-meta read (ADR-0010),
    returning `(source_lang_pin, candidate_languages, cover_languages)`:

    - an explicit per-job `source_lang` (the manual transcribe pin) wins as-is and
      BYPASSES the candidate-set machinery: it pins the operator's chosen model
      and covers nothing else (`cover_languages = ()` → generalist only), so the
      explicit model is honoured rather than overridden by a specialist;
    - a singleton declared set pins via `source_lang` and covers that one language
      (a specialist still joins if the language has one, e.g. {no});
    - a multi-language set defers to a per-region constrained auto-detect
      (carried as `candidate_languages`) and covers the whole set.

    `cover_languages` is what `cover_models` unions specialists over;
    `candidate_languages` is the constrained-detect set for the generalist (empty
    when the language is already pinned)."""
    if explicit_source_lang:
        return explicit_source_lang, (), ()
    langs = _effective_candidate_languages(session)
    if len(langs) == 1:
        return langs[0], (), langs
    return None, langs, langs


def _build_invocation(
    session: str,
    *,
    source_lang: str | None,
    candidate_languages: tuple[str, ...] = (),
) -> TranscriberInvocation:
    """Build the per-call envelope: resolve prompt/hotwords (session-meta over
    global) and carry the language fields as given. The candidate-language
    *policy* (singleton → pin, multi → constrained detect) is resolved by the
    caller via `_resolve_language_plan` and passed in — BOTH batch paths do so
    now: the session range AND the manual single-WAV `transcribe_one`, which runs
    the same cover as a one-WAV slice (ADR-0011)."""
    prompt, hotwords = _effective_prompt_hotwords(session)
    return TranscriberInvocation(
        initial_prompt=prompt,
        hotwords=hotwords,
        source_lang=source_lang,
        candidate_languages=candidate_languages,
        hallucination_rules=tuple(hallucinations_mod.parse_rules()),
    )


def resolve_batch_model(*, warn: bool = True) -> str:
    """The operator's configured default batch model (the ADR-0010 **generalist**
    slot, `config/batch-model.txt`), validated against the catalog so a
    stale/out-of-band edit can never reach a model loader. Falls back to the
    bundled default. This is the model the interactive Transcript page and the
    end-of-meeting pipeline both transcribe from — the operator declares
    *languages*, not a model (ADR-0011), so the routes resolve the generalist
    here rather than taking it from the request body.

    `warn=False` suppresses the invalid-config log line — pass it from the
    ~2 Hz `/api/state` poll (which resolves the generalist only to DISPLAY it),
    so a stale batch-model.txt warns once per real transcribe, not twice a
    second."""
    configured = read_config("batch-model")
    if configured:
        if REGISTRY.get(configured) is not None:
            return configured
        if warn:
            print(
                f"[tapscribe] configured batch model {configured!r} is not in the catalog — "
                f"falling back to {DEFAULT_BATCH_MODEL!r}",
                flush=True,
            )
    return DEFAULT_BATCH_MODEL


# ---------------------------------------------------------------------------
# Cover loop — shared by the session-range and single-WAV paths (ADR-0010 slice
# 2 + ADR-0011). Each region is transcribed with EVERY cover model, one model
# resident at a time (low peak memory); the caller selects a per-region winner.
# ---------------------------------------------------------------------------


async def _run_cover(
    wavs,
    *,
    models: tuple[str, ...],
    generalist: str,
    backend: str,
    inv: TranscriberInvocation,
    force: bool,
    source: str,
    on_step=None,
) -> dict[str, list[CachedTranscription]]:
    """Transcribe each WAV in `wavs` with every model in `models`, collecting
    THIS run's results per WAV (keyed by `wav.name`) so the caller's selector
    picks among exactly the transcripts we just produced — never a stale sidecar
    from a model the operator has since dropped.

    The operator's `backend` preference is for THEIR chosen generalist; a
    system-routed specialist self-resolves its own backend (``"auto"``) so an
    MLX-preference generalist doesn't drag nb-whisper (cpu/cuda only, no MLX
    binding) into an unsupported-backend crash on Apple Silicon. `on_step(step,
    wav)` — when given — is awaited before each transcribe so the session path
    can report per-WAV job progress; the single-WAV path passes None."""
    per_wav: dict[str, list[CachedTranscription]] = {wav.name: [] for wav in wavs}
    rules = list(inv.hallucination_rules)
    step = 0
    for model_id in models:
        model_backend = backend if model_id == generalist else "auto"
        # Structural lease (#231): acquire/release is handled by
        # `lease_transcriber` on every exit path (including a failed transcribe),
        # so the next model in the cover — or the next request — can never find
        # this one pinned by a forgotten release.
        async with lease_transcriber(model_id, backend=model_backend) as transcriber:
            for wav in wavs:
                if on_step is not None:
                    await on_step(step, wav)
                cached = await run_on_model_thread(
                    cached_transcribe,
                    wav,
                    transcriber,
                    initial_prompt=inv.initial_prompt,
                    hotwords=inv.hotwords,
                    source_lang=inv.source_lang,
                    candidate_languages=inv.candidate_languages,
                    hallucination_rules=rules,
                    force=force,
                    source=source,
                )
                per_wav[wav.name].append(cached)
                step += 1
    return per_wav


def _pick_primary(
    candidates: list[CachedTranscription], *, cover_languages: tuple[str, ...]
) -> CachedTranscription:
    """The winning transcript for one region. With ≥2 candidates the pluggable
    selector routes; a single candidate is the winner outright (and the selector
    is never consulted, so a monolingual run can't be perturbed by a selector
    regression)."""
    if len(candidates) < 2:
        return candidates[0]
    return default_language_selector().select(candidates, candidate_languages=cover_languages)


def _select_primaries(
    wavs, per_wav: dict[str, list[CachedTranscription]], *, cover_languages: tuple[str, ...]
) -> None:
    """Point each WAV's `_primary` at THIS run's winner — ALWAYS, even a
    single-model run. Repointing unconditionally is what keeps a re-transcribe
    after the operator NARROWS the meeting's languages from leaving `_primary`
    aimed at a prior cover's winner (e.g. a Norwegian specialist that no longer
    runs): the fresh run's model becomes primary. `_pick_primary` short-circuits
    the selector for a lone candidate, so a monolingual run still never consults
    it. Shared by both entry points so they can't diverge (ADR-0011: one routing
    behaviour)."""
    for wav in wavs:
        winner = _pick_primary(per_wav[wav.name], cover_languages=cover_languages)
        set_primary_transcript(wav, backend=winner.result.backend, model=winner.result.model)


def _resolve_cover(req):
    """Resolve one batch request's language policy into the per-call invocation,
    the cover's model set, and the cover languages (the selector's tie-break
    context). "Resolve languages → build the invocation → compute the cover" is a
    single conceptual step shared by both entry points (ADR-0011); `req` is a
    `BatchOneRequest` or `BatchSessionRequest` (both carry
    session/source_lang/model)."""
    resolved_source, candidate_languages, cover_languages = _resolve_language_plan(
        req.session, req.source_lang
    )
    inv = _build_invocation(
        req.session,
        source_lang=resolved_source,
        candidate_languages=candidate_languages,
    )
    models = cover_models(cover_languages, generalist=req.model)
    return inv, models, cover_languages


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


async def transcribe_one(recorder: Recorder, req: BatchOneRequest) -> dict:  # noqa: ARG001 — Recorder unused for the single-WAV path today but kept for symmetry with transcribe_session and so future per-WAV state (e.g. per-tap overrides) has a place to land
    """Transcribe one WAV, running the meeting's cover as a one-WAV slice
    (ADR-0011): the generalist plus a specialist for any of the meeting's
    candidate languages that has one, then point `_primary` at the selector's
    winner. Always force=True (explicit per-WAV requests bypass the cache).
    Returns the winning sidecar's raw JSON dict so the wire shape callers expect
    is preserved.

    Pre-checks the ORIGINAL WAV's size and RMS so the operator gets fast
    `WavUnreadable` / `WavTooQuiet` feedback on noise files instead of
    waiting for the model to chew through silence and produce a
    hallucinated transcript."""
    path = resolve_wav(req.session, req.name, req.source)
    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    if size < 64 or wav_duration_s(path) <= 0.0:
        raise WavUnreadable(f"empty or unreadable WAV (size={size} bytes)")

    # Silence detection always reads the ORIGINAL, not the per-source
    # file. The stripped sibling's RMS can be misleadingly high because
    # silero may have false-positive'd on a brief noise burst.
    original_path = config.RECORDINGS_DIR / req.session / req.name
    rms_dbfs = wav_rms_dbfs(original_path)
    if rms_dbfs < config.SILENT_RMS_DBFS_FLOOR:
        raise WavTooQuiet(
            f"original WAV is essentially silent ({rms_dbfs:.1f} dBFS RMS, "
            f"floor {config.SILENT_RMS_DBFS_FLOOR} dBFS) — Whisper would "
            "hallucinate. Remove or skip this file."
        )

    # Same language policy + cover as the session range (ADR-0011): declare
    # languages, not a model. An explicit per-job source_lang still pins and
    # covers nothing else; the multi-language default runs the ensemble.
    inv, models, cover_languages = _resolve_cover(req)

    per_wav = await _run_cover(
        [path],
        models=models,
        generalist=req.model,
        backend=req.backend,
        inv=inv,
        force=True,
        source=req.source,
    )
    # Point _primary at this run's winner (see `_select_primaries`), so the payload
    # we return is the transcript we just produced rather than a stale pointer left
    # by an earlier cover with different languages.
    _select_primaries([path], per_wav, cover_languages=cover_languages)

    payload = read_primary_payload(path)
    if payload is None:
        # cached_transcribe just wrote the sidecar — read_primary_payload
        # returning None here means the cache layout migration or write
        # silently failed. Bubble as a 500-equivalent rather than masking.
        raise BatchTranscribeError("cached_transcribe completed but no sidecar landed on disk")
    return payload


async def transcribe_session(recorder: Recorder, req: BatchSessionRequest) -> dict:
    """Transcribe every WAV in the supplied range, then merge and write
    `session-transcript.json` + `.txt`. Returns the merged dict.

    Brackets the work in `recorder.jobs.run(...)` — one transcribe / strip /
    summarize job per session at a time. The cm raises `SessionBusy` when the
    slot is already taken (releasing nothing in that case — and before the
    model is even loaded) and releases the slot on every other exit path.
    Raises `NoUsableWavs` when the range filter rejected every WAV; per-WAV
    failures inside the loop propagate through the cm, which still releases."""
    session_dir = resolve_session_dir(req.session)

    try:
        selection = select_session_wavs(
            session_dir,
            from_iso=req.from_iso,
            to_iso=req.to_iso,
            source=req.source,
        )
    except ValueError as e:
        raise InvalidRange(str(e)) from e
    if not selection.wavs:
        raise NoUsableWavs("no usable WAVs in the given range")

    async with recorder.jobs.run(
        req.session, kind="transcribe", total=len(selection.wavs), model=req.model
    ) as job:
        return await transcribe_session_locked(req, selection=selection, job=job)


async def transcribe_session_locked(req: BatchSessionRequest, *, selection, job) -> dict:
    """The transcribe core: load the model, drive the cache loop with per-WAV
    progress on the CALLER's job handle, merge, write the session outputs.
    Assumes the caller already holds the session's job slot — claims and
    releases NOTHING (the model release in the finally is the Transcriber
    idle-TTL bookkeeping, not the job slot), so the end-of-meeting pipeline
    can run it as one stage of a single `kind="pipeline"` claim.

    `selection` is the `SessionSelection` the wrapper validated; `job` is any
    object with `async update(**fields)` — `JobTracker.run`'s handle or
    `JobTracker.handle(session)` for a hand-held claim."""
    session_dir = resolve_session_dir(req.session)

    # The batch session path (transcribe_session + the end-of-meeting pipeline
    # both reach here) resolves the meeting's language policy — the generalist
    # pin/detect set, the per-call invocation, AND the cover's model set (the
    # generalist plus a specialist for any covered language that has one, v1:
    # no → nb-whisper). `cover_languages` is empty for an explicit per-job pin →
    # generalist only, honouring the operator's model. Shared with the single-WAV
    # path (ADR-0011).
    inv, models, cover_languages = _resolve_cover(req)

    total_steps = len(selection.wavs) * len(models)
    await job.update(total=total_steps)

    async def _report(step: int, wav) -> None:
        await job.update(current=step, current_file=wav.name)

    per_wav = await _run_cover(
        selection.wavs,
        models=models,
        generalist=req.model,
        backend=req.backend,
        inv=inv,
        force=req.force,
        source=selection.source,
        on_step=_report,
    )

    # Point each region's _primary at THIS run's winner; merge_session then reads
    # the winners. Always repoints (even a single-model run) — see
    # `_select_primaries` — so a re-transcribe after the languages narrow can't
    # leave _primary aimed at a prior cover's specialist.
    _select_primaries(selection.wavs, per_wav, cover_languages=cover_languages)

    transcript = merge_session(selection)
    merged = transcript.to_dict()
    if not merged.get("model"):
        merged["model"] = req.model

    out_path = session_dir / FILENAME_TRANSCRIPT_JSON
    atomic_write_text(out_path, json.dumps(merged, indent=2, ensure_ascii=False))
    atomic_write_text(session_dir / FILENAME_TRANSCRIPT_TXT, transcript.plain_text)

    return merged
