"""Summarization: run one, list the models, persist the default.

  POST  /api/sessions/{session}/summarize  summarize a session's merged transcript
  GET   /api/summarize/models              this machine's selectable local models
  GET   /api/summarize/config              the global default, redacted
  PUT   /api/summarize/config              persist the global default

The catalog served by GET /api/summarize/models is ALSO the allowlist a picked
model is validated against (an untrusted body string must never reach a model
loader or a Hub download), so the dropdown can only offer loadable choices. The
config PUT is a dedicated route rather than a `_CONFIG_WRITERS` entry because
that map is `{content: str}`-shaped and this is one structured object; all its
validation lives in `write_summarizer_config`.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Request,
)

from ..batch_summarize import (
    SummarizeSessionRequest,
    effective_summarizer_config,
    summarize_session,
)
from ..recorder import Recorder
from ..summarizers import summary_model_catalog
from ..summarizers.catalog import MAX_TOKENS_BOUNDS
from ..text import (
    read_summarizer_config,
    summarizer_default_public,
    write_summarizer_config,
)
from .body import (
    json_body,
    parse_bounded_int,
    parse_opt_str,
    parse_opt_str_keep_empty,
    require_json_object_body,
    require_opt_str,
)
from .deps import get_recorder

router = APIRouter()


@router.post("/api/sessions/{session}/summarize")
async def api_session_summarize(
    session: str,
    req: Request,
    recorder: Recorder = Depends(get_recorder),
):
    """Summarize a session's merged transcript. Thin HTTP shim over
    `batch_summarize.summarize_session` — parse the body; the registered
    domain-error handlers map failures to status codes. The Local (bundled,
    offline — #86) and Command (#82) sources are wired, while the API source
    (#85) still maps to a clear 400.

    Body fields the caller omits resolve through the saved config (#84):
    session-meta override → global default → built-ins, via
    `effective_summarizer_config`. An explicit body field wins over both
    saved layers, so a Generate with hand-edited values behaves exactly as
    before. The effective model/source were allowlist-validated at write
    time AND are re-validated inside `load_summarizer` (double guard)."""
    body = await json_body(req)
    overrides: dict[str, Any] = await asyncio.to_thread(effective_summarizer_config, session)
    # Boundary validation through the parse_* family: a non-string value
    # 400s instead of being silently ignored. source/model/api_key treat
    # blank as "not supplied" (fall through to the saved config);
    # command/prompt/base_url keep the empty string — an explicit clear of
    # the saved value is meaningful for them. prompt and api_key values are
    # passed VERBATIM (see their call sites below); the rest are stripped.
    source = parse_opt_str(body.get("source"), "source")
    if source is not None:
        overrides["source"] = source
    command = parse_opt_str_keep_empty(body.get("command"), "command")
    if command is not None:
        overrides["command"] = command
    model = parse_opt_str(body.get("model"), "model")
    if model is not None:
        overrides["model"] = model
    # max_tokens: parse + bounds-check exactly like the other numeric body knobs
    # (gate / strip-silence) — a clear 400 for out-of-range, None when omitted.
    # The adapter also clamps as a final safety net for non-route callers.
    max_tokens = parse_bounded_int(
        body.get("max_tokens"), "max_tokens", lo=MAX_TOKENS_BOUNDS[0], hi=MAX_TOKENS_BOUNDS[1]
    )
    if max_tokens is not None:
        overrides["max_tokens"] = max_tokens
    # prompt is deliberately VERBATIM (no strip): leading/trailing whitespace
    # in an operator-authored prompt template is meaningful, and "" is an
    # explicit clear. Only the type boundary applies.
    prompt = require_opt_str(body.get("prompt"), "prompt")
    if prompt is not None:
        overrides["prompt"] = prompt
    base_url = parse_opt_str_keep_empty(body.get("base_url"), "base_url")
    if base_url is not None:
        overrides["base_url"] = base_url
    # api_key: blank means "not supplied" (fall through to saved config) but
    # the ACCEPTED value is passed verbatim — keys are opaque tokens and a
    # strip could corrupt one that legitimately contains edge whitespace.
    api_key = require_opt_str(body.get("api_key"), "api_key")
    if api_key is not None and api_key.strip():
        overrides["api_key"] = api_key
    return await summarize_session(recorder, SummarizeSessionRequest(session=session, **overrides))


@router.get("/api/summarize/models")
async def api_summarize_models():
    """List the local summarizer's selectable models for THIS machine's backend.

    Drives the Summary view's model dropdown. The backend is hardware-routed
    (MLX on Apple Silicon, GGUF/CPU elsewhere — the same probe the summarizer
    uses), so a Mac sees the MLX catalog and a Linux/CUDA box sees the GGUF one.
    The catalog is also the allowlist the local source validates a picked model
    against, so the dropdown can only ever offer loadable choices.

    Response: `{ "backend", "default", "models": [{repo_id, label, approx_gb,
    context_tokens, note, is_default}, ...] }`."""
    return summary_model_catalog()


@router.get("/api/summarize/config")
async def api_summarize_config_get():
    """The structured global summarizer default (#84) as the REDACTED public
    projection. The api_key is write-only and never returned; `key_set`
    reflects whether one is stored. See `summarizer_default_public`."""
    return summarizer_default_public(read_summarizer_config())


@router.put("/api/summarize/config")
async def api_summarize_config_put(req: Request):
    """Persist the global summarizer default. Full-object semantics (a
    missing key clears that field). Dedicated endpoint rather than a
    `_CONFIG_WRITERS` entry — that map is `{content: str}`-shaped, this is
    one structured object. ALL validation (source/model allowlists, text
    caps, max_tokens int + bounds) lives in `write_summarizer_config`; its
    ValueError is the 400."""
    # Strict parse rather than `json_body` (which turns ANY parse failure into
    # {}): combined with the full-object semantics above, a dropped or
    # truncated body would WIPE the operator's saved summarizer default —
    # taking the end-of-meeting pipeline's summarize stage with it — and answer
    # {"ok": true}. Only a deliberate `{}` clears.
    body = await require_json_object_body(req, allow_empty=False)
    try:
        stored = write_summarizer_config(body)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    except OSError as e:
        raise HTTPException(500, f"failed to write config: {e}") from None
    return {"ok": True, "config": stored}
