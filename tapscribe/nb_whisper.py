"""NB-Whisper weight download + CT2 patching.

NB-Whisper (Norwegian-finetuned Whisper from Nasjonalbiblioteket) ships
pre-converted CTranslate2 weights inside `NbAiLab/nb-whisper-<size>/ct2/`.
Two paths in TapScribe need a local handle to that directory:

  - `transcribers.faster_whisper.FasterWhisperTranscriber.load()` for
    batch transcription (loads via WhisperModel).
  - `live.LiveChannel.start()` for the supervised whisperlivekit-server
    child (passes the path via `--model-path`).

NB-Whisper has no public MLX weights — the family always runs via
faster-whisper / CT2, even on Apple Silicon.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

# faster-whisper accepts a HuggingFace repo name as `model_size_or_path` and
# downloads/converts via huggingface_hub. NB-Whisper repos are resolved from
# the registry at call time.


def _resolve_nb_whisper_repo(model_name: str) -> str:
    """Resolve an NB-Whisper model_id to its HF repo via the registry."""
    from .transcribers.catalog import REGISTRY

    entry = REGISTRY.get(model_name)
    if entry is not None:
        repo = entry.repos.get("nb-whisper")
        if repo:
            return repo
    return model_name


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

    repo = _resolve_nb_whisper_repo(model_name)
    print(f"[tapscribe] fetching ct2/ subdir of {repo} via huggingface_hub…", flush=True)
    # Also pull the root-level preprocessor_config.json — faster-whisper reads
    # `feature_size` (n_mels) from it. NB-Whisper-large is based on Whisper
    # large-v3 (128 mels); without this file faster-whisper falls back to 80
    # mels and the encoder rejects the features with shape (1, 80, 3000).
    local = snapshot_download(
        repo_id=repo,
        allow_patterns=["ct2/*", "preprocessor_config.json"],
    )
    ct2 = Path(local) / "ct2"
    if not (ct2 / "model.bin").is_file():
        raise RuntimeError(
            f"NB-Whisper repo {repo} downloaded but ct2/model.bin is missing — "
            f"got files: {list(p.name for p in ct2.glob('*'))}"
        )
    ensure_nb_whisper_lang_ids(ct2)
    ensure_nb_whisper_preprocessor(ct2, Path(local))
    return ct2


# CTranslate2 reports `is_multilingual=False` when the model's config.json has
# no `lang_ids` array. faster-whisper then silently rewrites our `language="no"`
# hint to `"en"` (see SYSTRAN/faster-whisper transcribe.py: "current model is
# English-only…; using 'en' instead"). NB-Whisper's published ct2/ weights ship
# with empty/missing lang_ids despite being a multilingual finetune, so Norwegian
# audio comes back as broken English. We patch lang_ids in on download by mining
# the `<|xx|>` language tokens out of the bundled tokenizer.json.
_NON_LANG_SPECIAL_TOKENS = frozenset(
    {
        "<|endoftext|>",
        "<|startoftranscript|>",
        "<|translate|>",
        "<|transcribe|>",
        "<|startoflm|>",
        "<|startofprev|>",
        "<|nocaptions|>",
        "<|notimestamps|>",
        "<|nospeech|>",
    }
)


def ensure_nb_whisper_lang_ids(ct2_dir: Path) -> bool:
    """Inject Whisper language token IDs into ct2/config.json if missing.

    Returns True if the file was rewritten, False if it already had lang_ids
    (or could not be parsed). Idempotent on re-runs.
    """
    config_path = ct2_dir / "config.json"
    tokenizer_path = ct2_dir / "tokenizer.json"
    if not config_path.is_file() or not tokenizer_path.is_file():
        return False
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    existing = config.get("lang_ids")
    if isinstance(existing, list) and len(existing) > 1:
        return False
    lang_ids = _extract_whisper_lang_ids(tokenizer_path)
    if not lang_ids:
        return False
    config["lang_ids"] = lang_ids
    config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    print(
        f"[tapscribe] patched {config_path} with {len(lang_ids)} lang_ids — "
        "NB-Whisper now reports as multilingual to faster-whisper.",
        flush=True,
    )
    return True


def ensure_nb_whisper_preprocessor(ct2_dir: Path, snapshot_root: Path) -> bool:
    """Make sure `ct2_dir/preprocessor_config.json` exists so faster-whisper
    picks up the right `feature_size` (mel-bin count).

    NB-Whisper publishes `preprocessor_config.json` at the repo root, but
    faster-whisper only looks for it next to `model.bin`. We copy it in.
    Returns True if a copy was made, False if the target already existed or
    no source file was found. Idempotent on re-runs.
    """
    target = ct2_dir / "preprocessor_config.json"
    if target.is_file():
        return False
    for candidate in (ct2_dir / "preprocessor_config.json", snapshot_root / "preprocessor_config.json"):
        if candidate.is_file() and candidate.resolve() != target.resolve():
            shutil.copyfile(candidate, target)
            print(
                f"[tapscribe] copied {candidate.name} into {ct2_dir} — "
                "faster-whisper will now use the model's true mel-bin count.",
                flush=True,
            )
            return True
    return False


def _extract_whisper_lang_ids(tokenizer_path: Path) -> list[int]:
    """Scan a HuggingFace tokenizer.json for `<|xx|>` language tokens.

    Whisper's tokenizer uses two-letter `<|en|>`, `<|no|>`, … markers in its
    added_tokens table. We exclude the non-language `<|...|>` tokens (eos,
    transcribe, etc.) and return the remaining IDs sorted.
    """
    try:
        data = json.loads(tokenizer_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    added = data.get("added_tokens") or []
    ids: list[int] = []
    for entry in added:
        content = entry.get("content")
        tid = entry.get("id")
        if not isinstance(content, str) or not isinstance(tid, int):
            continue
        if not (content.startswith("<|") and content.endswith("|>")):
            continue
        if content in _NON_LANG_SPECIAL_TOKENS:
            continue
        inner = content[2:-2]
        # Whisper language tokens are 2-letter codes (e.g. "no", "nb", "en");
        # skip timestamp tokens like "<|0.00|>" and any other special markers.
        if not (inner.isalpha() and 2 <= len(inner) <= 3):
            continue
        ids.append(tid)
    return sorted(set(ids))
