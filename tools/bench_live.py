#!/usr/bin/env python3
"""Benchmark the *live* transcription path on known audio.

Unlike `tools/bench_backends.py` (single-shot BATCH transcription), this
drives the exact production live pipeline a Bridge would hit:

    WAV → 20 ms PCM frames → SpeechGate (Silero VAD, the TapScribe gate)
        → WlKRelay → whisperlivekit-server subprocess → settled lines

It reuses the production objects verbatim — `WhisperLiveKitChannel`,
`build_gate_for_config`, `WlKRelay` — so the audio gets segmented and
decoded by the same code that runs live, and the captions it captures
are what the dashboard would have shown. Then it scores those captions
against a reference transcript with a BROAD metric set (WER/CER plus the
substitution/deletion/insertion breakdown, latency, and gate
pass-through) so the numbers themselves reveal what to tune rather than
us guessing up front.

Usage:
    # One WAV, one config (detailed report):
    python tools/bench_live.py tests/fixtures/audio/armstrong-en.wav --model base.en

    # Override gate / streaming knobs:
    python tools/bench_live.py tests/fixtures/audio/armstrong-en.wav \
        --model small.en --gate-kind backend --min-chunk-size 1.0

    # Sweep a config matrix across every fixture, write results JSON:
    python tools/bench_live.py --sweep

Requirements (heavy, optional extras):
    pip install -e ".[whisper,bench]"
i.e. whisperlivekit (provides whisperlivekit-server), faster-whisper
(CPU/CUDA) or mlx-whisper (Apple Silicon), plus jiwer for scoring.
silero-vad + torch (the gate) are already core dependencies.

Caveats:
  * Frames are paced in REAL TIME by default (--speed 1.0). Feeding
    faster than 1x changes WhisperLiveKit's time-sensitive
    LocalAgreement / buffer-trimming behaviour, so wall-clock RTF is ~1
    by construction — the meaningful latency signal is WlK's per-tick
    `lag_s` (how far behind real time the decoder is) and the
    finalization delay after the last frame.
  * The first run for a model includes weight download + load; the
    readiness wait is generous. If the model can't be fetched (offline
    box / network policy), the run reports an error instead of hanging
    forever.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
import wave
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
# Make the in-tree package importable when run as `python tools/bench_live.py`
# without an editable install (the script's own dir, not the repo root,
# is sys.path[0] in that case).
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tapscribe.live import LiveConfig, WhisperLiveKitChannel  # noqa: E402
from tapscribe.live_relay import WlKRelay  # noqa: E402
from tapscribe.speech_gate import FRAME_BYTES, SAMPLE_RATE, build_gate_for_config  # noqa: E402

DEFAULT_FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "audio"
RESULTS_DIR = REPO_ROOT / "bench-results"

FRAME_MS = 20
FRAME_INTERVAL_S = FRAME_MS / 1000.0  # 0.02 s — one 20 ms frame at 1x


# ---------------------------------------------------------------------------
# WAV → frames (wire-format only, mirrors tests/e2e/harness.py)
# ---------------------------------------------------------------------------


def read_wav_as_pcm_bytes(path: Path) -> bytes:
    """Raw 16 kHz mono int16 PCM body of a WAV. Raises if the file isn't
    already in the recorder's wire format — same contract the live /tap
    path enforces, so a misconverted fixture fails loudly here."""
    with wave.open(str(path), "rb") as w:
        if w.getnchannels() != 1 or w.getsampwidth() != 2 or w.getframerate() != SAMPLE_RATE:
            raise RuntimeError(
                f"{path.name}: expected {SAMPLE_RATE} Hz mono int16, got "
                f"{w.getframerate()} Hz / {w.getnchannels()}ch / {w.getsampwidth() * 8}-bit"
            )
        return w.readframes(w.getnframes())


def frame_pcm(pcm: bytes) -> list[bytes]:
    """Slice raw PCM into 20 ms (640-byte) frames; drop the trailing
    partial frame. Mirrors the Bridge wire pattern."""
    n = len(pcm) // FRAME_BYTES
    return [pcm[i * FRAME_BYTES : (i + 1) * FRAME_BYTES] for i in range(n)]


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

_PUNCT_RE = re.compile(r"[^\w\s]", flags=re.UNICODE)
_WS_RE = re.compile(r"\s+")


def normalize_text(s: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace. Language-neutral
    so it works for both English and Norwegian fixtures (no Whisper
    English normalizer assumptions)."""
    s = s.lower().replace("’", "'")
    s = _PUNCT_RE.sub(" ", s)
    return _WS_RE.sub(" ", s).strip()


@dataclass
class Score:
    wer: float
    cer: float
    hits: int
    substitutions: int
    deletions: int
    insertions: int
    ref_words: int
    hyp_words: int


def score_text(reference: str, hypothesis: str) -> Score:
    """WER/CER + the hit/sub/del/ins breakdown via jiwer.

    The breakdown is the point: deletions ⇒ words the live path dropped
    (gate clipping speech, VAC too aggressive); insertions ⇒
    hallucination / repeats; substitutions ⇒ weak model / wrong
    language. Lets the baseline self-diagnose instead of us pre-choosing
    a single metric to chase."""
    import jiwer

    ref_n = normalize_text(reference)
    hyp_n = normalize_text(hypothesis)
    # jiwer raises on an empty reference; an empty hypothesis is fine
    # (all deletions). Guard the degenerate empty-reference case.
    if not ref_n:
        raise ValueError("reference is empty after normalization")
    out = jiwer.process_words(ref_n, hyp_n)
    cer = jiwer.cer(ref_n, hyp_n) if hyp_n else 1.0
    return Score(
        wer=out.wer,
        cer=float(cer),
        hits=out.hits,
        substitutions=out.substitutions,
        deletions=out.deletions,
        insertions=out.insertions,
        ref_words=len(ref_n.split()),
        hyp_words=len(hyp_n.split()),
    )


# ---------------------------------------------------------------------------
# Run result
# ---------------------------------------------------------------------------


@dataclass
class RunResult:
    fixture: str
    config: dict
    error: str | None = None
    hypothesis: str = ""
    reference: str = ""
    settled_lines: list[str] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Live-path driver
# ---------------------------------------------------------------------------


async def _wait_until_ready(channel: WhisperLiveKitChannel, *, timeout: float) -> str | None:
    """Poll the channel's INFO state until it reaches 'running'. Returns
    None on success, else an error string. Detects the 'error' state and
    an early child exit so a model that can't download fails fast instead
    of hanging until the timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        state = channel.info.get("state")
        if state == "running":
            return None
        if state == "error":
            return channel.info.get("last_error") or "whisperlivekit-server reported error"
        if not channel.running():
            tail = " | ".join(list(channel.log)[-5:])
            return f"whisperlivekit-server exited early: {tail or channel.info.get('last_error', '')}"
        await asyncio.sleep(0.1)
    return f"whisperlivekit-server not ready within {timeout:.0f}s (model load/download too slow?)"


async def run_one(
    wav: Path,
    cfg: LiveConfig,
    *,
    use_mlx: bool,
    speed: float,
    warmup_s: float,
    ready_timeout: float,
    verbose: bool,
) -> RunResult:
    """Drive one WAV through the live path under one LiveConfig and score
    the captions. Owns the whisperlivekit-server child for the duration."""
    cfg_summary = _config_summary(cfg)
    reference = _read_reference(wav)
    frame_interval = FRAME_INTERVAL_S / max(speed, 0.01)

    channel = WhisperLiveKitChannel(config=cfg, use_mlx=use_mlx)
    ok, msg = channel.start()
    if not ok:
        return RunResult(
            fixture=wav.stem, config=cfg_summary, error=f"start failed: {msg}", reference=reference
        )

    try:
        if verbose:
            print(
                f"  [{wav.stem}] waiting for whisperlivekit-server (model={cfg.model}, mlx={use_mlx})...",
                flush=True,
            )
        err = await _wait_until_ready(channel, timeout=ready_timeout)
        if err is not None:
            return RunResult(fixture=wav.stem, config=cfg_summary, error=err, reference=reference)

        # After start() the channel mutated config.port to the picked
        # ephemeral port — connect the relay to the live values.
        host, port = channel.config.host, channel.config.port

        settled: list[tuple[float, str]] = []
        lag_samples: list[float] = []
        t0 = time.perf_counter()

        def on_settled(text: str) -> None:
            settled.append((time.perf_counter() - t0, text))

        async def on_metrics(lag: float) -> None:
            lag_samples.append(lag)

        gate = build_gate_for_config(cfg)
        relay = WlKRelay(
            host=host,
            port=port,
            language=cfg.language,
            on_settled_line=on_settled,
            on_metrics=on_metrics,
            drain_timeout=3.0,
        )
        if not await relay.connect():
            return RunResult(
                fixture=wav.stem, config=cfg_summary, error="WlK relay connect failed", reference=reference
            )

        # Warm-up: feed silence so a still-loading ASR worker is ready
        # before the first real utterance, and so the first words aren't
        # clipped by model warm-up. Silence passes through the gate as
        # non-speech (or straight through when gate_kind=backend).
        if warmup_s > 0:
            silence = b"\x00" * FRAME_BYTES
            for _ in range(int(warmup_s / FRAME_INTERVAL_S)):
                await relay.send(silence)
                await asyncio.sleep(frame_interval)

        frames = frame_pcm(read_wav_as_pcm_bytes(wav))
        audio_s = len(frames) * FRAME_INTERVAL_S
        frames_forwarded = 0
        feed_start = time.perf_counter()
        for frame in frames:
            out = gate.feed(frame) if gate is not None else [frame]
            for f in out:
                if not await relay.send(f):
                    break
                frames_forwarded += 1
            await asyncio.sleep(frame_interval)
        last_frame_wall = time.perf_counter() - t0

        await relay.close()
        wall_s = time.perf_counter() - feed_start

        lines = [t for _, t in settled]
        hypothesis = " ".join(lines)
        last_settled_wall = settled[-1][0] if settled else last_frame_wall
        final_delay = max(0.0, last_settled_wall - last_frame_wall)

        metrics: dict = {
            "audio_s": round(audio_s, 2),
            "wall_s": round(wall_s, 2),
            "n_lines": len(lines),
            "frames_in": len(frames),
            "frames_forwarded": frames_forwarded,
            "gate_forward_pct": round(100.0 * frames_forwarded / max(1, len(frames)), 1),
            "lag_mean_s": round(sum(lag_samples) / len(lag_samples), 3) if lag_samples else None,
            "lag_max_s": round(max(lag_samples), 3) if lag_samples else None,
            "final_delay_s": round(final_delay, 2),
        }

        result = RunResult(
            fixture=wav.stem,
            config=cfg_summary,
            hypothesis=hypothesis,
            reference=reference,
            settled_lines=lines,
            metrics=metrics,
        )
        try:
            score = score_text(reference, hypothesis)
            result.metrics.update(asdict(score))
        except Exception as e:  # scoring is best-effort; keep raw output
            result.metrics["score_error"] = str(e)
        return result
    finally:
        channel.stop()


# ---------------------------------------------------------------------------
# Config / fixtures helpers
# ---------------------------------------------------------------------------

# Knobs we vary; everything else stays at LiveConfig defaults. Kept as a
# tuple so the table printer and the JSON summary agree on the columns.
_SUMMARY_FIELDS = (
    "model",
    "language",
    "gate_kind",
    "gate_min_speech_ms",
    "min_chunk_size",
    "buffer_trimming",
    "buffer_trimming_sec",
    "confidence_validation",
)


def _config_summary(cfg: LiveConfig) -> dict:
    return {f: getattr(cfg, f) for f in _SUMMARY_FIELDS}


def _read_reference(wav: Path) -> str:
    ref = wav.with_suffix("").with_suffix(".reference.txt")
    if not ref.exists():
        # `armstrong-en.wav` → `armstrong-en.reference.txt`
        ref = wav.parent / f"{wav.stem}.reference.txt"
    return ref.read_text(encoding="utf-8").strip() if ref.exists() else ""


def discover_fixtures(fixture_dir: Path) -> list[Path]:
    """Every *.wav in the dir that has a paired *.reference.txt."""
    out = []
    for wav in sorted(fixture_dir.glob("*.wav")):
        ref = fixture_dir / f"{wav.stem}.reference.txt"
        if ref.exists():
            out.append(wav)
    return out


def detect_use_mlx() -> bool:
    """MLX is the natural live backend on Apple Silicon when mlx-whisper
    is importable. Everywhere else (incl. this Linux CI box) use the
    faster-whisper CPU/CUDA path."""
    import platform

    if platform.system() != "Darwin" or platform.machine() != "arm64":
        return False
    try:
        import mlx_whisper  # noqa: F401

        return True
    except ImportError:
        return False


# Language per fixture stem so the sweep picks the right WlK --lan.
_FIXTURE_LANG = {"marlene-nb": "no"}


def _lang_for_fixture(wav: Path) -> str:
    return _FIXTURE_LANG.get(wav.stem, "en")


# Full-pipeline sweep matrix: each dict is a set of LiveConfig field
# overrides applied (via dataclasses.replace) onto a per-fixture baseline
# (the fixture's language). Row 0 of each matrix is the baseline every
# other row is compared against. Edit freely; any LiveConfig field name is
# a valid key.
#
# There are two matrices because the MODEL has to match the fixture's
# language: English-only Whisper (`*.en`) cannot transcribe Norwegian
# regardless of `--lan`, so a Norwegian fixture swept on `.en` models
# scores pure noise (this is exactly the bogus `marlene-nb` baseline that
# the first sweep produced). `_sweep_matrix_for` picks the right one.
#
# The non-model rows are chosen to isolate the hypotheses in
# docs/live-tuning-research.md: model size (1-3), the confidence/accuracy
# trade (4), whether our gate clips speech vs the backend VAD (5), blip
# suppression (6), and the WlK streaming knobs (7-8).

# English fixtures — English-only Whisper. Row 0 is the PRODUCTION DEFAULT.
SWEEP_MATRIX_EN: list[dict] = [
    {"model": "tiny.en"},  # production default — baseline
    {"model": "base.en"},
    {"model": "small.en"},
    {"model": "small.en", "confidence_validation": False},
    {"model": "small.en", "gate_kind": "backend"},
    {"model": "small.en", "gate_min_speech_ms": 200},
    {"model": "small.en", "min_chunk_size": 1.0},
    {"model": "small.en", "buffer_trimming": "segment"},
]

# Norwegian fixtures — NB-Whisper (NbAiLab, Norwegian-tuned). The channel
# auto-downloads the CT2 weights and build_live_cmd routes these via
# --model-path + --backend-policy localagreement; every other knob below
# still applies. Mirrors the EN matrix so the two are read side by side.
SWEEP_MATRIX_NB: list[dict] = [
    {"model": "nb-whisper-tiny"},  # Norwegian baseline
    {"model": "nb-whisper-base"},
    {"model": "nb-whisper-small"},
    {"model": "nb-whisper-small", "confidence_validation": False},
    {"model": "nb-whisper-small", "gate_kind": "backend"},
    {"model": "nb-whisper-small", "gate_min_speech_ms": 200},
    {"model": "nb-whisper-small", "min_chunk_size": 1.0},
    {"model": "nb-whisper-small", "buffer_trimming": "segment"},
]


def _sweep_matrix_for(wav: Path) -> list[dict]:
    """Pick the model sweep that matches the fixture's language. Norwegian
    fixtures need NB-Whisper; everything else uses the English `.en`
    matrix."""
    return SWEEP_MATRIX_NB if _lang_for_fixture(wav) == "no" else SWEEP_MATRIX_EN


def _config_from_overrides(overrides: dict, *, language: str, host: str) -> LiveConfig:
    """Build a LiveConfig from a per-fixture baseline plus a matrix row.
    `replace` applies any LiveConfig field, so a row can tune gate knobs,
    confidence_validation, streaming knobs — not just the hardcoded few."""
    base = LiveConfig(model="tiny.en", language=language, host=host, port=0)
    return replace(base, **overrides)


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def _fmt(v: object, width: int) -> str:
    if v is None:
        s = "-"
    elif isinstance(v, float):
        s = f"{v:.2f}"
    else:
        s = str(v)
    return s[:width].rjust(width)


def print_table(results: list[RunResult]) -> None:
    cols = [
        ("fixture", 12, lambda r: r.fixture),
        ("model", 9, lambda r: r.config.get("model")),
        ("gate", 9, lambda r: r.config.get("gate_kind")),
        ("WER", 6, lambda r: r.metrics.get("wer")),
        ("CER", 6, lambda r: r.metrics.get("cer")),
        ("sub", 4, lambda r: r.metrics.get("substitutions")),
        ("del", 4, lambda r: r.metrics.get("deletions")),
        ("ins", 4, lambda r: r.metrics.get("insertions")),
        ("lagμ", 6, lambda r: r.metrics.get("lag_mean_s")),
        ("lagX", 6, lambda r: r.metrics.get("lag_max_s")),
        ("finΔ", 6, lambda r: r.metrics.get("final_delay_s")),
        ("gate%", 6, lambda r: r.metrics.get("gate_forward_pct")),
        ("lines", 5, lambda r: r.metrics.get("n_lines")),
    ]
    header = " ".join(name.rjust(w) if i else name.ljust(w) for i, (name, w, _) in enumerate(cols))
    print("=" * len(header))
    print(header)
    print("-" * len(header))
    for r in results:
        if r.error:
            print(f"{r.fixture[:12].ljust(12)} {r.config.get('model', '')[:9]:>9}  ERROR: {r.error}")
            continue
        row = []
        for i, (_name, w, get) in enumerate(cols):
            v = get(r)
            row.append(v[:w].ljust(w) if i == 0 and isinstance(v, str) else _fmt(v, w))
        print(" ".join(row))
    print("=" * len(header))
    print(
        "WER/CER lower=better. sub/del/ins = word substitutions / deletions (dropped) / "
        "insertions (hallucinated)."
    )
    print(
        "lagμ/lagX = mean/max WlK decode lag (s); finΔ = finalization delay after last frame (s); "
        "gate% = frames forwarded."
    )


def write_results_json(results: list[RunResult], *, use_mlx: bool, speed: float) -> Path:
    RESULTS_DIR.mkdir(exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = RESULTS_DIR / f"live-{stamp}.json"
    payload = {
        "created": datetime.now(UTC).isoformat(),
        "host_backend": "mlx-whisper" if use_mlx else "faster-whisper",
        "speed": speed,
        "runs": [
            {
                "fixture": r.fixture,
                "config": r.config,
                "error": r.error,
                "metrics": r.metrics,
                "reference": r.reference,
                "hypothesis": r.hypothesis,
                "settled_lines": r.settled_lines,
            }
            for r in results
        ],
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def print_detailed(result: RunResult) -> None:
    print()
    print(f"fixture:  {result.fixture}")
    print(f"config:   {json.dumps(result.config)}")
    if result.error:
        print(f"ERROR:    {result.error}")
        return
    print(f"reference:  {result.reference!r}")
    print(f"hypothesis: {result.hypothesis!r}")
    print()
    print("settled lines:")
    for ln in result.settled_lines:
        print(f"  · {ln}")
    print()
    print("metrics:")
    for k, v in result.metrics.items():
        print(f"  {k:18} {v}")


# ---------------------------------------------------------------------------
# Gate-only analysis (no ASR model — Silero loads locally)
# ---------------------------------------------------------------------------
#
# The SpeechGate is the front half of the live path and a prime quality
# suspect: if it clips speech, words are dropped before the backend ever
# sees them. Silero VAD loads locally with no network, so we can measure
# exactly how much audio each gate config forwards — useful on boxes that
# can't download an ASR model (and fast: no subprocess, no real-time
# pacing). This is NOT a transcription benchmark; it's a gate-aggression
# benchmark.

_GATE_SUMMARY_FIELDS = (
    "gate_kind",
    "gate_speech_threshold",
    "gate_hangover_ms",
    "gate_pre_roll_ms",
    "gate_min_speech_ms",
)

# Gate-config matrix for --gate-only. Row 0 is the production default.
GATE_MATRIX: list[dict] = [
    {},  # production default: thr 0.5, hang 400, pre-roll 300, min-speech 0
    {"gate_speech_threshold": 0.3},
    {"gate_speech_threshold": 0.7},
    {"gate_hangover_ms": 800},
    {"gate_pre_roll_ms": 500},
    {"gate_min_speech_ms": 200},
    {"gate_kind": "backend"},  # no TapScribe gate → forwards everything
]


@dataclass
class GateStats:
    fixture: str
    config: dict
    frames_in: int
    frames_forwarded: int
    forward_pct: float
    segments: int
    retained_s: float
    audio_s: float


def analyze_gate(wav: Path, cfg: LiveConfig) -> GateStats:
    """Feed a WAV through the SpeechGate and report how much it forwards.

    `segments` counts silence→speech openings (gate.is_open False→True),
    a proxy for how finely the gate chops the audio. `gate_kind=backend`
    means no TapScribe gate, so everything is forwarded as one segment."""
    gate = build_gate_for_config(cfg)
    frames = frame_pcm(read_wav_as_pcm_bytes(wav))
    forwarded = 0
    segments = 0
    prev_open = False
    for fr in frames:
        out = gate.feed(fr) if gate is not None else [fr]
        forwarded += len(out)
        now_open = gate.is_open if gate is not None else True
        if now_open and not prev_open:
            segments += 1
        prev_open = now_open
    n = len(frames)
    return GateStats(
        fixture=wav.stem,
        config={f: getattr(cfg, f) for f in _GATE_SUMMARY_FIELDS},
        frames_in=n,
        frames_forwarded=forwarded,
        forward_pct=round(100.0 * forwarded / max(1, n), 1),
        segments=segments,
        retained_s=round(forwarded * FRAME_INTERVAL_S, 2),
        audio_s=round(n * FRAME_INTERVAL_S, 2),
    )


def print_gate_table(rows: list[GateStats]) -> None:
    cols = [
        ("fixture", 12, lambda r: r.fixture),
        ("kind", 9, lambda r: r.config.get("gate_kind")),
        ("thr", 5, lambda r: r.config.get("gate_speech_threshold")),
        ("hang", 5, lambda r: r.config.get("gate_hangover_ms")),
        ("proll", 5, lambda r: r.config.get("gate_pre_roll_ms")),
        ("minsp", 5, lambda r: r.config.get("gate_min_speech_ms")),
        ("fwd%", 6, lambda r: r.forward_pct),
        ("segs", 5, lambda r: r.segments),
        ("kept_s", 7, lambda r: r.retained_s),
        ("audio_s", 7, lambda r: r.audio_s),
    ]
    header = " ".join(name.rjust(w) if i else name.ljust(w) for i, (name, w, _) in enumerate(cols))
    print("=" * len(header))
    print(header)
    print("-" * len(header))
    for r in rows:
        cells = []
        for i, (_n, w, get) in enumerate(cols):
            v = get(r)
            cells.append(v[:w].ljust(w) if i == 0 and isinstance(v, str) else _fmt(v, w))
        print(" ".join(cells))
    print("=" * len(header))
    print(
        "fwd% = frames forwarded to backend; segs = silence→speech openings; "
        "kept_s = forwarded audio seconds."
    )
    print("A low fwd% on a mostly-speech clip suggests the gate is clipping speech (→ dropped words).")


def run_gate_sweep(fixture_dir: Path) -> list[GateStats]:
    fixtures = discover_fixtures(fixture_dir)
    if not fixtures:
        print(f"No fixtures under {fixture_dir}", file=sys.stderr)
        return []
    rows: list[GateStats] = []
    for wav in fixtures:
        for overrides in GATE_MATRIX:
            cfg = _config_from_overrides(overrides, language=_lang_for_fixture(wav), host="127.0.0.1")
            rows.append(analyze_gate(wav, cfg))
    return rows


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_base_config(args) -> LiveConfig:
    kwargs: dict = dict(
        model=args.model,
        language=args.language,
        host=args.live_host,
        port=0,  # ephemeral — channel picks a free port at spawn
        gate_kind=args.gate_kind,
    )
    if args.min_chunk_size is not None:
        kwargs["min_chunk_size"] = args.min_chunk_size
    if args.buffer_trimming is not None:
        kwargs["buffer_trimming"] = args.buffer_trimming
    if args.buffer_trimming_sec is not None:
        kwargs["buffer_trimming_sec"] = args.buffer_trimming_sec
    if args.gate_min_speech_ms is not None:
        kwargs["gate_min_speech_ms"] = args.gate_min_speech_ms
    return LiveConfig(**kwargs)


async def run_sweep(args, *, use_mlx: bool) -> list[RunResult]:
    fixtures = discover_fixtures(Path(args.fixture_dir))
    if not fixtures:
        print(f"No fixtures (paired *.wav + *.reference.txt) under {args.fixture_dir}", file=sys.stderr)
        return []
    results: list[RunResult] = []
    total = sum(len(_sweep_matrix_for(w)) for w in fixtures)
    i = 0
    for wav in fixtures:
        lang = _lang_for_fixture(wav)
        for overrides in _sweep_matrix_for(wav):
            i += 1
            cfg = _config_from_overrides(overrides, language=lang, host=args.live_host)
            print(f"[{i}/{total}] {wav.stem}  {_config_summary(cfg)}", flush=True)
            res = await run_one(
                wav,
                cfg,
                use_mlx=use_mlx,
                speed=args.speed,
                warmup_s=args.warmup_s,
                ready_timeout=args.ready_timeout,
                verbose=args.verbose,
            )
            if res.error:
                print(f"      -> ERROR: {res.error}", flush=True)
            else:
                m = res.metrics
                print(
                    f"      -> WER={m.get('wer')} del={m.get('deletions')} "
                    f"ins={m.get('insertions')} lagX={m.get('lag_max_s')}",
                    flush=True,
                )
            results.append(res)
    return results


def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("wav", nargs="?", type=Path, help="WAV to stream (omit with --sweep)")
    p.add_argument("--sweep", action="store_true", help="Run the config matrix over every fixture")
    p.add_argument(
        "--gate-only",
        action="store_true",
        help="Model-free: sweep GATE configs over every fixture and report how much audio each "
        "forwards (no ASR model needed — Silero loads locally).",
    )
    p.add_argument("--fixture-dir", default=str(DEFAULT_FIXTURE_DIR), help="Fixture directory for --sweep")
    p.add_argument(
        "--model", default="tiny.en", help="WhisperLiveKit model (default: tiny.en, the prod default)"
    )
    p.add_argument("--language", default="en", help="Language hint (en, no, auto). Default: en")
    p.add_argument("--gate-kind", choices=("tapscribe", "backend"), default="tapscribe")
    p.add_argument("--gate-min-speech-ms", type=int, default=None)
    p.add_argument("--min-chunk-size", type=float, default=None)
    p.add_argument("--buffer-trimming", choices=("sentence", "segment"), default=None)
    p.add_argument("--buffer-trimming-sec", type=float, default=None)
    p.add_argument("--live-host", default="127.0.0.1")
    p.add_argument(
        "--speed",
        type=float,
        default=1.0,
        help="Frame pacing multiplier. 1.0 = real time (faithful). >1 is faster but "
        "changes WlK's time-based commit behaviour — use with caution.",
    )
    p.add_argument(
        "--warmup-s", type=float, default=0.5, help="Seconds of silence to prime the decoder first"
    )
    p.add_argument(
        "--ready-timeout",
        type=float,
        default=240.0,
        help="Max seconds to wait for whisperlivekit-server to come up (first run downloads weights)",
    )
    p.add_argument("--mlx", dest="mlx", action="store_true", default=None, help="Force MLX backend")
    p.add_argument("--no-mlx", dest="mlx", action="store_false", help="Force faster-whisper (CPU/CUDA)")
    p.add_argument("--json", action="store_true", help="Also write a results JSON (always on for --sweep)")
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args()

    use_mlx = detect_use_mlx() if args.mlx is None else args.mlx

    # Gate-only mode needs neither jiwer nor an ASR model — handle it
    # before the scoring-dep check so it runs on a model-less box.
    if args.gate_only:
        rows = run_gate_sweep(Path(args.fixture_dir))
        if not rows:
            sys.exit(1)
        print()
        print_gate_table(rows)
        return

    # Fail fast with an actionable message if jiwer isn't importable —
    # scoring is the whole point and a cryptic ImportError mid-run wastes
    # a model load.
    try:
        import jiwer  # noqa: F401
    except ImportError:
        print("ERROR: jiwer is required for scoring. Install with: pip install jiwer", file=sys.stderr)
        sys.exit(1)

    if args.sweep:
        results = asyncio.run(run_sweep(args, use_mlx=use_mlx))
        if not results:
            sys.exit(1)
        print()
        print_table(results)
        out = write_results_json(results, use_mlx=use_mlx, speed=args.speed)
        print(f"\nresults written to {out}")
        return

    if args.wav is None:
        p.error("provide a WAV path or use --sweep")
    if not args.wav.exists():
        print(f"ERROR: {args.wav} not found", file=sys.stderr)
        sys.exit(1)

    cfg = build_base_config(args)
    print(
        f"backend: {'mlx-whisper' if use_mlx else 'faster-whisper'}  config: {_config_summary(cfg)}",
        flush=True,
    )
    result = asyncio.run(
        run_one(
            args.wav,
            cfg,
            use_mlx=use_mlx,
            speed=args.speed,
            warmup_s=args.warmup_s,
            ready_timeout=args.ready_timeout,
            verbose=True,
        )
    )
    print_detailed(result)
    print()
    print_table([result])
    if args.json:
        out = write_results_json([result], use_mlx=use_mlx, speed=args.speed)
        print(f"\nresults written to {out}")
    if result.error:
        sys.exit(2)


if __name__ == "__main__":
    main()
