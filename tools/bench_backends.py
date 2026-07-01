#!/usr/bin/env python3
"""Benchmark Whisper backends on the same WAV.

Usage:
    python tools/bench_backends.py path/to/audio.wav
    python tools/bench_backends.py path/to/audio.wav --model small.en
    python tools/bench_backends.py path/to/audio.wav --model large-v3 --language no
    python tools/bench_backends.py path/to/audio.wav --backends faster
    python tools/bench_backends.py path/to/audio.wav --model large-v3 --mlx-model mlx-community/whisper-large-v3-turbo

Times cold (first call, includes model load) and warm (second call, model
already in memory) for each backend, then prints realtime factor (rtf).
rtf = wall_seconds / audio_seconds. Lower is faster; rtf < 1 means
faster than realtime.

Caveat: this is single-shot batch transcription. WhisperLiveKit's
streaming path runs many short windows per minute with VAD on top, so
the absolute numbers differ — but the relative ordering between backends
on the same model is what tells you whether MLX or CPU is the right
choice for your hardware.
"""

import argparse
import sys
import time
import wave
from pathlib import Path

import numpy as np

MLX_REPO_MAP = {
    "tiny": "mlx-community/whisper-tiny-mlx",
    "tiny.en": "mlx-community/whisper-tiny.en-mlx",
    "base": "mlx-community/whisper-base-mlx",
    "base.en": "mlx-community/whisper-base.en-mlx",
    "small": "mlx-community/whisper-small-mlx",
    "small.en": "mlx-community/whisper-small.en-mlx",
    "medium": "mlx-community/whisper-medium-mlx",
    "medium.en": "mlx-community/whisper-medium.en-mlx",
    "large-v2": "mlx-community/whisper-large-v2-mlx",
    "large-v3": "mlx-community/whisper-large-v3-mlx",
    "large-v3-turbo": "mlx-community/whisper-large-v3-turbo",
}


def audio_seconds(wav_path: Path) -> float:
    with wave.open(str(wav_path), "rb") as w:
        return w.getnframes() / float(w.getframerate())


def _load_wav_float32(wav_path: Path):
    """Read a 16kHz mono int16 WAV directly into float32 [-1, 1]. Returns
    None when the format doesn't match (caller falls back to passing the
    path string so mlx-whisper's own ffmpeg-based loader can try)."""
    import wave as _wave

    try:
        with _wave.open(str(wav_path), "rb") as w:
            if w.getframerate() != 16000 or w.getnchannels() != 1 or w.getsampwidth() != 2:
                return None
            raw = w.readframes(w.getnframes())
    except (_wave.Error, OSError):
        return None
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def _audio_for_mlx(wav_path: Path):
    """Return something mlx-whisper can transcribe. Tries the direct-load
    fast path first so we don't need ffmpeg for the common case (recorder
    WAVs are always 16kHz mono int16). Falls back to the path string when
    the format is anything else."""
    audio = _load_wav_float32(wav_path)
    if audio is not None:
        return audio
    print(
        f"[bench] {wav_path.name} is not 16kHz mono int16; deferring to "
        "mlx-whisper's ffmpeg loader. Install ffmpeg if this fails.",
        flush=True,
    )
    return str(wav_path)


def _silero_strip_to_float32(wav_path: Path):
    """Load WAV, run silero-vad, return float32 numpy of speech-only samples.

    Used by mlx-whisper when --vad is set, since mlx-whisper has no built-in
    VAD. faster-whisper takes vad_filter=True and skips this path.
    """
    audio = _load_wav_float32(wav_path)
    if audio is None:
        raise ValueError(f"{wav_path.name}: --vad pre-strip needs 16kHz mono int16 WAV")

    try:
        import torch
        from silero_vad import get_speech_timestamps, load_silero_vad
    except ImportError:
        print("[bench] --vad with mlx-whisper requires silero-vad.", flush=True)
        print("        pip install silero-vad", flush=True)
        sys.exit(1)

    audio_t = torch.from_numpy(audio)
    model = load_silero_vad()
    ts = get_speech_timestamps(audio_t, model, sampling_rate=16000)
    if not ts:
        return audio
    parts = [audio[t["start"] : t["end"]] for t in ts]
    out = np.concatenate(parts)
    total = len(audio) / 16000.0
    kept = len(out) / 16000.0
    print(
        f"[bench] silero pre-strip: kept {kept:.1f}s of {total:.1f}s ({100 * kept / total:.0f}%)", flush=True
    )
    return out


def bench_faster_whisper(wav_path: Path, model_name: str, language, compute_type: str, vad: bool):
    from faster_whisper import WhisperModel

    print(f"[faster-whisper] loading {model_name} (device=cpu, compute_type={compute_type})...", flush=True)
    t0 = time.perf_counter()
    model = WhisperModel(model_name, device="cpu", compute_type=compute_type)
    load_s = time.perf_counter() - t0
    print(f"[faster-whisper] loaded in {load_s:.2f}s", flush=True)
    if vad:
        print("[faster-whisper] vad_filter=True (built-in silero-vad)", flush=True)

    def run_once():
        segments, _info = model.transcribe(str(wav_path), language=language, vad_filter=vad)
        return " ".join(s.text for s in segments)

    print("[faster-whisper] cold run...", flush=True)
    t0 = time.perf_counter()
    run_once()  # cold-run text discarded; the warm-run output is what we report
    cold_s = time.perf_counter() - t0

    print("[faster-whisper] warm run...", flush=True)
    t0 = time.perf_counter()
    text_warm = run_once()
    warm_s = time.perf_counter() - t0

    return {
        "load_s": load_s,
        "cold_s": cold_s,
        "warm_s": warm_s,
        "text": text_warm.strip(),
    }


def bench_mlx_whisper(wav_path: Path, repo: str, language, vad: bool):
    try:
        import mlx_whisper
    except ImportError:
        print("[mlx-whisper] not installed. Install with: pip install mlx-whisper", flush=True)
        return None

    print(f"[mlx-whisper] using {repo}", flush=True)

    if vad:
        audio_input = _silero_strip_to_float32(wav_path)
    else:
        audio_input = _audio_for_mlx(wav_path)

    def run_once():
        kwargs = {"path_or_hf_repo": repo}
        if language:
            kwargs["language"] = language
        result = mlx_whisper.transcribe(audio_input, **kwargs)
        return result["text"]

    print("[mlx-whisper] cold run (first call includes model fetch+load)...", flush=True)
    t0 = time.perf_counter()
    run_once()  # cold-run text discarded; the warm-run output is what we report
    cold_s = time.perf_counter() - t0

    print("[mlx-whisper] warm run...", flush=True)
    t0 = time.perf_counter()
    text_warm = run_once()
    warm_s = time.perf_counter() - t0

    return {
        "load_s": None,
        "cold_s": cold_s,
        "warm_s": warm_s,
        "text": text_warm.strip(),
    }


def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("wav", type=Path, help="WAV file to transcribe")
    parser.add_argument(
        "--model",
        default="small.en",
        help="Model name for faster-whisper (also used to derive MLX repo). Default: small.en",
    )
    parser.add_argument(
        "--mlx-model", default=None, help="Override MLX HF repo (e.g. mlx-community/whisper-large-v3-turbo)"
    )
    parser.add_argument(
        "--language", default=None, help="Force language code (e.g. en, no). Default: auto-detect"
    )
    parser.add_argument(
        "--compute-type",
        default="int8",
        help="faster-whisper compute_type (int8, int8_float16, float16, float32). Default: int8",
    )
    parser.add_argument(
        "--backends", default="faster,mlx", help="Comma-separated backends to run: faster, mlx. Default: both"
    )
    parser.add_argument(
        "--vad",
        action="store_true",
        help="Skip silent regions: faster-whisper uses built-in vad_filter, "
        "mlx-whisper gets silero-pre-stripped audio (requires pip install silero-vad)",
    )
    args = parser.parse_args()

    if not args.wav.exists():
        print(f"ERROR: {args.wav} not found", file=sys.stderr)
        sys.exit(1)

    duration = audio_seconds(args.wav)
    print(f"audio:    {args.wav}")
    print(f"duration: {duration:.1f}s")
    print(f"model:    {args.model}")
    print(f"language: {args.language or '(auto)'}")
    print(f"vad:      {'on' if args.vad else 'off'}")
    print()

    backends = [b.strip() for b in args.backends.split(",") if b.strip()]
    results = {}

    if "faster" in backends:
        try:
            results["faster-whisper"] = bench_faster_whisper(
                args.wav,
                args.model,
                args.language,
                args.compute_type,
                args.vad,
            )
        except Exception as e:
            print(f"[faster-whisper] FAILED: {e}", flush=True)
        print()

    if "mlx" in backends:
        mlx_repo = args.mlx_model or MLX_REPO_MAP.get(args.model)
        if not mlx_repo:
            print(
                f"[mlx-whisper] no MLX repo mapping for model '{args.model}'. "
                f"Pass --mlx-model <hf-repo> to override.",
                flush=True,
            )
        else:
            try:
                results["mlx-whisper"] = bench_mlx_whisper(args.wav, mlx_repo, args.language, args.vad)
            except Exception as e:
                print(f"[mlx-whisper] FAILED: {e}", flush=True)
        print()

    print("=" * 78)
    print(f"{'backend':20} {'load':>10} {'cold':>12} {'warm':>12} {'rtf (warm)':>12}")
    print("-" * 78)
    for name, r in results.items():
        if r is None:
            continue
        load = f"{r['load_s']:.2f}s" if r["load_s"] is not None else "(in cold)"
        rtf = r["warm_s"] / duration if duration > 0 else float("nan")
        print(f"{name:20} {load:>10} {r['cold_s']:>11.2f}s {r['warm_s']:>11.2f}s {rtf:>11.3f}x")
    print("=" * 78)
    print()
    print("rtf = wall_seconds / audio_seconds. Lower is faster.")
    print("'warm' is the meaningful number for a long-running server; 'cold' includes")
    print("model load and first-call graph compilation, which only happens once.")
    print()

    for name, r in results.items():
        if r is None:
            continue
        snippet = r["text"][:240].replace("\n", " ")
        print(f"[{name}] transcript[:240]: {snippet}")


if __name__ == "__main__":
    main()
