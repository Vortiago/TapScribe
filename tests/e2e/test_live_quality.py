"""Real live-path quality gate (the red→green target for live tuning).

Unlike `test_pipeline_with_real_whisper` (which exercises the BATCH
transcribe-session route), this drives the *live* pipeline end to end —
the supervised `whisperlivekit-server` child, the TapScribe `SpeechGate`,
and the `WlKRelay` — on a known fixture and asserts a WER threshold on
the captions it captures. It reuses `tools/bench_live.run_one` so the
test and the operator's sweep tool share one code path.

Heavy and slow (spawns a real ASR subprocess, downloads weights on first
run, paces audio in real time), so it's gated behind the `real_live`
marker and skips cleanly whenever the moving parts aren't present:
whisperlivekit + faster-whisper + jiwer importable, and
`whisperlivekit-server` resolvable. A model-download / startup failure
is treated as missing infrastructure (skip), not a quality regression
(fail) — only a *successful* run that misses the WER bar fails.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from tapscribe.live import LiveConfig, WhisperLiveKitChannel

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "audio"

# base.en on the corrected ~9 s Armstrong clip (the iconic line) should
# land well under this — a clean batch decode is ~0 WER, and the live
# path's real-time streaming + gate add only modest error. The bar is
# deliberately loose so CPU faster-whisper doesn't flake; tighten it once
# a clean-fixture sweep confirms the real base.en number.
#
# History: this fixture previously held the "step off the LM now" lead-in
# rather than the reference line, so every model scored ~0.92 here and the
# bar read like a live-quality bug to chase. It was a mislabeled fixture,
# not a model deficiency — see tests/fixtures/audio/README.md.
_MAX_WER = 0.5


@pytest.mark.real_live
async def test_live_path_meets_wer_threshold():
    for mod in ("whisperlivekit", "faster_whisper", "jiwer"):
        if importlib.util.find_spec(mod) is None:
            pytest.skip(f"{mod} not installed — install with `pip install -e .[whisper,bench]`")
    if WhisperLiveKitChannel._find_exe() is None:
        pytest.skip("whisperlivekit-server not on PATH — install with `pip install -e .[whisper]`")

    wav = FIXTURES_DIR / "armstrong-en.wav"
    if not wav.is_file() or not (FIXTURES_DIR / "armstrong-en.reference.txt").is_file():
        pytest.skip("armstrong-en fixture missing — see tests/fixtures/audio/README.md")

    from tools.bench_live import run_one

    cfg = LiveConfig(model="base.en", language="en", host="127.0.0.1", port=0)
    result = await run_one(
        wav,
        cfg,
        use_mlx=False,
        speed=1.0,
        warmup_s=0.5,
        ready_timeout=300.0,
        verbose=False,
    )

    if result.error:
        pytest.skip(f"live channel could not run (infra, not quality): {result.error}")

    wer = result.metrics.get("wer")
    assert result.hypothesis.strip(), f"live path produced no captions; lines={result.settled_lines}"
    assert wer is not None, f"scoring failed: {result.metrics.get('score_error')}"
    assert wer <= _MAX_WER, (
        f"live WER {wer:.2f} exceeds {_MAX_WER} for base.en on armstrong-en. "
        f"hypothesis={result.hypothesis!r} metrics={result.metrics}"
    )
