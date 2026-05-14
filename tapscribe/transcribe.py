"""Per-WAV synchronous transcription.

Each function in here is called via `asyncio.to_thread` from the FastAPI
routes; they're sync because the underlying backends are sync.

Quality knobs are tuned for batch (post-meeting) transcription, where we
prefer accuracy over speed — the per-WAV transcripts feed a downstream
session-merged transcript that's the source of truth for summarisation.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import hallucinations as hallucinations_mod
from .audio import load_recorder_wav_as_pcm, wav_duration_s
from .models import default_language_for
from .text import read_hotwords, read_prompt


def transcribe_wav_sync(
    model_pack,
    path: Path,
    model_name: str,
    prompt_override: str | None = None,
    hotwords_override: str | None = None,
) -> dict[str, Any]:
    """Top-level dispatch. Picks the right backend based on the model pack tag.

    `prompt_override` / `hotwords_override` (if non-empty after stripping)
    take precedence over the contents of prompt.txt / hotwords.txt.

    `model_pack` is a tuple from `models.get_model`:
      ("whisper", WhisperModel)        — faster-whisper / CTranslate2
      ("whisper_mlx", repo_str)        — mlx-whisper on Apple Silicon
      ("voxtral", processor, model, device, dtype) — Voxtral via transformers
    """
    po = (prompt_override or "").strip()
    ho = (hotwords_override or "").strip()
    initial_prompt = po or (read_prompt() or None)
    hotwords = ho or (read_hotwords() or None)
    if initial_prompt == "":
        initial_prompt = None
    if hotwords == "":
        hotwords = None

    kind = model_pack[0]
    if kind == "voxtral":
        return _transcribe_voxtral_sync(model_pack, path, model_name, initial_prompt, hotwords)
    if kind == "whisper_mlx":
        return _transcribe_mlx_sync(model_pack, path, model_name, initial_prompt, hotwords)
    return _transcribe_faster_whisper_sync(model_pack, path, model_name, initial_prompt, hotwords)


def _transcribe_faster_whisper_sync(
    model_pack,
    path: Path,
    model_name: str,
    initial_prompt: str | None,
    hotwords: str | None,
) -> dict[str, Any]:
    """faster-whisper / CTranslate2 path.

    Quality settings:
    - beam_size=5, patience=2.0: explores 5 hypotheses (vs greedy=1)
    - condition_on_previous_text=False: stops cascading errors across segments
    - hotwords=<list>: stronger biasing toward names than initial_prompt alone
    - word_timestamps=True: DTW re-alignment, improves boundaries and accuracy
    - repetition_penalty=1.1: anti-loop / anti-hallucinated-repetition
    - no_speech_threshold=0.4: lower than default 0.6 so silent stretches get
      flagged more aggressively and skipped (reduces YouTube-subtitle leaks).
    """
    _, model = model_pack
    common = dict(
        language=default_language_for(model_name),
        beam_size=5,
        patience=2.0,
        vad_filter=True,
        initial_prompt=initial_prompt,
        condition_on_previous_text=False,
        word_timestamps=True,
        no_speech_threshold=0.4,
    )
    optional = dict(
        hotwords=hotwords,
        repetition_penalty=1.1,
        hallucination_silence_threshold=2.0,
    )

    # Try with all the quality knobs first. If the installed faster-whisper
    # is too old to accept some kwargs (hotwords, repetition_penalty,
    # hallucination_silence_threshold), retry with the basics so we still
    # produce a transcript instead of erroring out.
    try:
        segments, info = model.transcribe(str(path), **common, **optional)
        segments = list(segments)
        applied_kwargs = {**common, **optional}
    except TypeError:
        segments, info = model.transcribe(str(path), **common)
        segments = list(segments)
        applied_kwargs = dict(common)
    except Exception:
        # Last-resort fallback so we don't lose a job.
        segments, info = model.transcribe(
            str(path),
            language=default_language_for(model_name),
            beam_size=5,
            vad_filter=True,
            initial_prompt=initial_prompt,
        )
        segments = list(segments)
        applied_kwargs = {"beam_size": 5, "vad_filter": True}

    halluc_rules = hallucinations_mod.parse_rules()

    segs: list[dict[str, Any]] = []
    full: list[str] = []
    suppressed: list[dict[str, Any]] = []
    for s in segments:
        text = s.text.strip()
        item: dict[str, Any] = {
            "start": round(s.start, 2),
            "end": round(s.end, 2),
            "text": text,
        }
        # avg_logprob is faster-whisper's per-segment mean log probability
        # of decoded tokens. Roughly -0.2 = confident, -0.5 = shaky,
        # -1.0+ = junk. Surface it so the dashboard can render a
        # low-confidence flag.
        if hasattr(s, "avg_logprob") and s.avg_logprob is not None:
            item["avg_logprob"] = round(float(s.avg_logprob), 3)
        if getattr(s, "words", None):
            item["words"] = [
                {"start": round(w.start, 2), "end": round(w.end, 2), "word": w.word, "prob": round(w.probability, 3)}
                for w in s.words
            ]
        matched_rule = hallucinations_mod.match(text, halluc_rules)
        if matched_rule is not None:
            item["matched_rule"] = matched_rule
            suppressed.append(item)
            continue
        segs.append(item)
        full.append(text)

    applied_view = {k: (v if not callable(v) else str(v)) for k, v in applied_kwargs.items()}
    return {
        "model": model_name,
        "backend": "faster-whisper",
        "device": "CPU (CTranslate2; NOT MLX)",
        "language": info.language,
        "language_probability": round(info.language_probability or 0.0, 3),
        "duration": round(info.duration or wav_duration_s(path), 2),
        "segments": segs,
        "text": " ".join(full).strip(),
        "initial_prompt_used": initial_prompt or "",
        "hotwords_used": hotwords or "",
        "quality_settings": applied_view,
        "suppressed_hallucinations": suppressed,
    }


def _transcribe_voxtral_sync(
    model_pack,
    path: Path,
    model_name: str,
    initial_prompt: str | None,
    hotwords: str | None,
) -> dict[str, Any]:
    """Transcribe via Voxtral with deterministic, anti-loop, transcribe-only
    settings. Single segment per WAV since Voxtral generates free-form text
    rather than per-window timestamps. Acceptable here because each WAV is
    already one mute-to-mute utterance."""
    import torch  # type: ignore

    _, processor, model, device, _ = model_pack

    # Sharper instruction. Voxtral is an audio-LLM so it can drift into
    # summarising / answering / "helpful" framing. Pinning it to verbatim
    # transcription matters more than for Whisper.
    instr = (
        "Transcribe the audio verbatim into text. "
        "Do not add commentary, summarisation, translation, or interpretation. "
        "Do not describe the audio. Output only the spoken words. "
        "Use punctuation and casing that matches the speech."
    )
    if initial_prompt:
        instr += " Context for this conversation: " + initial_prompt
    if hotwords:
        instr += " Proper nouns and jargon that may appear (use these spellings): " + hotwords

    conversation = [
        {
            "role": "user",
            "content": [
                {"type": "audio", "path": str(path)},
                {"type": "text", "text": instr},
            ],
        }
    ]
    inputs = processor.apply_chat_template(conversation, return_tensors="pt").to(device)

    gen_kwargs = dict(
        max_new_tokens=2048,
        do_sample=False,
        repetition_penalty=1.1,
        num_beams=1,
    )

    with torch.no_grad():
        outputs = model.generate(**inputs, **gen_kwargs)
    prompt_len = inputs.input_ids.shape[1] if hasattr(inputs, "input_ids") else 0
    gen_ids = outputs[:, prompt_len:]
    text = processor.batch_decode(gen_ids, skip_special_tokens=True)[0].strip()

    dur = wav_duration_s(path)
    return {
        "model": model_name,
        "language": "auto",
        "language_probability": 0.0,
        "duration": round(dur, 2),
        "segments": [
            {"start": 0.0, "end": round(dur, 2), "text": text},
        ],
        "text": text,
        "initial_prompt_used": initial_prompt or "",
        "hotwords_used": hotwords or "",
        "backend": "voxtral",
        "device": "CPU (HF transformers; NOT MLX)" if device == "cpu" else f"{device.upper()} (HF transformers; NOT MLX)",
        "quality_settings": gen_kwargs,
    }


def _transcribe_mlx_sync(
    model_pack,
    path: Path,
    model_name: str,
    initial_prompt: str | None,
    hotwords: str | None,
) -> dict[str, Any]:
    """Transcribe via mlx-whisper on Apple Silicon GPU. Quality knobs that
    overlap with the faster-whisper path are kept identical
    (no_speech_threshold, hallucination_silence_threshold, word_timestamps,
    condition_on_previous_text). mlx-whisper has no `hotwords` kwarg so the
    hotword list is folded into initial_prompt with a short framing line."""
    import mlx_whisper  # type: ignore

    _, repo = model_pack

    effective_prompt = initial_prompt or ""
    if hotwords:
        framing = "Proper nouns, names, and jargon that may appear: "
        if effective_prompt:
            effective_prompt = effective_prompt + "\n" + framing + hotwords
        else:
            effective_prompt = framing + hotwords
    effective_prompt_arg = effective_prompt or None

    kwargs: dict[str, Any] = dict(
        path_or_hf_repo=repo,
        language=default_language_for(model_name),
        initial_prompt=effective_prompt_arg,
        condition_on_previous_text=False,
        word_timestamps=True,
        no_speech_threshold=0.4,
        hallucination_silence_threshold=2.0,
        temperature=0.0,
    )

    # mlx-whisper's path-based load runs `ffmpeg` as a subprocess, which
    # fails if ffmpeg isn't on PATH (common on fresh macOS installs).
    # Pre-decode ourselves from the WAV — the recorder always writes the
    # exact format mlx-whisper wants internally. Fall back to the string
    # path if the WAV has unexpected format (lets ffmpeg/PyAV handle
    # resampling), so we keep working even with non-standard WAVs.
    try:
        audio = load_recorder_wav_as_pcm(path)
        result = mlx_whisper.transcribe(audio, **kwargs)
    except RuntimeError as e:
        print(f"[tapscribe] mlx pre-decode failed ({e}); falling back to path (needs ffmpeg).", flush=True)
        result = mlx_whisper.transcribe(str(path), **kwargs)

    halluc_rules = hallucinations_mod.parse_rules()
    segs: list[dict[str, Any]] = []
    full: list[str] = []
    suppressed: list[dict[str, Any]] = []
    for s in result.get("segments", []) or []:
        text = (s.get("text") or "").strip()
        item: dict[str, Any] = {
            "start": round(float(s.get("start", 0.0)), 2),
            "end": round(float(s.get("end", 0.0)), 2),
            "text": text,
        }
        if s.get("avg_logprob") is not None:
            item["avg_logprob"] = round(float(s["avg_logprob"]), 3)
        words = s.get("words")
        if words:
            item["words"] = [
                {
                    "start": round(float(w.get("start", 0.0)), 2),
                    "end": round(float(w.get("end", 0.0)), 2),
                    "word": w.get("word", ""),
                    "prob": round(float(w.get("probability", 0.0)), 3),
                }
                for w in words
            ]
        matched = hallucinations_mod.match(text, halluc_rules)
        if matched is not None:
            item["matched_rule"] = matched
            suppressed.append(item)
            continue
        segs.append(item)
        if text:
            full.append(text)

    applied_view = {k: (v if not callable(v) else str(v)) for k, v in kwargs.items()}
    return {
        "model": model_name,
        "backend": "mlx-whisper",
        "device": "Apple Silicon GPU (MLX)",
        "language": result.get("language", "?"),
        "language_probability": 0.0,
        "duration": round(wav_duration_s(path), 2),
        "segments": segs,
        "text": " ".join(full).strip(),
        "initial_prompt_used": effective_prompt or "",
        "hotwords_used": hotwords or "",
        "quality_settings": applied_view,
        "suppressed_hallucinations": suppressed,
    }
