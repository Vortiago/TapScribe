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


def process_one(
    path: Path, mode: str, min_silence_ms: int, pad_ms: int, speech_floor_db: float = ss.SPEECH_RMS_DBFS_FLOOR
) -> None:
    """Detection goes through `plan_strip_regions` — the ONE shared
    detect → filter → stats path (extracted in #89) — so this CLI applies
    the exact pipeline the dashboard's strip button does, including the
    whole-file-silence gate (`config.SILENT_RMS_DBFS_FLOOR`) a hand-rolled
    detect-then-filter would miss. Only the trim/split WRITING lives here."""
    samples = ss.read_wav_int16(path)
    plan = ss.plan_strip_regions(
        samples,
        min_silence_ms=min_silence_ms,
        pad_ms=pad_ms,
        speech_floor_db=speech_floor_db,
    )

    if not plan.regions:
        # Empty file / whole-file silent / no speech / all regions below the
        # floor — the plan's reason says which.
        print(f"[strip-silence] {path.name}: {plan.reason}, no output written")
        return

    pct = 100.0 * plan.speech_seconds / plan.in_seconds if plan.in_seconds else 0.0
    dropped_note = (
        ""
        if not plan.segments_filtered_below_floor
        else f" (filtered {plan.segments_filtered_below_floor} below floor)"
    )
    print(
        f"[strip-silence] {path.name}: {plan.speech_seconds:.1f}s speech of "
        f"{plan.in_seconds:.1f}s ({pct:.0f}%), {len(plan.regions)} segments{dropped_note}"
    )

    if mode == "trim":
        out_path = path.with_suffix(".stripped.wav")
        out_samples = np.concatenate([samples[s:e] for s, e in plan.regions])
        ss.write_wav_int16(out_path, out_samples)
        print(f"  -> {out_path.name} ({len(out_samples) / ss.SAMPLE_RATE:.1f}s)")
    elif mode == "split":
        out_dir = path.parent / (path.stem + "_split")
        out_dir.mkdir(exist_ok=True)
        for idx, (s, e) in enumerate(plan.regions, start=1):
            out_path = out_dir / f"{idx:03d}.wav"
            ss.write_wav_int16(out_path, samples[s:e])
        print(f"  -> {out_dir.name}/ ({len(plan.regions)} files)")


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
    parser.add_argument(
        "--mode",
        choices=["trim", "split"],
        default="trim",
        help="trim writes one stripped WAV; split writes one WAV per speech segment",
    )
    parser.add_argument(
        "--min-silence-ms",
        type=int,
        default=500,
        help="Silences shorter than this don't break a segment (default: 500)",
    )
    parser.add_argument(
        "--pad-ms", type=int, default=200, help="Padding around each speech region (default: 200)"
    )
    parser.add_argument(
        "--speech-floor-db",
        type=float,
        default=ss.SPEECH_RMS_DBFS_FLOOR,
        help="Drop regions whose RMS is below this dBFS (default: -45 dBFS)",
    )
    parser.add_argument(
        "--recursive", action="store_true", help="If input is a directory, recurse into subdirectories"
    )
    args = parser.parse_args()

    if not args.input.exists():
        print(f"ERROR: {args.input} not found", file=sys.stderr)
        sys.exit(1)

    targets = collect_targets(args.input, args.recursive)
    if not targets:
        print(f"No WAV files to process under {args.input}")
        return

    for t in targets:
        try:
            process_one(t, args.mode, args.min_silence_ms, args.pad_ms, args.speech_floor_db)
        except Exception as e:
            print(f"[strip-silence] {t}: FAILED: {e}")


if __name__ == "__main__":
    main()
