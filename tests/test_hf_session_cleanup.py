"""Regression guard: huggingface_hub's pooled HTTPS client must not outlive
the test that opened it.

`huggingface_hub` 1.x keeps ONE process-wide `httpx.Client` and never closes
it, so a real-model test (`test_tap_live_gate_real_audio.py` builds
`WhisperModel("tiny.en", ...)`, which resolves weights over HTTPS) left a
keep-alive TLS socket alive past its own teardown. The garbage collector
finalized it at an arbitrary later point, emitting `ResourceWarning: unclosed
socket`; under `filterwarnings = ["error"]` pytest turned that into an error
and attributed it to whichever test was running at that instant — an
intermittent red in an unrelated file that went green on re-run and under `-k`
isolation.

`conftest.close_hf_http_client_if_open` + its autouse fixture make that
teardown deterministic. These tests pin the helper's behaviour (the fixture's
own teardown cannot assert on its own effect) and pin that the fixture is
still autouse — dropping `autouse=True` would silently restore the leak while
every assertion below still passed.

Note what is deliberately NOT asserted: that some particular test downloads a
model. The invariant is "nothing leaves the Hub client open", whoever opens it.
"""

from __future__ import annotations

import sys

import pytest
from conftest import (  # type: ignore[import-not-found]
    _close_hf_http_client,
    close_hf_http_client_if_open,
)

pytest.importorskip("huggingface_hub", reason="huggingface_hub not installed")


def _http_module():
    from huggingface_hub.utils import _http

    return _http


def test_closing_is_a_noop_when_the_hub_never_built_a_client() -> None:
    """The ~1200 tests that never touch the Hub must pay nothing and must not
    trip over a missing module or a None client."""
    http = _http_module()
    close_hf_http_client_if_open()  # ensure the None state
    assert http._GLOBAL_CLIENT is None
    assert close_hf_http_client_if_open() is False


def test_closing_shuts_and_clears_the_pooled_client() -> None:
    """The actual fix: after the helper runs, the shared client is closed AND
    the module-global is cleared, so no pooled socket survives for the GC to
    finalize later."""
    http = _http_module()
    # `get_session()` builds the client without issuing a request — no network.
    client = http.get_session()
    assert http._GLOBAL_CLIENT is client
    assert client.is_closed is False

    assert close_hf_http_client_if_open() is True

    assert client.is_closed is True, "the pooled client was dropped but never closed"
    assert http._GLOBAL_CLIENT is None


def test_the_client_is_rebuilt_lazily_after_a_close() -> None:
    """Closing between tests is only safe because the library rebuilds on next
    use — otherwise this cleanup would break every later Hub call."""
    http = _http_module()
    http.get_session()
    close_hf_http_client_if_open()
    fresh = http.get_session()
    assert fresh.is_closed is False
    close_hf_http_client_if_open()


def test_the_cleanup_fixture_is_still_autouse() -> None:
    """The helper is only load-bearing because something calls it after EVERY
    test. Dropping `autouse=True` would restore the flake while leaving the
    behavioural assertions above green."""
    marker = _close_hf_http_client._fixture_function_marker
    assert marker.autouse is True, "the Hub-client cleanup fixture is no longer autouse"


def test_no_test_module_reintroduces_an_unclosed_hub_session() -> None:
    """`close_session` is the supported call; a hand-rolled
    `_GLOBAL_CLIENT = None` elsewhere would drop the client WITHOUT closing it
    and reintroduce exactly this bug."""
    offenders = [
        name
        for name, mod in list(sys.modules.items())
        if name.startswith("tests.") and getattr(mod, "_GLOBAL_CLIENT", None) is not None
    ]
    assert not offenders, f"test modules holding a Hub client global: {offenders}"
