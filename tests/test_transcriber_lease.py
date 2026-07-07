"""RED contract for #231 — a structural `lease_transcriber` async context
manager that owns load-on-model-thread + release-on-model-thread on EVERY exit
path, replacing the hand-rolled load + try/finally-release ritual the batch
orchestrators each re-derive.

The factory's `load_transcriber` / `release_transcriber` refcount pair
(`tapscribe/transcribers/__init__.py`) is a leak-by-omission contract: a
forgotten `finally` pins the model's in-flight refcount forever, keeping several
GB resident and blocking idle eviction for that key. `batch_transcribe` carries
that ritual twice by hand (`transcribe_one`, `transcribe_session_locked`), each
also remembering to offload the release via `run_on_model_thread`. This is the
identical shape `JobTracker.run` (`recorder.py`) already made structural for the
job slot ("claim/release is structural, not a try/finally discipline each
orchestrator re-derives").

`lease_transcriber` makes the model lease structural too:

    async with lease_transcriber(model, backend=...) as t:
        ...  # use t; released on normal exit AND when the block raises

`load_transcriber` / `release_transcriber` stay public for the rare hand-held
case (like `JobTracker.handle`).

Observation (mirrors test_transcribers_cache_eviction.py): with
TAPSCRIBE_MODEL_IDLE_TTL_S=0 the last release evicts the model immediately, so a
released lease leaves the spy's heavy handle nulled and its `unload()` fired —
a leaked lease leaves the handle resident and `unload_calls == 0`.
"""

from __future__ import annotations

import inspect
import threading
from pathlib import Path

import pytest

from tapscribe import batch_transcribe, transcribers
from tapscribe.transcribers import ENV_IDLE_TTL_S, MODEL_THREAD_PREFIX


class _Spy:
    """Stands in for a multi-GB loaded model. Records the thread its loader and
    its `unload()` ran on, so the contract can pin that both hit the model
    thread — as the hand-held sites do via `run_on_model_thread`."""

    def __init__(self, model_name: str, kind: str):
        self.name = "spy"
        self.backend = f"spy-{kind}"
        self.device = kind.upper()
        self.model_name = model_name
        self._model: object | None = object()  # stands in for the GBs of weights
        self.unload_calls = 0
        self.unload_thread: str | None = None
        self.load_thread: str | None = None

    def transcribe(self, path, **_kwargs):  # noqa: ARG002 — protocol parity, never called
        raise NotImplementedError

    def unload(self) -> None:
        self.unload_calls += 1
        self.unload_thread = threading.current_thread().name
        self._model = None


class _Resolved:
    def __init__(self, kind: str, loader):
        self.kind = kind
        self.loader = loader


class _Registry:
    """Duck-typed stand-in that `load_transcriber`'s `registry=` arg accepts;
    resolves every model to one fixed kind + loader."""

    def __init__(self, kind: str, loader):
        self._resolved = _Resolved(kind, loader)

    def resolve(self, model_name: str, *, preference: str):  # noqa: ARG002
        return self._resolved


def _reg(created: list) -> _Registry:
    def _loader(model_name: str, kind: str) -> _Spy:
        spy = _Spy(model_name, kind)
        spy.load_thread = threading.current_thread().name
        created.append(spy)
        return spy

    return _Registry("cpu", _loader)


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch):
    # TTL=0: the last release evicts immediately, so "released" is observable as
    # a nulled handle / fired unload().
    monkeypatch.setenv(ENV_IDLE_TTL_S, "0")
    transcribers.clear_cache()
    yield
    transcribers.clear_cache()


async def test_lease_yields_the_loaded_transcriber_and_releases_on_normal_exit():
    created: list = []
    reg = _reg(created)
    async with transcribers.lease_transcriber("m", backend="cpu", registry=reg) as t:
        assert t is created[0]  # entry loaded + yielded the transcriber
        assert t._model is not None  # held: refcount > 0, not yet evicted
    # normal exit released the lease; at TTL=0 that evicts → heavy handle dropped
    assert created[0]._model is None
    assert created[0].unload_calls == 1


async def test_lease_releases_when_the_body_raises():
    """The whole point of the structural lease: an exception inside the block
    must NOT leak the refcount — the model is released on the exception path."""
    created: list = []
    reg = _reg(created)
    with pytest.raises(ValueError, match="boom"):
        async with transcribers.lease_transcriber("m", backend="cpu", registry=reg):
            raise ValueError("boom")
    assert created[0]._model is None  # released despite the exception
    assert created[0].unload_calls == 1


async def test_lease_runs_load_and_release_on_the_model_thread():
    """Like the hand-held sites, both the load (entry) and the release/evict
    (exit) run on the single dedicated model thread — MLX's Metal stream must
    stay pinned across a job's load -> use -> release."""
    created: list = []
    reg = _reg(created)
    async with transcribers.lease_transcriber("m", backend="cpu", registry=reg):
        pass
    spy = created[0]
    assert spy.load_thread is not None and spy.load_thread.startswith(MODEL_THREAD_PREFIX)
    assert spy.unload_thread is not None and spy.unload_thread.startswith(MODEL_THREAD_PREFIX)


def test_both_batch_sites_go_through_the_lease_context_manager():
    """`transcribe_one` and `transcribe_session_locked` must acquire via
    `lease_transcriber`, not re-derive the load + finally-release ritual by hand
    (the leak-by-omission #231 removes). `release_transcriber` stays available
    for the hand-held case, but these two batch sites must not call it directly."""
    for fn in (batch_transcribe.transcribe_one, batch_transcribe.transcribe_session_locked):
        src = inspect.getsource(fn)
        assert "lease_transcriber" in src, f"{fn.__name__} must acquire via lease_transcriber"
        assert "release_transcriber" not in src, (
            f"{fn.__name__} must not hand-roll release_transcriber — the lease owns release"
        )


def test_batch_transcribe_no_longer_exposes_the_factory_bindings():
    """Root-cause invariant: #231 drops `load_transcriber`/`release_transcriber`
    from the `batch_transcribe` module namespace — they live in
    `tapscribe.transcribers`, where `lease_transcriber` resolves them at call
    time. Re-adding either (e.g. `from .transcribers import load_transcriber`)
    is what would let a stale `monkeypatch.setattr(batch_transcribe, ...)` /
    `setattr("tapscribe.batch_transcribe.load_transcriber", ...)` silently pass
    again while the structural lease contract is broken. Pin the absence so the
    regression fails here (form-independent) rather than as a scattered
    AttributeError per stale-patch test."""
    for name in ("load_transcriber", "release_transcriber"):
        assert not hasattr(batch_transcribe, name), (
            f"batch_transcribe must not re-export {name!r} — callers lease via "
            "lease_transcriber, which resolves it from tapscribe.transcribers"
        )


def test_no_test_targets_the_removed_batch_transcribe_binding():
    """Companion to the runtime invariant above: no test may name the removed
    string target `monkeypatch.setattr("tapscribe.batch_transcribe.load_transcriber", ...)`.
    `inspect.getsource` only sees the two batch call sites; it cannot see stale
    monkeypatch targets in sibling test files. This scanner reads the other
    files (skipping itself, which names the forbidden targets)."""
    self_path = Path(__file__).resolve()
    tests_dir = self_path.parent
    forbidden = (
        "tapscribe.batch_transcribe.load_transcriber",
        "tapscribe.batch_transcribe.release_transcriber",
    )
    offenders = []
    for path in sorted(tests_dir.rglob("test_*.py")):
        if path.resolve() == self_path:
            continue
        text = path.read_text(encoding="utf-8")
        offenders.extend(f"{path.name}: {needle}" for needle in forbidden if needle in text)
    assert not offenders, (
        "these tests patch a binding #231 removed from tapscribe.batch_transcribe "
        f"(retarget to tapscribe.transcribers): {offenders}"
    )
