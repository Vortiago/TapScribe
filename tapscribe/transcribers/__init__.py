"""Transcribers — the stateful adapters that turn one WAV into text.

A `Transcriber` instance is one loaded model (faster-whisper / mlx-whisper /
Voxtral / Parakeet) holding its own model object, model name,
and device label. The factory `load_transcriber(name, *, backend)`
consults the `TranscriberRegistry` (see `tapscribe.transcribers.catalog`)
to pick the right adapter, then caches by `(model_name, resolved_kind)`.

The protocol-level contract is policy-free: callers resolve prompt /
hotwords / source/target language / hallucination rules and pass them
in. Post-processing (notably the hallucination filter) composes on top
of `transcribe()` via pure functions — see `tapscribe.hallucinations.apply`.

Memory lifecycle (`TAPSCRIBE_MODEL_IDLE_TTL_S`)
-----------------------------------------------
A loaded model is several GB resident. `load_transcriber` doubles as the
*acquire* half of a use-tracking pair: it bumps an in-flight refcount for
the `(model_name, kind)` key so a concurrent job's release can't evict a
model out from under another job still using it. Batch callers SHOULD use
`lease_transcriber()` (the `asynccontextmanager` that wraps acquire +
release) or pair `load_transcriber` with `release_transcriber(transcriber)`
in a `finally` (see `tapscribe.batch_transcribe`). The configured policy
decides what release does:

  * ``0`` (default) — unload immediately when the last in-flight job for a
    key finishes. Lowest idle footprint; the next job reloads from disk.
  * ``>0`` — keep the model warm and evict it lazily once it has been idle
    for at least that many seconds (swept on the next `load_transcriber`).
  * ``<0`` (e.g. ``-1``) — never auto-evict; the legacy "cache forever"
    behaviour. Use `clear_cache()` / the `DELETE /api/models/cache`
    endpoint to reclaim manually.

Eviction drops the cache entry, releases the adapter's heavy framework
handles, and best-effort-reclaims pooled GPU memory (`torch.cuda.empty_cache`
/ `mlx.core.clear_cache`). The env var is read per call so an operator can
retune it without a restart, mirroring how the config files are re-read
per job.
"""

from __future__ import annotations

import asyncio
import functools
import gc
import sys
import threading
import time
from collections.abc import AsyncIterator, Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from typing import Any, TypeVar

from .. import config
from .base import (
    BackendKind,
    BackendPreference,
    ModelInput,
    SelectInput,
    TextInput,
    Transcriber,
    TranscriptionResult,
    TranscriptionSegment,
    Word,
)
from .catalog import REGISTRY, TranscriberRegistry

__all__ = [
    "ENV_IDLE_TTL_S",
    "MODEL_THREAD_PREFIX",
    "BackendKind",
    "BackendPreference",
    "ModelInput",
    "REGISTRY",
    "SelectInput",
    "TextInput",
    "Transcriber",
    "TranscriberRegistry",
    "TranscriptionResult",
    "TranscriptionSegment",
    "Word",
    "clear_cache",
    "evict_idle_now",
    "lease_transcriber",
    "load_transcriber",
    "release_transcriber",
    "run_on_model_thread",
]


_T = TypeVar("_T")

# Every model op — load, transcribe, evict — runs on this ONE worker thread.
# MLX's Metal GPU stream is thread-local: a model whose weight arrays were
# created on one thread can't be `mx.eval`'d from another, or MLX raises
# "There is no Stream(gpu, 0) in current thread". `asyncio.to_thread` hands
# work to the shared default executor, so once the ~2 Hz /api/state poll (also
# offloaded there) or a multi-clip transcribe loop ran concurrently, a model's
# `generate` could land on a different worker than the one that built it and
# blow up. Pinning all model work to a single worker keeps load → generate →
# release on one Metal stream. It also serialises GPU work, which is what we
# want — one model runs at a time. (Non-MLX backends don't have the constraint
# but are equally happy on one thread.)
MODEL_THREAD_PREFIX = "tapscribe-model"
_MODEL_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix=MODEL_THREAD_PREFIX)


# The inline UP047 suppression keeps the 3.11-valid TypeVar generic: the
# PEP-695 `[T]` form ruff (target py312) prefers is a SyntaxError on 3.11, which
# the dev box + pre-push hook still run, and the codebase uses no PEP-695 syntax
# anywhere for that reason.
async def run_on_model_thread(func: Callable[..., _T], /, *args: Any, **kwargs: Any) -> _T:  # noqa: UP047
    """Run a blocking model op (load / transcribe / evict) on the single
    dedicated model thread. The `asyncio.to_thread` analogue, but pinned to one
    worker so MLX's thread-local Metal stream stays consistent across a job's
    load → transcribe → release sequence (see `MODEL_THREAD_PREFIX` above)."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_MODEL_EXECUTOR, functools.partial(func, *args, **kwargs))


# Operator knob name, hoisted to a module constant so the (eventual)
# dashboard wiring and any docs have one source of truth — same convention
# as the chunk-size knobs on the MLX adapters.
ENV_IDLE_TTL_S = "TAPSCRIBE_MODEL_IDLE_TTL_S"
_DEFAULT_IDLE_TTL_S = 0.0
# Bounds for the knob. Negative is the "never evict" sentinel (floored at
# -1); the upper bound is a day, far past any sane keep-warm window. Out-of-
# range values fall back to the default via `env_float`.
_IDLE_TTL_BOUNDS = (-1.0, 86_400.0)


_Key = tuple[str, BackendKind]

# Cache keyed by (model_name, resolved_kind). Multi-language sessions hold
# several entries — `nb-whisper-medium` on CPU for Norwegian, `parakeet-…`
# on CUDA for English — without double-loading shared models. The cache
# key uses the resolved kind (`mlx` / `cuda` / `cpu`) rather than the
# operator's preference, because two different preferences that resolve
# to the same kind should share one loaded model.
_cache: dict[_Key, Transcriber] = {}
# Per-key monotonic timestamp of the last acquire/release — drives the
# idle-TTL sweep. Last-write wins; only meaningful when TTL > 0.
_last_used: dict[_Key, float] = {}
# Per-key count of jobs currently using the model. A key is safe to evict
# only at refcount 0. Also reserved during a slow load (before the entry
# lands in `_cache`) so a concurrent release can't race the loader.
_inflight: dict[_Key, int] = {}
# One lock guards all three dicts. Held only for the fast bookkeeping; the
# slow model load runs OUTSIDE it (see load_transcriber) so concurrent
# acquires of different models don't serialise behind one fetch.
_lock = threading.Lock()


def _idle_ttl_s() -> float:
    """Current eviction policy in seconds (see module docstring)."""
    return config.env_float(
        ENV_IDLE_TTL_S,
        _DEFAULT_IDLE_TTL_S,
        min_value=_IDLE_TTL_BOUNDS[0],
        max_value=_IDLE_TTL_BOUNDS[1],
    )


def _free_framework_memory() -> None:
    """Best-effort reclamation of pooled accelerator memory after a model's
    Python objects were dropped. `gc.collect()` finalises the now-orphaned
    weights; the framework calls return buffers the allocator was pooling.

    Guarded twice over: only fires for frameworks actually imported (probed
    via `sys.modules`, never importing one just to clean up), and never lets
    a reclaim hiccup surface — the weights are already freed by gc, only the
    pooled VRAM/Metal buffers would linger to the next allocation.
    """
    gc.collect()

    torch = sys.modules.get("torch")
    if torch is not None:
        try:
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            # torch.cuda probing raises on driverless / partially-broken CUDA
            # builds. Reclaim is best-effort and the weights are already gone;
            # swallow so a finished transcribe isn't failed by cleanup.
            pass

    mx = sys.modules.get("mlx.core")
    if mx is not None:
        # `mx.clear_cache` (newer MLX) supersedes `mx.metal.clear_cache`
        # (older); try the current name first, fall back to the legacy one.
        # `getattr(None, ...)` is safe, so the chained fallback needs no guard.
        clear = getattr(mx, "clear_cache", None) or getattr(getattr(mx, "metal", None), "clear_cache", None)
        if callable(clear):
            try:
                clear()
            except Exception:
                # Same best-effort contract as the CUDA branch: the Metal
                # buffer pool is reclaimed on the next allocation if this
                # fails. Never surface to the operator.
                pass


def _release_model_object(transcriber: Transcriber) -> None:
    """Drop the heavy framework handles an adapter holds so gc can reclaim
    the weights now — even though the releasing caller may still reference
    the (now-gutted) adapter for the rest of its request. The entry has
    already been removed from `_cache`, so the gutted adapter is never reused.

    Most adapters keep their model on `_model` (Voxtral families also hold a
    `_processor`); mlx-whisper holds no object (its weights live in
    mlx_whisper's own module cache) and so defines an `unload()` to clear
    that. Prefer an adapter-defined `unload()` when present; otherwise null
    the known handles.
    """
    unload = getattr(transcriber, "unload", None)
    if callable(unload):
        try:
            unload()
        except Exception:
            # An adapter's unload hook is best-effort teardown; a failure must
            # not break the just-finished transcribe's response. The cache
            # entry is dropped regardless, so the adapter won't be reused —
            # worst case a framework-internal cache lingers until process exit.
            pass
        return
    for attr in ("_model", "_processor"):
        if getattr(transcriber, attr, None) is not None:
            setattr(transcriber, attr, None)


def _evict_keys(keys: list[_Key]) -> int:
    """Detach `keys` from every bookkeeping dict, release each adapter's
    model handles, then reclaim framework memory once. Caller holds `_lock`.
    Returns the number of entries actually freed."""
    freed = 0
    for key in keys:
        transcriber = _cache.pop(key, None)
        _last_used.pop(key, None)
        _inflight.pop(key, None)
        if transcriber is not None:
            _release_model_object(transcriber)
            freed += 1
    if freed:
        _free_framework_memory()
    return freed


def _sweep_idle_locked(ttl: float) -> None:
    """Evict every not-in-use key idle for at least `ttl` seconds. Caller
    holds `_lock`. Only called when `ttl > 0`."""
    now = time.monotonic()
    stale = [key for key, ts in list(_last_used.items()) if _inflight.get(key, 0) == 0 and (now - ts) >= ttl]
    if stale:
        _evict_keys(stale)


def _key_of(transcriber: Transcriber) -> _Key | None:
    """Find the cache key whose value is this exact instance, or None when
    the adapter isn't (or is no longer) cached — e.g. a test that injected a
    fake via `load_transcriber` monkeypatch, so the real cache never saw it.
    Caller holds `_lock`. The cache holds at most a handful of entries, so
    the linear scan is cheaper than maintaining a reverse map."""
    for key, cached in _cache.items():
        if cached is transcriber:
            return key
    return None


def _acquire_slot(key: _Key) -> None:
    """Mark one more in-flight use of `key`. Caller holds `_lock`."""
    _inflight[key] = _inflight.get(key, 0) + 1


def _release_slot(key: _Key) -> int:
    """Drop one in-flight use of `key`, removing the counter once it hits zero.
    Returns the remaining count. Caller holds `_lock`."""
    remaining = _inflight.get(key, 0) - 1
    if remaining > 0:
        _inflight[key] = remaining
    else:
        _inflight.pop(key, None)
    return remaining


def load_transcriber(
    model_name: str,
    *,
    backend: BackendPreference = "auto",
    registry: TranscriberRegistry | None = None,
) -> Transcriber:
    """Return a cached stateful `Transcriber` for `model_name` and mark it
    in-use.

    The registry decides which adapter handles each model on each backend
    (see `tapscribe.transcribers.catalog.REGISTRY` for the canonical table).
    `backend` is the operator's preference; the registry resolves it into one
    of `mlx` / `cuda` / `cpu` based on what's available on this machine and
    what the model supports.

    `registry` is injected only by tests; production passes None and gets the
    module-level singleton.

    Heavy adapter modules are imported lazily (via the registry's loader
    thunks) so booting TapScribe never pulls in PyTorch / MLX / transformers
    unless an operator actually picks that backend.

    This is the *acquire* half of the memory lifecycle: it increments the
    key's in-flight count. Long-running callers MUST balance it with
    `release_transcriber(transcriber)` (see the module docstring), or the
    model can never be evicted. The model load itself runs outside the lock
    so concurrent loads of different models don't serialise.
    """
    reg = registry or REGISTRY
    resolved = reg.resolve(model_name, preference=backend)
    key = (model_name, resolved.kind)

    with _lock:
        ttl = _idle_ttl_s()
        if ttl > 0:
            _sweep_idle_locked(ttl)
        cached = _cache.get(key)
        if cached is not None:
            _acquire_slot(key)
            _last_used[key] = time.monotonic()
            return cached
        # Miss: reserve the in-flight slot BEFORE dropping the lock to load,
        # so a concurrent release of this key (once we store it) can't evict
        # before our increment lands, and the idle sweep skips it.
        _acquire_slot(key)

    try:
        loaded = resolved.loader(model_name, resolved.kind)
    except BaseException:
        # Roll the reserved slot back so a failed load doesn't pin the key
        # forever (blocking future idle sweeps and leaking the refcount).
        with _lock:
            _release_slot(key)
        raise

    with _lock:
        existing = _cache.get(key)
        if existing is not None:
            # A concurrent acquire finished loading the same key while we
            # loaded too; adopt theirs and drop our duplicate so we don't keep
            # two copies of the weights resident. Both callers hold a slot.
            _release_model_object(loaded)
            _last_used[key] = time.monotonic()
            return existing
        _cache[key] = loaded
        _last_used[key] = time.monotonic()
        return loaded


def release_transcriber(transcriber: Transcriber) -> None:
    """Release one use of `transcriber` acquired via `load_transcriber`.

    When the last in-flight job for the key finishes, the configured
    `TAPSCRIBE_MODEL_IDLE_TTL_S` policy decides its fate: ``0`` unloads it
    now, ``>0`` leaves it warm for the idle sweep, ``<0`` keeps it forever.

    A no-op when the instance isn't in the cache — the canonical case is a
    test that monkeypatched `load_transcriber` to hand back a fake, so the
    real cache never tracked it. That keeps the batch layer's `finally`
    release harmless under those tests.
    """
    with _lock:
        key = _key_of(transcriber)
        if key is None:
            return
        if _release_slot(key) > 0:
            _last_used[key] = time.monotonic()
            return
        if _idle_ttl_s() == 0:
            _evict_keys([key])
        else:
            # >0 keep warm for the idle sweep; <0 keep indefinitely. Either
            # way refresh last_used so a >0 TTL measures idle-since-now.
            _last_used[key] = time.monotonic()


@asynccontextmanager
async def lease_transcriber(
    model_name: str,
    *,
    backend: BackendPreference = "auto",
    registry: TranscriberRegistry | None = None,
) -> AsyncIterator[Transcriber]:
    """Load a transcriber on the model thread, yield it, and release on
    every exit path (normal exit AND exceptions).

    Replacement for the hand-rolled `load_transcriber` + `try` +
    `finally: release_transcriber` pattern in the batch orchestrators.
    Keeps the model lease structural — no forgotten `finally` possible.
    """
    transcriber = await run_on_model_thread(load_transcriber, model_name, backend=backend, registry=registry)
    try:
        yield transcriber
    finally:
        await run_on_model_thread(release_transcriber, transcriber)


def evict_idle_now() -> int:
    """Evict every cached model that isn't currently in use, freeing its
    weights and pooled GPU memory. Returns the count freed.

    Backs the manual `DELETE /api/models/cache` endpoint: an in-flight
    transcribe keeps its model (refcount > 0) so an operator can't yank a
    model out from under a running job. The nuclear `clear_cache()` is for
    tests and hard resets."""
    with _lock:
        idle = [key for key in _cache if _inflight.get(key, 0) == 0]
        return _evict_keys(idle)


def clear_cache() -> None:
    """Drop and unload ALL cached transcribers — including in-flight ones —
    freeing their model weights and any pooled GPU memory. Mostly for tests
    and hard resets; prefer `evict_idle_now()` when a concurrent job might be
    mid-transcribe. Also useful when an operator flips the backend preference
    at runtime and wants every old instance evicted."""
    with _lock:
        _evict_keys(list(_cache.keys()))
        # Belt-and-suspenders: the dicts should already be empty after the
        # sweep, but reset them so a stray reserved-but-unloaded slot (a load
        # in flight on another thread) doesn't survive a hard reset.
        _cache.clear()
        _last_used.clear()
        _inflight.clear()
