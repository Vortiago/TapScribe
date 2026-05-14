#!/usr/bin/env python3
"""CLI wrapper around tapscribe.strip_silence.

The same detector pipeline that powers the dashboard's strip-silence button
is also runnable standalone for offline batch jobs.

Usage:
    python tools/strip_silence_cli.py recordings/<session>/some.wav
    python tools/strip_silence_cli.py recordings/<session>/some.wav --mode split
    python tools/strip_silence_cli.py recordings/<session>/
    python tools/strip_silence_cli.py recordings/ --recursive
    python tools/strip_silence_cli.py x.wav --min-silence-ms 800 --pad-ms 100

Modes:
    trim    Write <name>.stripped.wav next to the input, silent regions removed.
    split   Write <name>_split/001.wav, 002.wav, ... one WAV per speech segment.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Make the tapscribe package importable when running this script from a
# checkout (no install required).
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import numpy as np  # noqa: E402

from tapscribe import strip_silence as ss  # noqa: E402


def process_one(path: Path, mode: str, min_silence_ms: int, pad_ms: int,
                threshold_db: float, use_silero: bool,
                speech_floor_db: float = ss.SPEECH_RMS_DBFS_FLOOR) -> None:
    samples = ss.read_wav_int16(path)
    total = len(samples)
    if total == 0:
        print(f"[strip-silence] {path.name}: empty file, skipping")
        return

    regions = None
    if use_silero:
        regions = ss.detect_speech_silero(samples, min_silence_ms=min_silence_ms, pad_ms=pad_ms)
        if regions is None:
            print("[strip-silence] silero-vad not installed; using RMS threshold fallback.")
            print("                pip install silero-vad  for higher accuracy.")
    if regions is None:
        regions = ss.detect_speech_rms(samples, threshold_db=threshold_db,
                                       min_silence_ms=min_silence_ms, pad_ms=pad_ms)

    in_secs = total / ss.SAMPLE_RATE
    if not regions:
        print(f"[strip-silence] {path.name}: no speech detected in {in_secs:.1f}s, no output written")
        return

    pre_filter_count = len(regions)
    regions = ss.filter_low_energy_regions(samples, regions, floor_dbfs=speech_floor_db)
    if not regions:
        print(f"[strip-silence] {path.name}: all {pre_filter_count} regions below "
              f"{speech_floor_db:.1f} dBFS speech floor, no output written")
        return

    speech_secs = sum(e - s for s, e in regions) / ss.SAMPLE_RATE
    pct = 100.0 * speech_secs / in_secs
    dropped_note = "" if len(regions) == pre_filter_count else f" (filtered {pre_filter_count - len(regions)} below floor)"
    print(f"[strip-silence] {path.name}: {speech_secs:.1f}s speech of {in_secs:.1f}s ({pct:.0f}%), {len(regions)} segments{dropped_note}")

    if mode == "trim":
        out_path = path.with_suffix(".stripped.wav")
        out_samples = np.concatenate([samples[s:e] for s, e in regions])
        ss.write_wav_int16(out_path, out_samples)
        print(f"  -> {out_path.name} ({len(out_samples)/ss.SAMPLE_RATE:.1f}s)")
    elif mode == "split":
        out_dir = path.parent / (path.stem + "_split")
        out_dir.mkdir(exist_ok=True)
        for idx, (s, e) in enumerate(regions, start=1):
            out_path = out_dir / f"{idx:03d}.wav"
            ss.write_wav_int16(out_path, samples[s:e])
        print(f"  -> {out_dir.name}/ ({len(regions)} files)")


def collect_targets(input_path: Path, recursive: bool) -> list[Path]:
    if input_path.is_file():
        return [input_path]
    pattern = "**/*.wav" if recursive else "*.wav"
    out = []
    for p in sorted(input_path.glob(pattern)):
        if p.name.endswith(".stripped.wav"):
            continue
        if p.parent.name.endswith("_split"):
            continue
        out.append(p)
    return out


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("input", type=Path, help="WAV file or directory of WAVs")
    parser.add_argument("--mode", choices=["trim", "split"], default="trim",
                        help="trim writes one stripped WAV; split writes one WAV per speech segment")
    parser.add_argument("--min-silence-ms", type=int, default=500,
                        help="Silences shorter than this don't break a segment (default: 500)")
    parser.add_argument("--pad-ms", type=int, default=200,
                        help="Padding around each speech region (default: 200)")
    parser.add_argument("--threshold-db", type=float, default=-45.0,
                        help="RMS threshold for fallback detector when silero is unavailable (default: -45 dBFS)")
    parser.add_argument("--speech-floor-db", type=float, default=ss.SPEECH_RMS_DBFS_FLOOR,
                        help="Drop regions whose RMS is below this dBFS (default: -45 dBFS)")
    parser.add_argument("--no-silero", action="store_true",
                        help="Use the RMS-threshold detector even if silero-vad is installed")
    parser.add_argument("--recursive", action="store_true",
                        help="If input is a directory, recurse into subdirectories")
    args = parser.parse_args()

    if not args.input.exists():
        print(f"ERROR: {args.input} not found", file=sys.stderr)
        sys.exit(1)

    targets = collect_targets(args.input, args.recursive)
    if not targets:
        print(f"No WAV files to process under {args.input}")
        return

    use_silero = not args.no_silero
    for t in targets:
        try:
            process_one(t, args.mode, args.min_silence_ms, args.pad_ms,
                        args.threshold_db, use_silero, args.speech_floor_db)
        except Exception as e:
            print(f"[strip-silence] {t}: FAILED: {e}")


if __name__ == "__main__":
    main()
