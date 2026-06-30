"""Fetch-on-demand FLEURS clips for the multi-language benchmarks.

FLEURS (google/fleurs, CC-BY-4.0, ungated) gives short read-speech clips with
EXACT reference transcripts in the languages we care about (da_dk, nb_no, sv_se,
en_us) — far better ground truth than hand-matching spoken-Wikipedia articles.
We don't commit the audio (it would bloat the repo); instead each benchmark
fetches N clips per language into a local cache on first run.

Reads the parquet test split directly via pyarrow (no `datasets` runtime dep at
benchmark time beyond pyarrow + soundfile, both light). Clips are normalised to
the recorder wire format (16 kHz mono int16) and cached at
``~/.cache/tapscribe-fleurs/<lang>/<id>.wav`` with a sibling ``.txt`` reference.
"""

from __future__ import annotations

import io
import urllib.request
from pathlib import Path

CACHE = Path.home() / ".cache" / "tapscribe-fleurs"
# fixture-dir code -> (FLEURS config, language code the candidate set uses).
CONFIGS = {"da": ("da_dk", "da"), "nb": ("nb_no", "no"), "sv": ("sv_se", "sv"), "en": ("en_us", "en")}
_PARQUET = "https://huggingface.co/api/datasets/google/fleurs/parquet/{cfg}/test/0.parquet"


def fetch(code: str, *, n: int = 6, min_s: float = 8.0, max_s: float = 18.0) -> list[tuple[Path, str]]:
    """Return up to `n` (wav_path, reference) pairs for language `code`
    (da/nb/sv/en), downloading + caching them on first call. Idempotent: a
    cached clip is reused, so re-runs are instant and offline-friendly."""
    import pyarrow.parquet as pq
    import soundfile as sf

    from tapscribe.audio import open_recorder_wav

    cfg, _ = CONFIGS[code]
    out = CACHE / code
    out.mkdir(parents=True, exist_ok=True)
    have = sorted(out.glob("*.wav"))
    if len(have) >= n:
        return [(w, (w.with_suffix(".txt")).read_text(encoding="utf-8")) for w in have[:n]]

    parquet = CACHE / f"{cfg}.parquet"
    if not parquet.exists():
        urllib.request.urlretrieve(_PARQUET.format(cfg=cfg), parquet)  # noqa: S310 — fixed hf.co host
    # Keep the columns as Arrow and materialise each row's (large) audio blob
    # only when it passes the cheap duration filter and we still need clips —
    # `to_pylist()` on the whole audio column would pull the entire split's bytes
    # into RAM to extract a handful.
    table = pq.read_table(parquet, columns=["id", "audio", "raw_transcription", "num_samples"])
    ids, auds, refs, nsamps = (table[c] for c in ("id", "audio", "raw_transcription", "num_samples"))

    pairs: list[tuple[Path, str]] = []
    for i in range(len(ids)):
        if not (min_s <= nsamps[i].as_py() / 16000.0 <= max_s):
            continue
        arr, sr = sf.read(io.BytesIO(auds[i].as_py()["bytes"]), dtype="float32")
        if sr != 16000 or arr.ndim > 1:
            continue
        pcm = (arr.clip(-1.0, 1.0) * 32767.0).astype("<i2")
        ref = refs[i].as_py()
        stem = out / f"{code}-{ids[i].as_py()}"
        wav = stem.with_suffix(".wav")
        with open_recorder_wav(wav) as w:
            w.writeframes(pcm.tobytes())
        stem.with_suffix(".txt").write_text(ref, encoding="utf-8")
        pairs.append((wav, ref))
        if len(pairs) >= n:
            break
    return pairs
