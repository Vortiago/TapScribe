"""Per-backend model helpers — pure mappings shared by the Transcriber adapters.

The actual loading + caching now lives in `tapscribe.transcribers` via
the `load_transcriber` factory and per-adapter `.load(model_name)`
classmethods. This module is reduced to:

  - The MLX HuggingFace repo table + lookup
  - The NB-Whisper HF repo table + ct2/ download helper
  - `default_language_for(model_name)` — picks "en" / "no" / None from the name
  - `is_voxtral(model_name)` and `voxtral_repo(model_name)`

Everything here is import-light; the heavy backends are imported only
inside their adapter modules in `tapscribe.transcribers.*`.
"""

from __future__ import annotations

from pathlib import Path

# Mirrors whisperlivekit/model_mapping.py exactly — upstream is the source
# of truth since they've pressure-tested it. Note: `large-v3-turbo` is
# published WITHOUT the `-mlx` suffix; the naive `whisper-<name>-mlx`
# pattern would 404. Anything not in this table falls back to that pattern.
MLX_REPO_TABLE: dict[str, str] = {
    "tiny.en":         "mlx-community/whisper-tiny.en-mlx",
    "tiny":            "mlx-community/whisper-tiny-mlx",
    "base.en":         "mlx-community/whisper-base.en-mlx",
    "base":            "mlx-community/whisper-base-mlx",
    "small.en":        "mlx-community/whisper-small.en-mlx",
    "small":           "mlx-community/whisper-small-mlx",
    "medium.en":       "mlx-community/whisper-medium.en-mlx",
    "medium":          "mlx-community/whisper-medium-mlx",
    "large-v1":        "mlx-community/whisper-large-v1-mlx",
    "large-v2":        "mlx-community/whisper-large-v2-mlx",
    "large-v3":        "mlx-community/whisper-large-v3-mlx",
    "large-v3-turbo":  "mlx-community/whisper-large-v3-turbo",
    "large":           "mlx-community/whisper-large-mlx",
    # NB-Whisper (Norwegian-finetuned by Nasjonalbiblioteket) intentionally
    # excluded here: probed HF and there are NO public MLX conversions
    # (NbAiLabBeta/*-mlx, mlx-community/nb-whisper-*-mlx all 404). NB-Whisper
    # runs via faster-whisper on the CT2 weights bundled inside
    # NbAiLab/nb-whisper-<size>/ct2/, even on Apple Silicon.
}


# faster-whisper accepts a HuggingFace repo name as `model_size_or_path` and
# downloads/converts via huggingface_hub. NbAiLab publishes the canonical
# Norwegian-tuned Whisper checkpoints; for batch CPU transcription this is
# the path.
FW_HF_REPO_TABLE: dict[str, str] = {
    "nb-whisper-tiny":   "NbAiLab/nb-whisper-tiny",
    "nb-whisper-base":   "NbAiLab/nb-whisper-base",
    "nb-whisper-small":  "NbAiLab/nb-whisper-small",
    "nb-whisper-medium": "NbAiLab/nb-whisper-medium",
    "nb-whisper-large":  "NbAiLab/nb-whisper-large",
}


def mlx_whisper_repo(name: str) -> str:
    """Map an OpenAI-style Whisper model name to its mlx-community HF repo.
    Looks up the WhisperLiveKit-compatible table first; falls back to
    `mlx-community/whisper-<name>-mlx` for anything else."""
    return MLX_REPO_TABLE.get(name, f"mlx-community/whisper-{name}-mlx")


def default_language_for(model_name: str) -> str | None:
    """Pick a language hint from the model name.

    `.en` suffix → English-only Whisper checkpoint.
    `nb-*` → Norwegian-tuned (NB-Whisper).
    Everything else returns None so the model runs language detection.
    """
    n = (model_name or "").lower()
    if n.endswith(".en"):
        return "en"
    if n.startswith("nb-"):
        return "no"
    return None


def is_voxtral(model_name: str) -> bool:
    return model_name.lower().startswith("voxtral")


def voxtral_repo(_model_name: str) -> str:
    # Currently only Voxtral Mini is realistic for local CPU use. Other
    # Voxtral sizes can be added here if/when needed.
    return "mistralai/Voxtral-Mini-3B-2507"


def download_nb_whisper_ct2_dir(model_name: str) -> Path:
    """Fetch only the `ct2/` subdirectory of NbAiLab/nb-whisper-<size> via
    huggingface_hub and return its local path. faster-whisper (and
    WhisperLiveKit's auto-detect) can then load it directly because the
    relevant CT2 marker files (model.bin, config.json, vocabulary.json)
    live at the path root once we point inside that subdir.

    Cached by HF Hub — first call downloads a few hundred MB, subsequent
    calls return the same local snapshot path instantly.
    """
    from huggingface_hub import snapshot_download
    repo = FW_HF_REPO_TABLE.get(model_name, model_name)
    print(f"[tapscribe] fetching ct2/ subdir of {repo} via huggingface_hub…", flush=True)
    local = snapshot_download(repo_id=repo, allow_patterns=["ct2/*"])
    ct2 = Path(local) / "ct2"
    if not (ct2 / "model.bin").is_file():
        raise RuntimeError(
            f"NB-Whisper repo {repo} downloaded but ct2/model.bin is missing — "
            f"got files: {list(p.name for p in ct2.glob('*'))}"
        )
    return ct2


