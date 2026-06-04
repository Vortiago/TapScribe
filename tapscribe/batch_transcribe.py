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
from datetime import UTC, datetime
from typing import Any, Literal

from . import config
from . import hallucinations as hallucinations_mod
from .audio import wav_duration_s, wav_rms_dbfs
from .recorder import JobState, Recorder
from .session_merge import merge_session, select_session_wavs
from .sessions import read_session_meta, resolve_session_dir, resolve_wav
from .text import read_hotwords, read_prompt
from .transcribers import load_transcriber, release_transcriber, run_on_model_thread
from .wav_cache import cached_transcribe, read_primary_payload

# ---------------------------------------------------------------------------
# Domain errors — the route layer maps these to HTTP codes
# ---------------------------------------------------------------------------


class BatchTranscribeError(Exception):
    """Base class for every domain error this module raises."""


class WavUnreadable(BatchTranscribeError):
    """The target WAV is empty, truncated, or its header won't parse."""


class WavTooQuiet(BatchTranscribeError):
    """The original WAV's RMS is below the silence floor — Whisper would
    hallucinate, so the single-WAV path refuses rather than producing
    junk. The session loop doesn't apply this check; `select_session_wavs`
    surfaces silent WAVs separately."""


class SessionBusy(BatchTranscribeError):
    """A transcribe or strip job is already in flight on this session.
    The `JobTracker.claim` returned False; only one such job per session
    runs at a time."""


class NoUsableWavs(BatchTranscribeError):
    """The session range filter rejected every WAV — either the directory
    is empty or the from_iso/to_iso range matched nothing. Distinct from
    `InvalidRange`: this is "your inputs were valid but nothing matched",
    not "your inputs were unparseable.\""""


class InvalidRange(BatchTranscribeError):
    """`select_session_wavs` raised `ValueError` — typically an
    unparseable `from_iso` / `to_iso`. The caller's inputs were
    syntactically wrong; the matching `NoUsableWavs` is for "valid
    inputs, empty result." Routes map this to 400, `NoUsableWavs` to
    404."""


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
    target_lang: str | None
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
    target_lang: str | None


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
    target_lang: str | None


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
    prompt = (meta.get("prompt") or "").strip() or (read_prompt() or "").strip()
    hotwords = (meta.get("hotwords") or "").strip() or (read_hotwords() or "").strip()
    return (prompt or None), (hotwords or None)


def _build_invocation(
    session: str,
    *,
    source_lang: str | None,
    target_lang: str | None,
) -> TranscriberInvocation:
    prompt, hotwords = _effective_prompt_hotwords(session)
    return TranscriberInvocation(
        initial_prompt=prompt,
        hotwords=hotwords,
        source_lang=source_lang,
        target_lang=target_lang,
        hallucination_rules=tuple(hallucinations_mod.parse_rules()),
    )


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


async def transcribe_one(recorder: Recorder, req: BatchOneRequest) -> dict:  # noqa: ARG001 — Recorder unused for the single-WAV path today but kept for symmetry with transcribe_session and so future per-WAV state (e.g. per-tap overrides) has a place to land
    """Transcribe one WAV; always force=True (explicit per-WAV requests
    bypass the cache). Returns the freshly-written sidecar's raw JSON
    dict so the wire shape callers expect is preserved.

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

    transcriber = await run_on_model_thread(load_transcriber, req.model, backend=req.backend)
    try:
        inv = _build_invocation(req.session, source_lang=req.source_lang, target_lang=req.target_lang)

        await run_on_model_thread(
            cached_transcribe,
            path,
            transcriber,
            initial_prompt=inv.initial_prompt,
            hotwords=inv.hotwords,
            source_lang=inv.source_lang,
            target_lang=inv.target_lang,
            hallucination_rules=list(inv.hallucination_rules),
            force=True,
            source=req.source,
        )

        payload = read_primary_payload(path)
        if payload is None:
            # cached_transcribe just wrote the sidecar — read_primary_payload
            # returning None here means the cache layout migration or write
            # silently failed. Bubble as a 500-equivalent rather than masking.
            raise BatchTranscribeError("cached_transcribe completed but no sidecar landed on disk")
        return payload
    finally:
        # Release our use of the model so the configured idle-TTL policy can
        # unload it (default: immediately, freeing several GB). Offloaded
        # because eviction may run gc + GPU-cache reclaim; a no-op when
        # load_transcriber was monkeypatched to a fake in tests.
        await run_on_model_thread(release_transcriber, transcriber)


async def transcribe_session(recorder: Recorder, req: BatchSessionRequest) -> dict:
    """Transcribe every WAV in the supplied range, then merge and write
    `session-transcript.json` + `.txt`. Returns the merged dict.

    Claims a `JobTracker` slot for the session — only one transcribe /
    strip job per session at a time. Raises `SessionBusy` when the slot
    is already taken; raises `NoUsableWavs` when the range filter
    rejected every WAV. Per-WAV failures inside the cache loop
    propagate, mark the job `status="error: …"`, then release the slot
    via the `finally` block."""
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

    transcriber = await run_on_model_thread(load_transcriber, req.model, backend=req.backend)
    try:
        inv = _build_invocation(req.session, source_lang=req.source_lang, target_lang=req.target_lang)

        claimed = await recorder.jobs.claim(
            JobState(
                session=req.session,
                kind="transcribe",
                current=0,
                total=len(selection.wavs),
                started_at=datetime.now(UTC),
                model=req.model,
                status="running",
            )
        )
        if not claimed:
            raise SessionBusy("session is already busy (transcribe or strip in flight)")

        try:
            for idx, wav in enumerate(selection.wavs):
                await recorder.jobs.update(req.session, current=idx, current_file=wav.name)
                await run_on_model_thread(
                    cached_transcribe,
                    wav,
                    transcriber,
                    initial_prompt=inv.initial_prompt,
                    hotwords=inv.hotwords,
                    source_lang=inv.source_lang,
                    target_lang=inv.target_lang,
                    hallucination_rules=list(inv.hallucination_rules),
                    force=req.force,
                    source=selection.source,
                )

            transcript = merge_session(selection)
            merged = transcript.to_dict()
            if not merged.get("model"):
                merged["model"] = req.model

            out_path = session_dir / "session-transcript.json"
            out_path.write_text(json.dumps(merged, indent=2, ensure_ascii=False), encoding="utf-8")
            (session_dir / "session-transcript.txt").write_text(transcript.plain_text, encoding="utf-8")

            return merged
        except Exception as e:
            await recorder.jobs.update(req.session, status="error: " + str(e))
            raise
        finally:
            await recorder.jobs.release(req.session)
    finally:
        # Release our use of the model on every exit path (success, the
        # SessionBusy short-circuit, or a per-WAV failure) so the idle-TTL
        # policy can unload it. Offloaded for the same reason as transcribe_one.
        await run_on_model_thread(release_transcriber, transcriber)
