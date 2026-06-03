"""Tests for the transcriber cache's memory lifecycle — acquire/release
refcounting and the `TAPSCRIBE_MODEL_IDLE_TTL_S` eviction policy.

These never touch real model weights: a duck-typed registry resolves every
model to a spy adapter that stands in for the multi-GB object on `_model`.
We assert eviction by observing that the heavy handle is nulled (or an
`unload()` hook fired) and that the cache no longer hands the instance back.
"""

from __future__ import annotations

import time

import pytest

from tapscribe import transcribers
from tapscribe.transcribers import ENV_IDLE_TTL_S


class _GenericSpy:
    """Adapter that keeps its weights on `_model` and defines NO unload hook
    — exercises the generic null-the-handle eviction path."""

    def __init__(self, model_name: str, kind: str):
        self.name = "spy"
        self.backend = f"spy-{kind}"
        self.device = kind.upper()
        self.model_name = model_name
        self._model: object | None = object()  # stands in for the GBs of weights

    def transcribe(self, path, **_kwargs):  # noqa: ARG002 — protocol parity, never called
        raise NotImplementedError


class _UnloadSpy(_GenericSpy):
    """Adapter that defines its own `unload()` — exercises the override path
    (mlx-whisper is the real instance of this, clearing an external cache)."""

    def __init__(self, model_name: str, kind: str):
        super().__init__(model_name, kind)
        self.unload_calls = 0

    def unload(self) -> None:
        self.unload_calls += 1
        self._model = None


class _Resolved:
    def __init__(self, kind: str, loader):
        self.kind = kind
        self.loader = loader


class _Registry:
    """Duck-typed stand-in for `TranscriberRegistry` that `load_transcriber`'s
    `registry=` arg accepts. Resolves every model to one fixed kind + loader."""

    def __init__(self, kind: str, loader):
        self._resolved = _Resolved(kind, loader)

    def resolve(self, model_name: str, *, preference: str):  # noqa: ARG002
        return self._resolved


def _loader_for(cls, created: list):
    """A loader thunk that builds a fresh spy per call and records it, so a
    test can tell a cache hit (no new instance) from a reload (new one)."""

    def _loader(model_name: str, kind: str):
        instance = cls(model_name, kind)
        created.append(instance)
        return instance

    return _loader


@pytest.fixture(autouse=True)
def _clean_cache_and_env(monkeypatch: pytest.MonkeyPatch):
    """Each test starts and ends with an empty cache and the knob unset
    (so an explicit `setenv` is the only thing steering policy)."""
    monkeypatch.delenv(ENV_IDLE_TTL_S, raising=False)
    transcribers.clear_cache()
    yield
    transcribers.clear_cache()


def test_default_evicts_model_after_release(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(ENV_IDLE_TTL_S, "0")
    created: list = []
    reg = _Registry("cpu", _loader_for(_GenericSpy, created))

    t = transcribers.load_transcriber("m", backend="cpu", registry=reg)
    assert t._model is not None

    transcribers.release_transcriber(t)
    # The heavy handle is dropped...
    assert t._model is None
    # ...and the cache no longer holds it: the next load builds anew.
    t2 = transcribers.load_transcriber("m", backend="cpu", registry=reg)
    assert t2 is not t
    assert len(created) == 2


def test_unload_hook_invoked_when_present(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(ENV_IDLE_TTL_S, "0")
    created: list = []
    reg = _Registry("cuda", _loader_for(_UnloadSpy, created))

    t = transcribers.load_transcriber("m", backend="cuda", registry=reg)
    transcribers.release_transcriber(t)

    assert t.unload_calls == 1
    assert t._model is None


def test_refcount_holds_model_until_last_release(monkeypatch: pytest.MonkeyPatch):
    """Two concurrent jobs share one cached model; the first release must NOT
    evict it out from under the second."""
    monkeypatch.setenv(ENV_IDLE_TTL_S, "0")
    created: list = []
    reg = _Registry("cpu", _loader_for(_GenericSpy, created))

    a = transcribers.load_transcriber("m", backend="cpu", registry=reg)
    b = transcribers.load_transcriber("m", backend="cpu", registry=reg)
    assert a is b  # cache hit
    assert len(created) == 1

    transcribers.release_transcriber(a)  # one user remains
    assert a._model is not None
    assert transcribers.load_transcriber("m", backend="cpu", registry=reg) is a  # still cached
    transcribers.release_transcriber(a)  # back to one user
    transcribers.release_transcriber(a)  # last release → evict
    assert a._model is None


def test_negative_ttl_never_evicts(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(ENV_IDLE_TTL_S, "-1")
    created: list = []
    reg = _Registry("cpu", _loader_for(_GenericSpy, created))

    t = transcribers.load_transcriber("m", backend="cpu", registry=reg)
    transcribers.release_transcriber(t)

    assert t._model is not None  # kept resident (legacy behaviour)
    assert transcribers.load_transcriber("m", backend="cpu", registry=reg) is t
    assert len(created) == 1


def test_positive_ttl_keeps_warm_then_idle_sweep_evicts(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(ENV_IDLE_TTL_S, "0.02")  # 20 ms idle window
    created_a: list = []
    reg_a = _Registry("cpu", _loader_for(_GenericSpy, created_a))

    a = transcribers.load_transcriber("a", backend="cpu", registry=reg_a)
    transcribers.release_transcriber(a)
    assert a._model is not None  # warm — not evicted on release

    time.sleep(0.05)  # let it age past the TTL

    # Loading any other key runs the idle sweep, which reaps the stale entry.
    created_b: list = []
    reg_b = _Registry("cuda", _loader_for(_GenericSpy, created_b))
    b = transcribers.load_transcriber("b", backend="cuda", registry=reg_b)

    assert a._model is None  # swept
    assert b._model is not None  # the freshly-loaded one is in use
    transcribers.release_transcriber(b)


def test_evict_idle_now_skips_inflight(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(ENV_IDLE_TTL_S, "-1")  # disable auto-evict so we set the scene
    created: list = []
    reg = _Registry("cpu", _loader_for(_GenericSpy, created))

    busy = transcribers.load_transcriber("busy", backend="cpu", registry=reg)  # never released → in use
    idle = transcribers.load_transcriber("idle", backend="cpu", registry=reg)
    transcribers.release_transcriber(idle)  # refcount 0, kept (ttl<0)

    freed = transcribers.evict_idle_now()

    assert freed == 1
    assert idle._model is None  # the idle one is reclaimed
    assert busy._model is not None  # the in-use one is left alone
    transcribers.release_transcriber(busy)


def test_clear_cache_force_evicts_in_flight(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(ENV_IDLE_TTL_S, "-1")
    created: list = []
    reg = _Registry("cpu", _loader_for(_GenericSpy, created))

    busy = transcribers.load_transcriber("busy", backend="cpu", registry=reg)  # in use
    transcribers.clear_cache()

    assert busy._model is None  # the nuclear path drops even in-flight models
    assert transcribers.load_transcriber("busy", backend="cpu", registry=reg) is not busy


def test_release_unknown_instance_is_noop():
    """A transcriber the cache never tracked (the canonical case: a test
    monkeypatched `load_transcriber` to a fake) must release cleanly without
    touching it — that's what keeps the batch layer's `finally` harmless."""

    class _Foreign:
        _model = object()

    obj = _Foreign()
    transcribers.release_transcriber(obj)  # must not raise
    assert obj._model is not None
