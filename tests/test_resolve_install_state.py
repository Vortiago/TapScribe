"""RED contract for issue #232 — `TranscriberRegistry.resolve()`'s `auto` walk
must consult binding install-state, not just machine-available BackendKind.

Bug: the `auto` branch (catalog.py) returns the first binding whose kind is
machine-available WITHOUT checking `binding.is_installed()`. So on a box where a
higher-priority kind is available (e.g. `mlx`) but that model's adapter package
for it is NOT importable, `auto` hands back an uninstalled binding — the failure
then surfaces as a bare `ImportError` at first transcribe (loaders defer the
import) instead of `auto` routing to the installed lower-priority binding the
same way it already silently skips machine-unavailable kinds (ADR-0001 §4).

Fix contract (pin every branch the change touches):
  * `auto` SKIPS a binding whose kind is available but whose adapter is not
    installed, and routes to a later installed binding for the same model.
  * `auto` RAISES (clean `RuntimeError` at resolve time, not a returned binding
    that ImportErrors later) when no installed × available binding exists.
  * the EXPLICIT-preference branch is UNCHANGED: an explicit pick of a kind
    whose adapter is not installed still RETURNS that binding (stays "loud" at
    load time via the deferred ImportError) — it must NOT be silently rerouted
    nor newly raise at resolve time. Guards against over-applying the fix.
  * the pre-existing machine-availability skip, ordering, and installed-binding
    routing still hold. Regression guards.

Deterministic + box-independent: every test forces both the machine-available
kinds (`set_available_backends_for_testing`) and the importable adapter modules
(`set_installed_modules_for_testing`), and builds fresh registries from
hand-made entries so the real environment's install state is irrelevant.
"""

from __future__ import annotations

import pytest

from tapscribe.runtime_probe import (
    set_available_backends_for_testing,
    set_installed_modules_for_testing,
)
from tapscribe.transcribers.catalog import (
    BackendBinding,
    ModelEntry,
    ResolvedBinding,
    TranscriberRegistry,
)


@pytest.fixture(autouse=True)
def _restore_overrides():
    """Force-and-restore BOTH override hooks so tests never leak the machine
    kinds or the fake install set into each other or into other files."""
    yield
    set_available_backends_for_testing(None)
    set_installed_modules_for_testing(None)


def _entry(model_id: str, *bindings: BackendBinding) -> ModelEntry:
    return ModelEntry(
        model_id=model_id,
        family="whisper",
        display_name=model_id,
        description="",
        languages=("en",),
        contexts=frozenset({"batch"}),
        backends=bindings,
    )


def _multi_binding(kinds: frozenset[str], probe_module: str):
    # One binding that serves several kinds behind a single probe module —
    # the real shape of the parakeet/whisper `transformers`/`faster_whisper`
    # (cuda+cpu) bindings.
    return BackendBinding(
        kinds=kinds,
        loader=lambda *_: None,  # type: ignore[arg-type]
        probe_module=probe_module,
    )


def _binding(kind: str, probe_module: str):
    # A no-op loader thunk; resolve() never calls it, only returns it.
    # The single-kind case of _multi_binding.
    return _multi_binding(frozenset({kind}), probe_module)


# ── auto skips an uninstalled higher-priority binding ────────────────────────


def test_auto_skips_uninstalled_mlx_binding_and_routes_to_installed_cpu():
    """The headline bug: mlx is machine-available and the model declares an mlx
    binding, but its adapter package isn't installed — while the cpu binding's
    adapter IS. `auto` must skip the uninstalled mlx binding and return the
    installed cpu one, not hand back an mlx binding that ImportErrors on load."""
    set_available_backends_for_testing(frozenset({"mlx", "cpu"}))
    set_installed_modules_for_testing(frozenset({"cpu_pkg"}))  # mlx_pkg NOT installed
    entry = _entry(
        "dual-model",
        _binding("mlx", "mlx_pkg"),
        _binding("cpu", "cpu_pkg"),
    )
    reg = TranscriberRegistry((entry,))

    rb = reg.resolve("dual-model", preference="auto")

    assert isinstance(rb, ResolvedBinding)
    assert rb.kind == "cpu"


def test_auto_raises_when_only_available_binding_is_not_installed():
    """No installed × machine-available binding exists — `auto` must raise a
    clean RuntimeError at resolve time, NOT return the uninstalled binding and
    let it fail as an opaque ImportError deep in the loader."""
    set_available_backends_for_testing(frozenset({"cpu"}))
    set_installed_modules_for_testing(frozenset())  # nothing importable
    entry = _entry("cpu-only", _binding("cpu", "cpu_pkg"))
    reg = TranscriberRegistry((entry,))

    with pytest.raises(RuntimeError):
        reg.resolve("cpu-only", preference="auto")


# ── the explicit-preference branch is untouched by install-state ─────────────


def test_explicit_preference_returns_uninstalled_binding_unchanged():
    """An EXPLICIT pick of a kind whose adapter isn't installed must still
    RETURN that binding — the install failure stays loud at load time via the
    deferred ImportError. The fix must not silently reroute an explicit pick,
    nor make resolve() newly raise for it. Guards against applying the
    install-state gate to the explicit branch too."""
    set_available_backends_for_testing(frozenset({"mlx", "cpu"}))
    set_installed_modules_for_testing(frozenset({"cpu_pkg"}))  # mlx_pkg NOT installed
    entry = _entry(
        "dual-model",
        _binding("mlx", "mlx_pkg"),
        _binding("cpu", "cpu_pkg"),
    )
    reg = TranscriberRegistry((entry,))

    rb = reg.resolve("dual-model", preference="mlx")

    assert rb.kind == "mlx"


# ── regression guards: existing semantics still hold ─────────────────────────


def test_auto_returns_preferred_kind_when_its_adapter_is_installed():
    """When the higher-priority binding's adapter IS installed, `auto` still
    prefers it — the fix must not over-skip installed bindings."""
    set_available_backends_for_testing(frozenset({"mlx", "cpu"}))
    set_installed_modules_for_testing(frozenset({"mlx_pkg", "cpu_pkg"}))
    entry = _entry(
        "dual-model",
        _binding("mlx", "mlx_pkg"),
        _binding("cpu", "cpu_pkg"),
    )
    reg = TranscriberRegistry((entry,))

    rb = reg.resolve("dual-model", preference="auto")

    assert rb.kind == "mlx"


def test_auto_still_skips_machine_unavailable_kind():
    """ADR-0001 §4 back-compat: a binding whose kind isn't on this machine is
    skipped even when its adapter is importable — machine-availability skip
    composes with (is not replaced by) the new install-state skip."""
    set_available_backends_for_testing(frozenset({"cpu"}))  # no mlx on this machine
    set_installed_modules_for_testing(frozenset({"mlx_pkg", "cpu_pkg"}))  # both installed
    entry = _entry(
        "dual-model",
        _binding("mlx", "mlx_pkg"),
        _binding("cpu", "cpu_pkg"),
    )
    reg = TranscriberRegistry((entry,))

    rb = reg.resolve("dual-model", preference="auto")

    assert rb.kind == "cpu"


# ── multi-kind binding: one probe gates every kind it serves ─────────────────
# The real parakeet/whisper cuda+cpu bindings are a SINGLE BackendBinding with
# kinds={cuda, cpu} behind one probe module, so the auto walk visits the same
# binding once per kind. The install-state skip must compose across ALL of a
# multi-kind binding's kinds, not match it on the lower kind after skipping the
# higher one.


def test_auto_skips_multi_kind_binding_for_every_kind_when_uninstalled():
    """A multi-kind (cuda+cpu) binding whose adapter is NOT installed must be
    skipped at BOTH the cuda and the cpu step of the auto walk — not matched on
    cpu after the cuda step skipped it — so with no other installed binding the
    resolve raises rather than handing back an uninstalled binding."""
    set_available_backends_for_testing(frozenset({"cuda", "cpu"}))
    set_installed_modules_for_testing(frozenset())  # transformers NOT importable
    entry = _entry("hf-only", _multi_binding(frozenset({"cuda", "cpu"}), "transformers"))
    reg = TranscriberRegistry((entry,))

    # The operator-facing error keeps the actionable pip-install hint.
    with pytest.raises(RuntimeError, match="pip install tapscribe"):
        reg.resolve("hf-only", preference="auto")


def test_auto_returns_installed_multi_kind_binding_on_its_highest_kind():
    """The composed skip must not OVER-skip: when the multi-kind binding's
    adapter IS installed it is returned, on the highest machine-available kind
    it serves (cuda before cpu)."""
    set_available_backends_for_testing(frozenset({"cuda", "cpu"}))
    set_installed_modules_for_testing(frozenset({"transformers"}))
    entry = _entry("hf-only", _multi_binding(frozenset({"cuda", "cpu"}), "transformers"))
    reg = TranscriberRegistry((entry,))

    rb = reg.resolve("hf-only", preference="auto")

    assert isinstance(rb, ResolvedBinding)
    assert rb.kind == "cuda"
