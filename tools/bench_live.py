#!/usr/bin/env python3
"""Benchmark the *live* transcription path on known audio.

Unlike `tools/bench_backends.py` (single-shot BATCH transcription), this
drives the exact production live pipeline a Bridge would hit:

    WAV → 20 ms PCM frames → TapFanOut (SpeechGate + WlKRelay) → Recorder
        → whisperlivekit-server subprocess → ActiveStreams / LiveTranscripts

It drives the production path verbatim — a real `Recorder` and
`TapFanOut.write_frame`, exactly as the /tap WebSocket endpoint does — so
the gate decisions, the per-tap lag, the in-flight buffer, and the settled
captions all come from the same code that runs live (read back from
`recorder.streams` / `recorder.transcripts`, the dashboard's own sources).
There is no parallel reimplementation to drift out of sync. It scores those captions
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
import contextlib
import json
import re
import sys
import tempfile
import time
import uuid
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
from tapscribe.recorder import Recorder  # noqa: E402
from tapscribe.speech_gate import FRAME_BYTES, SAMPLE_RATE, build_gate_for_config  # noqa: E402
from tapscribe.tap_fan_out import TapFanOut  # noqa: E402

DEFAULT_FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "audio"
RESULTS_DIR = REPO_ROOT / "bench-results"

FRAME_MS = 20
FRAME_INTERVAL_S = FRAME_MS / 1000.0  # 0.02 s — one 20 ms frame at 1x

# How often (wall-clock seconds) a background task samples the recorder's
# per-tap state, matching the dashboard's poll-based view of lag / gate_open /
# buffer. Kept OFF the feed loop so sampling overhead can't slow the feed
# below real time (which would silently under-load WlK as N grows).
STATE_SAMPLE_INTERVAL_S = 0.1


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


def _build_bench_recorder(cfg: LiveConfig, use_mlx: bool) -> Recorder:
    """A throwaway production Recorder (temp dirs) wired to a real
    WhisperLiveKitChannel from `cfg`. The bench drives this exactly like
    the /tap endpoint does, so it exercises the live path verbatim."""
    tmp = Path(tempfile.mkdtemp(prefix="bench-live-"))
    (tmp / "recordings").mkdir()
    (tmp / "config").mkdir()
    return Recorder(
        recordings_dir=tmp / "recordings",
        config_dir=tmp / "config",
        live_config=cfg,
        use_mlx=use_mlx,
        auth_password_file=tmp / ".auth-password",
    )


async def _drive_one_stream(
    recorder: Recorder,
    *,
    identity: str,
    name: str,
    frames: list[bytes],
    speed: float,
    start_delay_s: float = 0.0,
) -> tuple[dict, str, list[str]]:
    """Drive ONE gated stream through the PRODUCTION fan-out and return
    (per-stream metrics, hypothesis, settled lines).

    Opens a real `TapFanOut` against `recorder` and feeds it 20 ms frames
    exactly as the /tap WS would, so the SpeechGate, the WlKRelay, the
    per-tap `lag_s` (with its gate-closed suppression), the in-flight
    `buffer_transcription`, and the settled captions are all produced by
    the production code — no parallel reimplementation to drift out of
    sync. lag / gate_open / buffer are sampled from `recorder.streams`
    (the same snapshot `/api/state` serves); settled lines are read back
    from `recorder.transcripts`, filtered to this stream's identity.

    `start_delay_s` delays *opening* the tap, modelling a real bridge that
    opens a fresh /tap WS when its speaker starts an utterance (the bridge
    contract is one WebSocket per utterance). Staggering the open — rather
    than padding a long-lived connection with leading silence — keeps WlK's
    per-connection clock anchored at speech onset, so lag isn't inflated by
    silence that, in production, simply wouldn't be on an open connection."""
    if start_delay_s > 0:
        await asyncio.sleep(start_delay_s)
    frame_interval = FRAME_INTERVAL_S / max(speed, 0.01)

    utterance_id = f"bench-{identity}-{uuid.uuid4().hex[:8]}"
    fan = await TapFanOut.open(
        recorder,
        identity=identity,
        name=name,
        utterance_id=utterance_id,
        do_record=False,
        do_live=True,
    )
    # Live was requested but the relay never attached (WlK down / connect
    # failed): the stream can't produce captions, so flag it errored rather
    # than letting it masquerade as a zero-caption (WER 1.0) result.
    if not fan._relay_alive:
        await fan._close()
        return {"error": "WlK relay did not connect"}, "", []
    conn_id = fan._conn_id

    lag_samples: list[float] = []
    buffers: list[str] = []
    gate_open_hits = 0
    samples = 0

    async def _sample() -> None:
        nonlocal gate_open_hits, samples
        for s in await recorder.streams.snapshot():
            if s.conn_id != conn_id:
                continue
            samples += 1
            if s.gate_open:
                gate_open_hits += 1
            if s.lag_s is not None:
                lag_samples.append(s.lag_s)
            buf = (s.buffer_transcription or "").strip()
            if buf:
                buffers.append(buf)
            break

    # Sample per-tap state from a background task, off the feed path.
    sampling = True

    async def _sampler() -> None:
        while sampling:
            await _sample()
            await asyncio.sleep(STATE_SAMPLE_INTERVAL_S)

    sampler_task = asyncio.create_task(_sampler())

    # try/finally so a mid-feed exception can't leak the sampler task or the
    # tap's open relay + ActiveStream — important under --concurrency, where a
    # leaked stream would otherwise dangle for the rest of the sweep.
    try:
        # Pace against an ABSOLUTE schedule (target = start + i*interval) so
        # per-frame production work (gate, relay send, ActiveStream lock) is
        # absorbed instead of stacked on top of a fixed sleep — otherwise the
        # feed drifts below real time as N grows and WlK looks falsely
        # under-loaded. `max_slip` is how far behind schedule we fell: > ~0
        # means the host couldn't feed this many streams in real time, so the
        # numbers are soft.
        start = time.perf_counter()
        max_slip = 0.0
        for i, frame in enumerate(frames):
            await fan.write_frame(frame)
            slip = time.perf_counter() - (start + (i + 1) * frame_interval)
            max_slip = max(max_slip, slip)
            if slip < 0:
                await asyncio.sleep(-slip)
        last_frame_wall = time.perf_counter()
        sampling = False
        await sampler_task
        await _sample()
    finally:
        sampling = False
        if not sampler_task.done():
            sampler_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sampler_task
        await fan._close()
    final_delay = time.perf_counter() - last_frame_wall

    lines = [e["text"] for e in recorder.transcripts.snapshot() if e.get("identity") == identity]
    hypothesis = " ".join(lines)
    metrics: dict = {
        "n_lines": len(lines),
        "frames_in": len(frames),
        "gate_open_pct": round(100.0 * gate_open_hits / max(1, samples), 1),
        "lag_mean_s": round(sum(lag_samples) / len(lag_samples), 3) if lag_samples else None,
        "lag_max_s": round(max(lag_samples), 3) if lag_samples else None,
        "lag_samples": len(lag_samples),
        "final_delay_s": round(final_delay, 2),
        "pacing_slip_s": round(max(0.0, max_slip), 2),
        "buffer_nonempty": len(buffers),
        "buffer_sample": buffers[-1] if buffers else "",
    }
    return metrics, hypothesis, lines


async def run_one(
    wav: Path,
    cfg: LiveConfig,
    *,
    use_mlx: bool,
    speed: float,
    ready_timeout: float,
    verbose: bool,
) -> RunResult:
    """Drive one WAV through the production live path under one LiveConfig
    and score the captions. Owns the whisperlivekit-server child for the
    duration."""
    cfg_summary = _config_summary(cfg)
    reference = _read_reference(wav)

    recorder = _build_bench_recorder(cfg, use_mlx)
    ok, msg = recorder.live.start()
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
        err = await _wait_until_ready(recorder.live, timeout=ready_timeout)
        if err is not None:
            return RunResult(fixture=wav.stem, config=cfg_summary, error=err, reference=reference)

        recorder.transcripts.clear()
        frames = frame_pcm(read_wav_as_pcm_bytes(wav))
        audio_s = len(frames) * FRAME_INTERVAL_S
        feed_start = time.perf_counter()
        smetrics, hypothesis, lines = await _drive_one_stream(
            recorder, identity="bench", name="Bench Speaker", frames=frames, speed=speed
        )
        wall_s = time.perf_counter() - feed_start
        if "error" in smetrics:
            return RunResult(
                fixture=wav.stem, config=cfg_summary, error=smetrics["error"], reference=reference
            )

        metrics: dict = {"audio_s": round(audio_s, 2), "wall_s": round(wall_s, 2), **smetrics}
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
        recorder.live.stop()


def _aggregate_streams(outs: list[tuple[dict, str, list]], n: int, reference: str) -> dict:
    errored = sum(1 for m, _, _ in outs if "error" in m)
    lag_means = [m["lag_mean_s"] for m, _, _ in outs if m.get("lag_mean_s") is not None]
    lag_maxes = [m["lag_max_s"] for m, _, _ in outs if m.get("lag_max_s") is not None]
    fin = [m["final_delay_s"] for m, _, _ in outs if "final_delay_s" in m]
    slips = [m["pacing_slip_s"] for m, _, _ in outs if "pacing_slip_s" in m]
    wers: list[float] = []
    for m, hyp, _l in outs:
        if "error" in m:
            continue  # didn't run — counted via `errored`, kept out of the WER mean
        try:
            wers.append(score_text(reference, hyp).wer)
        except Exception:  # empty reference etc. — skip; can't score
            pass
    return {
        "streams": n,
        "errored": errored,
        "lag_mean_s": round(sum(lag_means) / len(lag_means), 2) if lag_means else None,
        "lag_max_s": round(max(lag_maxes), 2) if lag_maxes else None,
        "wer_mean": round(sum(wers) / len(wers), 2) if wers else None,
        "fin_delay_max_s": round(max(fin), 2) if fin else None,
        "pacing_slip_s": round(max(slips), 2) if slips else None,
    }


def _print_concurrency_row(row: dict) -> None:
    print(
        f"  streams={row['streams']}: lagμ={row['lag_mean_s']} lagX={row['lag_max_s']} "
        f"WERμ={row['wer_mean']} finΔX={row['fin_delay_max_s']} slip={row.get('pacing_slip_s')} "
        f"errored={row['errored']}",
        flush=True,
    )


async def run_concurrency_sweep(
    wav: Path,
    cfg: LiveConfig,
    *,
    counts: list[int],
    stagger_s: float,
    use_mlx: bool,
    speed: float,
    ready_timeout: float,
    verbose: bool,
) -> list[dict]:
    """Stress the production live path with N concurrent gated /tap streams
    of `wav` and report how lag / WER degrade as N grows.

    This is the multi-speaker reproduction, driven exactly as production
    runs it: ONE shared `WhisperLiveKitChannel` (loaded once, reused across
    all N) with N concurrent `TapFanOut` streams relaying into it — every
    active tap contends for the one decoder, same as the real /tap fan-out.
    Each stream gets its own identity so its settled captions can be read
    back from `recorder.transcripts`.

    `stagger_s` offsets when each stream OPENS its tap (`stagger_s * index`),
    modelling the bridge contract of one /tap WebSocket per utterance: a
    later speaker's connection opens when they start talking, not at t0. So
    stagger=0 is full overlap (all taps open at once) and stagger >= clip
    length is pure turn-taking (taps open and close one after another, ~1
    connection live at a time). Staggering the OPEN — vs. padding a single
    long-lived connection with leading silence — keeps WlK's per-connection
    clock anchored at speech onset so lag reflects real backlog."""
    reference = _read_reference(wav)
    frames = frame_pcm(read_wav_as_pcm_bytes(wav))

    recorder = _build_bench_recorder(cfg, use_mlx)
    ok, msg = recorder.live.start()
    if not ok:
        print(f"start failed: {msg}", file=sys.stderr)
        return []

    rows: list[dict] = []
    try:
        err = await _wait_until_ready(recorder.live, timeout=ready_timeout)
        if err is not None:
            print(f"whisperlivekit-server not ready: {err}", file=sys.stderr)
            return []

        for n in counts:
            recorder.transcripts.clear()
            # return_exceptions=True so one stream blowing up is recorded as an
            # errored row instead of aborting the sweep (and losing the rows
            # already computed for smaller N). _drive_one_stream's try/finally
            # has already closed that stream's tap by the time we see the exc.
            raw = await asyncio.gather(
                *(
                    _drive_one_stream(
                        recorder,
                        identity=f"spk{i}",
                        name=f"Speaker {i}",
                        frames=frames,
                        speed=speed,
                        start_delay_s=stagger_s * i,
                    )
                    for i in range(n)
                ),
                return_exceptions=True,
            )
            outs = [({"error": repr(o)}, "", []) if isinstance(o, BaseException) else o for o in raw]
            row = _aggregate_streams(outs, n, reference)
            rows.append(row)
            if verbose:
                _print_concurrency_row(row)
    finally:
        recorder.live.stop()
    return rows


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
        ("gateOn%", 7, lambda r: r.metrics.get("gate_open_pct")),
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


def print_concurrency_table(rows: list[dict], *, wav: Path, cfg: LiveConfig, stagger_s: float) -> None:
    overlap = "full overlap" if stagger_s == 0 else f"speech staggered {stagger_s:g}s/stream"
    print(
        f"concurrency stress — fixture={wav.stem} model={cfg.model} gate={cfg.gate_kind} "
        f"({overlap}, 1 shared server, production fan-out)"
    )
    cols = [
        ("streams", 8, lambda r: r.get("streams")),
        ("lagμ", 7, lambda r: r.get("lag_mean_s")),
        ("lagX", 7, lambda r: r.get("lag_max_s")),
        ("finΔX", 7, lambda r: r.get("fin_delay_max_s")),
        ("slipX", 7, lambda r: r.get("pacing_slip_s")),
        ("WERμ", 7, lambda r: r.get("wer_mean")),
        ("errored", 8, lambda r: r.get("errored")),
    ]
    header = " ".join(name.rjust(w) for name, w, _ in cols)
    print("=" * len(header))
    print(header)
    print("-" * len(header))
    for r in rows:
        print(" ".join(_fmt(get(r), w) for _name, w, get in cols))
    print("=" * len(header))
    print(
        "lag/WER are read from the production ActiveStreams + LiveTranscripts (same source as the "
        "dashboard). Each stream opens its tap at stagger*i (one /tap per utterance, as the bridge"
    )
    print(
        "does). lagμ/lagX climbing with N = decode contention; WERμ rising / errored>0 = dropped"
        " speech. slipX = worst real-time pacing slip; if it's > ~0.1s the host couldn't feed N"
    )
    print(
        "streams in real time, so lag/WER for that row are soft (the feed under-loaded WlK)."
        " Compare stagger=0 (all overlap) vs large stagger (turn-taking, ~1 tap live)."
    )


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
    if args.confidence_validation is not None:
        kwargs["confidence_validation"] = args.confidence_validation
    if args.backend_policy is not None:
        kwargs["backend_policy"] = args.backend_policy
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
    p.add_argument(
        "--concurrency",
        default=None,
        metavar="N1,N2,...",
        help="Multi-speaker stress: feed this many concurrent copies of the fixture into one WlK "
        "server (e.g. '1,2,3,4') and report how lag/WER degrade with load. Uses --wav (default: "
        "armstrong-en).",
    )
    p.add_argument(
        "--stagger",
        type=float,
        default=0.0,
        metavar="SECONDS",
        help="With --concurrency: offset when each stream OPENS its tap by this many seconds (one "
        "/tap per utterance, as the bridge does). 0 = all taps open at once (full overlap); >= clip "
        "length = pure turn-taking (taps open/close in sequence, ~1 live at a time).",
    )
    p.add_argument("--fixture-dir", default=str(DEFAULT_FIXTURE_DIR), help="Fixture directory for --sweep")
    p.add_argument(
        "--model", default="tiny.en", help="WhisperLiveKit model (default: tiny.en, the prod default)"
    )
    p.add_argument("--language", default="en", help="Language hint (en, no, auto). Default: en")
    p.add_argument("--gate-kind", choices=("tapscribe", "backend"), default="tapscribe")
    p.add_argument("--gate-min-speech-ms", type=int, default=None)
    p.add_argument(
        "--confidence-validation",
        dest="confidence_validation",
        action="store_true",
        default=None,
        help="Force WlK confidence-validation on (commits tokens fast; no in-flight buffer).",
    )
    p.add_argument(
        "--no-confidence-validation",
        dest="confidence_validation",
        action="store_false",
        help="Turn confidence-validation off (LocalAgreement; populates the in-flight buffer preview).",
    )
    p.add_argument(
        "--backend-policy",
        dest="backend_policy",
        choices=("simulstreaming", "localagreement"),
        default=None,
        help="WlK transcription policy. Default (None) = WlK's own default (simulstreaming: commits "
        "as it decodes, empty in-flight buffer). 'localagreement' holds tokens until they agree, "
        "populating buffer_transcription (the dashboard's in-flight preview).",
    )
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

    if args.concurrency:
        try:
            counts = [int(x) for x in args.concurrency.split(",") if x.strip()]
        except ValueError:
            p.error("--concurrency must be a comma list of integers, e.g. 1,2,4")
        if not counts or any(c < 1 for c in counts):
            p.error("--concurrency needs positive stream counts, e.g. 1,2,4")
        wav = args.wav or (DEFAULT_FIXTURE_DIR / "armstrong-en.wav")
        if not wav.exists():
            print(f"ERROR: {wav} not found", file=sys.stderr)
            sys.exit(1)
        cfg = build_base_config(args)
        print(
            f"backend: {'mlx-whisper' if use_mlx else 'faster-whisper'}  "
            f"concurrency {counts} stagger={args.stagger:g}s on {wav.stem}  "
            f"config: {_config_summary(cfg)}",
            flush=True,
        )
        rows = asyncio.run(
            run_concurrency_sweep(
                wav,
                cfg,
                counts=counts,
                stagger_s=args.stagger,
                use_mlx=use_mlx,
                speed=args.speed,
                ready_timeout=args.ready_timeout,
                verbose=True,
            )
        )
        if not rows:
            sys.exit(1)
        print()
        print_concurrency_table(rows, wav=wav, cfg=cfg, stagger_s=args.stagger)
        return

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
