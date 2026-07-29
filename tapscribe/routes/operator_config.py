"""The operator's persisted text config.

  PUT  /api/config/{key}  write one config text file (prompt, hotwords, …)
  GET  /api/languages     the candidate-language catalog plus the saved default

The two halves of the same concern: `_CONFIG_WRITERS` routes a URL segment to
the writer that owns that file, and `languages.txt` is the one key whose writer
validates against the catalog, which is also what the GET here serves (ADR-0010).
The structured summarizer default is NOT a `_CONFIG_WRITERS` entry (that map is
`{content: str}`-shaped); it lives in `routes/summarize.py`.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import partial

from fastapi import (
    APIRouter,
    HTTPException,
    Request,
)

from ..text import (
    CONFIG_KEYS,
    MAX_CONFIG_TEXT_LEN,
    read_languages,
    write_config,
    write_languages,
)
from ..transcribers.catalog import (
    REGISTRY,
    SPECIALIST_MODELS,
    candidate_language_codes,
    language_display_name,
)
from .body import json_body

router = APIRouter()


# Map of config key (URL segment) → writer. Keeps the PUT handler one
# branch deep. The plain text-file keys ride text.CONFIG_KEYS/write_config;
# languages.txt keeps its own richer writer (catalog-validated code set).
_CONFIG_WRITERS: dict[str, Callable[[str], None]] = {
    **{key: partial(write_config, key) for key in CONFIG_KEYS},
    "languages": write_languages,
}


@router.get("/api/languages")
async def api_languages():
    """The candidate-language catalog (ADR-0010) for the dashboard picker: the
    full allowlist of selectable languages with display names, plus the
    operator's current global default. Static apart from the default, so the
    dashboard fetches it once (like /api/models) rather than per poll.

    `specialists` maps a language code → the purpose-built model the cover adds
    for it (ADR-0010's specialist table, v1: `{"no": "nb-whisper-large"}`),
    registry-filtered like `cover_models`. The Transcript page reads it (with the
    generalist from `/api/state`'s `batch_model_effective`) to show which models a
    transcribe will actually run for the declared languages — so a Norwegian
    meeting's nb-whisper (faster-whisper) pass is visible up front
    rather than a surprise sidecar (ADR-0011).

    Response shape:
      {
        "languages":   [ {"code": "da", "name": "Danish"}, ... ],
        "default":     ["da", "no", "en"],
        "specialists": {"no": "nb-whisper-large"}
      }
    """
    return {
        "languages": [{"code": c, "name": language_display_name(c)} for c in candidate_language_codes()],
        "default": list(read_languages()),
        # Registry-filtered so the readout drops exactly what `cover_models` drops
        # (an env-overridden specialist absent from the catalog never runs), keeping
        # the client-side "models that will run" union provably equal to the cover.
        "specialists": {lang: m for lang, m in SPECIALIST_MODELS.items() if REGISTRY.get(m) is not None},
    }


@router.put("/api/config/{key}")
async def api_config_put(key: str, req: Request):
    writer = _CONFIG_WRITERS.get(key)
    if writer is None:
        raise HTTPException(404, f"unknown config key: {key!r}")
    body = await json_body(req)
    content = body.get("content")
    if not isinstance(content, str):
        raise HTTPException(400, "content must be a string")
    if len(content) > MAX_CONFIG_TEXT_LEN:
        raise HTTPException(
            400,
            f"content exceeds {MAX_CONFIG_TEXT_LEN}-char cap (got {len(content)})",
        )
    try:
        writer(content)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    except OSError as e:
        raise HTTPException(500, f"failed to write config: {e}") from None
    return {"ok": True, "key": key, "length": len(content)}
