"""Multi-person multilingual meeting, end-to-end through the REAL pipeline.

Simulates the realistic multi-person tray flow — the recorder's silence-split
yields ONE WAV per utterance — by laying interleaved single-language FLEURS
clips into a session as per-speaker utterances, then running the real
`transcribe_session_locked` (cover -> constrained-detect -> specialist-routing
selector -> merge) and summarising the merged transcript via the OpenAI-
compatible `ApiSummarizer` (point it at any Ollama/vLLM endpoint).

Reports per-utterance recall (each transcribed in its own language) and the
summary, so you can see whether a mixed da/no/en meeting comes out faithful.

  OLLAMA_URL=http://host:11434/v1  SUMMARY_MODEL=qwen3:4b-instruct-2507-q4_K_M \
    python -m benchmarks.multiperson_meeting

Needs faster-whisper + the FLEURS cache + a reachable summariser endpoint.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import wave
from pathlib import Path

from . import _fleurs, _metrics

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://100.108.163.4:11434/v1")
SUMMARY_MODEL = os.environ.get("SUMMARY_MODEL", "qwen3:4b-instruct-2507-q4_K_M")
GENERALIST = os.environ.get("GENERALIST", "large-v3-turbo")

# 3 speakers, 3 languages, interleaved — a real bilingual + English meeting.
MEETING = [("Lars", "da", 0), ("Ola", "nb", 0), ("Lars", "da", 1), ("Ola", "nb", 1), ("John", "en", 0)]


class _FakeJob:
    async def update(self, **_k):
        pass


async def _run() -> None:
    import tapscribe.config as C

    tmp = Path(tempfile.mkdtemp(prefix="tapscribe-mp-"))
    (tmp / "config").mkdir()
    (tmp / "recordings").mkdir()
    C.RECORDINGS_DIR = tmp / "recordings"
    C.CONFIG_DIR = tmp / "config"
    for name in dir(C):
        v = getattr(C, name)
        if name.endswith("_FILE") and isinstance(v, Path):
            setattr(C, name, tmp / "config" / v.name)

    from tapscribe.batch_transcribe import BatchSessionRequest, transcribe_session_locked
    from tapscribe.session_merge import select_session_wavs
    from tapscribe.summarizers.api import ApiSummarizer
    from tapscribe.text import write_languages
    from tapscribe.wav_cache import read_cached

    sd = C.RECORDINGS_DIR / "meeting"
    sd.mkdir(parents=True)
    refs: dict[str, tuple[str, str, str]] = {}
    for i, (spk, lang, k) in enumerate(MEETING):
        src, ref = _fleurs.fetch(lang, n=k + 1)[k]
        with wave.open(str(src), "rb") as w:
            pcm = w.readframes(w.getnframes())
        name = f"2026-01-01T10-{i:02d}-00Z__{spk}__{i}.wav"
        with wave.open(str(sd / name), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(pcm)
        refs[name] = (spk, lang, ref)
    write_languages("da, no, en")

    sel = select_session_wavs(sd, source="original")
    req = BatchSessionRequest(
        session="meeting",
        source="original",
        model=GENERALIST,
        backend="cpu",
        from_iso=None,
        to_iso=None,
        force=False,
        source_lang=None,
        target_lang=None,
    )
    print(f"running cover ({GENERALIST} + specialists) over {len(sel.wavs)} utterances…", flush=True)
    merged = await transcribe_session_locked(req, selection=sel, job=_FakeJob())

    print("\n=== per-utterance (each should be transcribed in its OWN language) ===", flush=True)
    for wav in sel.wavs:
        spk, lang, ref = refs[wav.name]
        c = read_cached(wav)
        print(
            f"  {spk:<6} {lang:<3} recall={_metrics.recall(ref, c.result.text):.2f}  "
            f"model={c.result.model:<16} :: {c.result.text[:55]}",
            flush=True,
        )
    summ = ApiSummarizer(
        base_url=OLLAMA_URL, model=SUMMARY_MODEL, api_key="benchmark", max_tokens=500, timeout_s=180.0
    ).summarize(
        merged["plain_text"],
        prompt="This meeting has Danish, Norwegian and English speakers. Summarise what each person discussed, in bullet points.",
    )
    print("\n=== SUMMARY (" + SUMMARY_MODEL + ") ===\n" + summ.summary.strip(), flush=True)
    shutil.rmtree(tmp, ignore_errors=True)


def main() -> None:
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    asyncio.run(_run())


if __name__ == "__main__":
    main()
