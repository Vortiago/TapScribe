"""The model catalog and the in-process model cache.

  GET     /api/models        every registry entry for a context, with backends
  DELETE  /api/models/cache  evict every idle transcription model now

Both read the TranscriberRegistry rather than any per-session state: the catalog
answers "what can this box run", the cache lever answers "give the RAM back".
The live channel runs in its own subprocess and is unaffected by the eviction.
"""

from __future__ import annotations

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
)

from ..recorder import Recorder
from ..runtime_probe import available_backend_strs
from ..transcribers import evict_idle_now, run_on_model_thread
from ..transcribers.catalog import REGISTRY
from .deps import get_recorder

router = APIRouter()


@router.get("/api/models")
async def api_models(context: str = "batch"):
    """List every model the registry knows about, filtered by context.

    Drives the dashboard's batch + live model pickers (each calls with
    `?context=batch` and `?context=live` respectively). The response also
    includes the operator's available backends so the UI can gray out
    backend chips for kinds that aren't installed on this machine.

    Response shape:
      {
        "context": "batch" | "live",
        "available_backends": ["cpu", "cuda", ...],
        "models": [ {model_id, family, display_name, description,
                     languages, contexts, backends, inputs, available}, ... ]
      }
    """
    if context not in ("batch", "live"):
        raise HTTPException(400, f"context must be 'batch' or 'live' (got {context!r})")
    # `only_installed` filters out families whose adapter packages weren't
    # selected at install time (the picker in tapscribe/install_picker.py only
    # pulls in extras the operator ticks). Without this filter, the
    # dashboard would advertise Parakeet even on machines that skipped the
    # transformers install — and the operator would only find out by
    # clicking and hitting the lazy-import error.
    entries = REGISTRY.for_context(context, only_installed=True)  # type: ignore[arg-type]
    return {
        "context": context,
        "available_backends": sorted(available_backend_strs()),
        "models": [e.to_mapping() for e in entries],
    }


@router.delete("/api/models/cache")
async def api_models_cache_clear(recorder: Recorder = Depends(get_recorder)):  # noqa: ARG001
    """Evict every idle (not-in-use) transcription model from the in-process
    cache, freeing its weights + pooled GPU memory now.

    Batch models are unloaded automatically per the TAPSCRIBE_MODEL_IDLE_TTL_S
    policy (default: immediately after each job). This endpoint is the manual
    lever for operators who set a keep-warm TTL (or disabled eviction) and
    want to reclaim RAM/VRAM on demand. An in-flight transcribe keeps its
    model, so clicking this can't yank a model out from under a running job.
    The live channel runs in its own subprocess and is unaffected — stop it
    via /api/live/stop to reclaim that memory."""
    # On the dedicated model thread: eviction calls mlx.core.clear_cache(),
    # which (like every MLX op) must run on the thread that holds the Metal
    # stream — see run_on_model_thread.
    freed = await run_on_model_thread(evict_idle_now)
    print(f"[tapscribe] evicted {freed} idle transcription model(s) from cache", flush=True)
    return {"ok": True, "evicted": freed}
