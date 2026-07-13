"""RED contract for issue #259 — the Moonshine placeholders must not surface as
ready models.

The two Moonshine entries are "coming soon" placeholders: their loaders are
`NotImplementedError` stubs (real inference lands in #122/#123) and the live
channel (`build_live_cmd`) has no Moonshine backend, so an operator who picks
`moonshine-tiny` today gets an opaque whisperlivekit child-spawn crash. The
registry already has the mechanism for exactly this — `available=False` marks a
placeholder: `is_installed()` reports it as not-installed (so it drops out of
`only_installed` listings the dashboard serves) and `resolve()` refuses it early
with an actionable "not available yet" message. But the Moonshine entries ship
with the default `available=True`, gated only by install probes — and the ONNX
binding probes `optimum`, a generic HF companion package many transformers
installs carry — so on such a box they list as pickable and resolve straight
into the stub.

These tests pin the OPERATOR-observable harm at the aggregation layer (the
`only_installed` live listing `/api/models?context=live` serves, and the
`resolve()` refusal), NEVER the proximate `entry.available` field — so any fix
that gates the placeholders (mark `available=False`, the intended mechanism)
passes, while the current state and a degenerate "hide every live model" fix
both fail.

Hermetic (issue #232 lever): the install probes are forced BOTH ways via
`set_installed_modules_for_testing`, and the backends via
`set_available_backends_for_testing`, so `is_installed()` reduces to `available`
alone and the result does not depend on what this box happens to have importable
(e.g. whether `optimum` is present).
"""

from __future__ import annotations

import pytest

from tapscribe.runtime_probe import (
    set_available_backends_for_testing,
    set_installed_modules_for_testing,
)
from tapscribe.transcribers.catalog import REGISTRY, ResolvedBinding

_MOONSHINE_IDS = frozenset({"moonshine-tiny", "moonshine-base"})
# The Moonshine bindings' own probe modules (mlx: `moonshine`, onnx: `optimum`).
_MOONSHINE_PROBES = frozenset({"moonshine", "optimum"})
# A real, always-registered whisper model that supports the live context; its
# bindings probe these modules.
_REAL_LIVE_MODEL = "large-v3"
_WHISPER_PROBES = frozenset({"faster_whisper", "mlx_whisper"})


@pytest.fixture(autouse=True)
def _restore_probes():
    """Each test forces the available-backends set and the install-probe set;
    restore BOTH auto-probes afterwards so other test files see real detection
    and no fake install-state leaks."""
    yield
    set_available_backends_for_testing(None)
    set_installed_modules_for_testing(None)


def test_moonshine_placeholder_hidden_from_installed_live_and_refused():
    """Harm case: even on a box where the Moonshine probes ARE importable (so the
    old install-probe gate would pass them), the placeholders must NOT appear in
    the `only_installed` live listing the dashboard offers, and `resolve()` must
    refuse them with the clean "not available yet" message rather than returning
    a binding that dead-ends in the NotImplementedError stub.

    Asserted at the aggregation layer (the listing membership + the resolve
    refusal), not at `entry.available`, so the intended fix (mark the entries
    `available=False`) passes without the test knowing how it's implemented."""
    set_available_backends_for_testing(frozenset({"cpu", "cuda", "mlx"}))
    set_installed_modules_for_testing(_MOONSHINE_PROBES)  # probes importable

    live_installed = {e.model_id for e in REGISTRY.for_context("live", only_installed=True)}
    assert live_installed.isdisjoint(_MOONSHINE_IDS), (
        "a 'coming soon' Moonshine placeholder surfaced in the installed live "
        f"listing the dashboard offers (listed: {sorted(live_installed & _MOONSHINE_IDS)}) "
        "— an operator can pick a model whose loader is a NotImplementedError stub"
    )

    with pytest.raises(RuntimeError, match="not available"):
        REGISTRY.resolve("moonshine-tiny", preference="auto")


def test_real_live_model_still_installed_and_resolvable():
    """Guardrail case: a genuine available live model must STILL surface in the
    installed live listing and resolve to a binding. Without this pin a degenerate
    'fix' that hides every live model (or marks them all unavailable) would pass
    the harm case while breaking the picker entirely — this distinguishes gating
    the placeholders from disabling the live channel."""
    set_available_backends_for_testing(frozenset({"cpu", "cuda", "mlx"}))
    set_installed_modules_for_testing(_WHISPER_PROBES)  # real whisper probes importable

    live_installed = {e.model_id for e in REGISTRY.for_context("live", only_installed=True)}
    assert _REAL_LIVE_MODEL in live_installed, (
        f"a real available live model ({_REAL_LIVE_MODEL}) dropped out of the "
        "installed live listing — the fix disabled more than the placeholders"
    )

    rb = REGISTRY.resolve(_REAL_LIVE_MODEL, preference="auto")
    assert isinstance(rb, ResolvedBinding)
