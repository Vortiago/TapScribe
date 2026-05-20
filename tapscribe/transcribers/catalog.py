"""TranscriberRegistry — the single declarative source of truth for every
model TapScribe knows about.

One entry per model. Each entry declares which families it belongs to,
which backends can run it (MLX / CUDA / CPU), which contexts it's valid
in (batch / live), the languages it supports, and which UI form inputs
the dashboard should render for it. The factory in
`tapscribe.transcribers.__init__.load_transcriber` consults the registry
to pick an adapter; `GET /api/models` serialises a context-filtered view
of the registry for the dashboard.

Adding a model means adding one `ModelEntry` here. Adding a *family* of
models means adding the family-level constants (input tuple, language
list) once and reusing them — see how Whisper, NB-Whisper, Voxtral,
Parakeet, and Canary each get one small block below.

Loader thunks are lazy: importing `catalog` never imports `faster_whisper`,
`parakeet_mlx`, `nemo_toolkit`, or any heavy adapter module. The thunk
imports its adapter only when the operator actually picks that backend.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from .base import (
    BackendKind,
    BackendPreference,
    ModelInput,
    SelectInput,
    TextInput,
    Transcriber,
)

# ---------------------------------------------------------------------------
# Backend resolution — turn "auto" into one of mlx / cuda / cpu, based on
# what's importable on this machine. Cached at module import so the dashboard
# poll doesn't probe torch.cuda every second.
# ---------------------------------------------------------------------------


def _detect_available_backends() -> frozenset[BackendKind]:
    """Probe the runtime for MLX / CUDA / CPU. CPU is always present.

    Each probe is a quick `find_spec` + (for CUDA) a torch.cuda check.
    Probes never throw — a missing import returns False, never propagates.
    """
    import importlib.util
    import platform

    available: set[BackendKind] = {"cpu"}

    if platform.system() == "Darwin" and platform.machine() == "arm64":
        if importlib.util.find_spec("mlx") is not None:
            available.add("mlx")

    # CUDA: torch must be importable AND report cuda.is_available(). We
    # use find_spec first to avoid importing torch (slow) when it's
    # missing entirely, then the actual import only when we know it's
    # present.
    if importlib.util.find_spec("torch") is not None:
        try:
            import torch  # type: ignore

            if torch.cuda.is_available():
                available.add("cuda")
        except Exception:  # noqa: BLE001 — torch import can fail in many ways on bad installs
            pass

    return frozenset(available)


_AVAILABLE_BACKENDS_CACHE: frozenset[BackendKind] | None = None


def available_backends() -> frozenset[BackendKind]:
    """Return the cached set of BackendKinds this machine can serve.

    First call probes; subsequent calls are O(1). Tests that want to
    force a specific set can call `set_available_backends_for_testing`."""
    global _AVAILABLE_BACKENDS_CACHE
    if _AVAILABLE_BACKENDS_CACHE is None:
        _AVAILABLE_BACKENDS_CACHE = _detect_available_backends()
    return _AVAILABLE_BACKENDS_CACHE


def set_available_backends_for_testing(kinds: frozenset[BackendKind] | None) -> None:
    """Override the detected-backends cache. `None` re-enables auto-probe."""
    global _AVAILABLE_BACKENDS_CACHE
    _AVAILABLE_BACKENDS_CACHE = kinds


# ---------------------------------------------------------------------------
# Adapter-module availability — does this install have the right Python
# packages to run a given binding? Drives the UI filter so the dashboard
# doesn't advertise Voxtral / Parakeet / Canary on installs where the
# operator told the install picker to skip them.
# ---------------------------------------------------------------------------


_INSTALLED_MODULES_OVERRIDE: frozenset[str] | None = None


def _is_module_available(name: str) -> bool:
    """True iff `name` is importable. The test override hook
    (`set_installed_modules_for_testing`) replaces the probe with a
    fixed set so tests can pretend e.g. `parakeet_mlx` is uninstalled
    without touching the real environment."""
    if _INSTALLED_MODULES_OVERRIDE is not None:
        return name in _INSTALLED_MODULES_OVERRIDE
    import importlib.util

    return importlib.util.find_spec(name) is not None


def set_installed_modules_for_testing(names: frozenset[str] | None) -> None:
    """Override what `_is_module_available` reports. `None` re-enables
    real probing. Use `frozenset()` to simulate "nothing installed"."""
    global _INSTALLED_MODULES_OVERRIDE
    _INSTALLED_MODULES_OVERRIDE = names


# `auto` resolves to the first kind in this list that's available. MLX first
# (cheapest, lowest-latency on Apple Silicon), then CUDA, then CPU.
_AUTO_RESOLUTION_ORDER: tuple[BackendKind, ...] = ("mlx", "cuda", "cpu")


def resolve_backend_preference(preference: BackendPreference) -> BackendKind:
    """Turn a `BackendPreference` (the operator's choice, possibly `auto`)
    into the concrete `BackendKind` that should be used.

    Raises `RuntimeError` when the operator picked a specific kind that's
    not available on this machine — clearer failure than silently
    falling back. `auto` falls through the resolution order until
    something works; CPU is always present so the loop always terminates.
    """
    avail = available_backends()
    if preference == "auto":
        for kind in _AUTO_RESOLUTION_ORDER:
            if kind in avail:
                return kind
        # avail always contains "cpu", so this is unreachable, but the
        # explicit fallback keeps the function total.
        return "cpu"
    if preference not in avail:
        raise RuntimeError(
            f"backend={preference!r} requested but not available on this machine. "
            f"Available: {sorted(avail)}. Install the matching extra "
            f"(pip install tapscribe[mlx|parakeet|canary]) or pick a different backend."
        )
    return preference


# ---------------------------------------------------------------------------
# Per-family UI input tuples — re-used across every variant in a family.
# ---------------------------------------------------------------------------


# Whisper-family (faster-whisper, mlx-whisper, nb-whisper): both prompt and
# hotwords. mlx-whisper folds hotwords into the prompt internally, but the
# UI affordance is identical.
WHISPER_INPUTS: tuple[ModelInput, ...] = (
    TextInput(
        name="initial_prompt",
        label="Initial prompt",
        kind="textarea",
        placeholder="meeting context — overrides prompt.txt for this job",
        description="Free-form context shown to Whisper before transcription. "
        "Improves transcription of proper nouns and jargon.",
    ),
    TextInput(
        name="hotwords",
        label="Hotwords",
        kind="text",
        placeholder="e.g. Acme Inc., Patricia Lin",
        description="Comma-separated names / jargon the model should bias toward. "
        "On mlx-whisper these get folded into the prompt.",
    ),
)


# Voxtral / Parakeet: neither accepts prompt or hotwords in the API. Empty
# input tuple means the dashboard renders zero extra fields.
NO_INPUTS: tuple[ModelInput, ...] = ()


# Canary's 25 supported languages — used for both source_lang and target_lang
# SelectInputs. English first so it's the natural default.
_CANARY_LANG_PAIRS: tuple[tuple[str, str], ...] = (
    ("en", "English"),
    ("bg", "Bulgarian"),
    ("hr", "Croatian"),
    ("cs", "Czech"),
    ("da", "Danish"),
    ("nl", "Dutch"),
    ("et", "Estonian"),
    ("fi", "Finnish"),
    ("fr", "French"),
    ("de", "German"),
    ("el", "Greek"),
    ("hu", "Hungarian"),
    ("it", "Italian"),
    ("lv", "Latvian"),
    ("lt", "Lithuanian"),
    ("mt", "Maltese"),
    ("pl", "Polish"),
    ("pt", "Portuguese"),
    ("ro", "Romanian"),
    ("ru", "Russian"),
    ("sk", "Slovak"),
    ("sl", "Slovenian"),
    ("es", "Spanish"),
    ("sv", "Swedish"),
    ("uk", "Ukrainian"),
)
_CANARY_LANG_CODES: tuple[str, ...] = tuple(code for code, _ in _CANARY_LANG_PAIRS)


CANARY_INPUTS: tuple[ModelInput, ...] = (
    SelectInput(
        name="source_lang",
        label="Source language",
        options=_CANARY_LANG_PAIRS,
        default="en",
        description="Language of the speech in the WAV. Required — Canary has no auto-detect.",
    ),
    SelectInput(
        name="target_lang",
        label="Target language",
        options=_CANARY_LANG_PAIRS,
        default="en",
        description="Output language. Equal to source = transcription; "
        "different = translation (only X↔English is supported by the model).",
    ),
)


# Parakeet's 25 languages — same set as Canary minus the source/target split,
# so this is the entry-level `languages` declaration only.
_PARAKEET_LANG_CODES: tuple[str, ...] = _CANARY_LANG_CODES


# ---------------------------------------------------------------------------
# Loader thunks — lazy imports so the heavy adapter modules don't load until
# the operator actually picks that backend.
# ---------------------------------------------------------------------------


def _load_faster_whisper(model_id: str, kind: BackendKind) -> Transcriber:
    from .faster_whisper import FasterWhisperTranscriber

    return FasterWhisperTranscriber.load(model_id, kind=kind)


def _load_mlx_whisper(model_id: str, kind: BackendKind) -> Transcriber:  # noqa: ARG001 — kind always "mlx" here
    from .mlx_whisper import MlxWhisperTranscriber

    return MlxWhisperTranscriber.load(model_id)


def _load_voxtral_hf(model_id: str, kind: BackendKind) -> Transcriber:
    from .voxtral import VoxtralTranscriber

    return VoxtralTranscriber.load(model_id, kind=kind)


def _load_voxtral_mlx(model_id: str, kind: BackendKind) -> Transcriber:  # noqa: ARG001
    from .mlx_voxtral import MlxVoxtralTranscriber

    return MlxVoxtralTranscriber.load(model_id)


def _load_parakeet_mlx(model_id: str, kind: BackendKind) -> Transcriber:  # noqa: ARG001
    from .mlx_parakeet import MlxParakeetTranscriber

    return MlxParakeetTranscriber.load(model_id)


def _load_parakeet_hf(model_id: str, kind: BackendKind) -> Transcriber:
    from .parakeet import ParakeetTranscriber

    return ParakeetTranscriber.load(model_id, kind=kind)


def _load_canary_mlx(model_id: str, kind: BackendKind) -> Transcriber:  # noqa: ARG001
    from .mlx_canary import MlxCanaryTranscriber

    return MlxCanaryTranscriber.load(model_id)


def _load_canary_nemo(model_id: str, kind: BackendKind) -> Transcriber:
    from .canary import CanaryTranscriber

    return CanaryTranscriber.load(model_id, kind=kind)


# ---------------------------------------------------------------------------
# Core data types
# ---------------------------------------------------------------------------


Family = Literal["whisper", "nb-whisper", "voxtral", "parakeet", "canary"]
Context = Literal["batch", "live"]


@dataclass(frozen=True)
class BackendBinding:
    """One adapter's claim over a set of `BackendKind`s for a model.

    `kinds` is the set of hardware/runtime kinds this binding's loader
    can serve (faster-whisper handles both CPU and CUDA in one binding;
    parakeet-mlx handles only MLX). The registry's `resolve()` walks an
    entry's `backends` tuple in order and picks the first binding whose
    `kinds` contains the resolved `BackendKind`.

    `probe_module` is the top-level package whose presence indicates this
    binding's adapter dependency is installed (e.g. `"faster_whisper"`,
    `"mlx_whisper"`, `"nemo"`). The registry uses `find_spec(probe_module)`
    to decide whether the binding is usable on this install — drives
    `ModelEntry.is_installed()` so `/api/models` can hide families the
    operator didn't pick during the install picker.
    """

    kinds: frozenset[BackendKind]
    loader: Callable[[str, BackendKind], Transcriber]
    # Empty string = no probe; the binding is treated as always-installed.
    # Used by tests that construct hypothetical bindings whose loader is
    # a no-op lambda; the real registry below sets this for every binding.
    probe_module: str = ""

    def is_installed(self) -> bool:
        """True iff `probe_module` is importable (or empty, meaning "no
        probe required" — see the field docstring). Cheap — uses
        `find_spec` so the heavy adapter module never actually loads."""
        if not self.probe_module:
            return True
        return _is_module_available(self.probe_module)


@dataclass(frozen=True)
class ResolvedBinding:
    """The output of `TranscriberRegistry.resolve()` — one ready-to-call
    loader pinned to a concrete `BackendKind`. Caller invokes
    `binding.loader(model_id, binding.kind)` to materialise the adapter."""

    kind: BackendKind
    loader: Callable[[str, BackendKind], Transcriber]


@dataclass(frozen=True)
class ModelEntry:
    """One row in the registry. Adding a model = adding one of these.

    Fields:
      model_id      — canonical short name used everywhere (API, JSON, UI)
      family        — groups variants for UI optgroup labelling
      display_name  — human-friendly label shown in the dropdown
      description   — short one-line explanation under the option
      languages     — ISO codes; ("auto",) for auto-detecting models
      contexts      — {"batch"}, {"live"}, or {"batch","live"}
      backends      — tuple of BackendBindings, walked in order on resolve
      inputs        — UI form fields the dashboard renders for this model
      available     — false marks "coming soon" placeholders; the UI shows
                      them grayed-out. The factory refuses to load them.
    """

    model_id: str
    family: Family
    display_name: str
    description: str
    languages: tuple[str, ...]
    contexts: frozenset[Context]
    backends: tuple[BackendBinding, ...]
    inputs: tuple[ModelInput, ...] = field(default_factory=tuple)
    available: bool = True

    def supports_context(self, context: Context) -> bool:
        return context in self.contexts

    def supported_backend_kinds(self) -> frozenset[BackendKind]:
        out: set[BackendKind] = set()
        for b in self.backends:
            out |= b.kinds
        return frozenset(out)

    def is_installed(self) -> bool:
        """True iff at least one of this entry's bindings has an importable
        adapter AND a kind this machine can serve. Drives the `/api/models`
        filter so families the operator left out of the install picker
        don't clutter the dropdowns. `available=False` placeholders
        ("coming soon") are always reported as not-installed."""
        if not self.available:
            return False
        avail_kinds = available_backends()
        for b in self.backends:
            if (b.kinds & avail_kinds) and b.is_installed():
                return True
        return False

    def to_mapping(self) -> dict:
        """JSON-friendly view used by `GET /api/models`."""
        return {
            "model_id": self.model_id,
            "family": self.family,
            "display_name": self.display_name,
            "description": self.description,
            "languages": list(self.languages),
            "contexts": sorted(self.contexts),
            "backends": sorted(self.supported_backend_kinds()),
            "inputs": [inp.to_mapping() for inp in self.inputs],
            "available": self.available,
        }


# ---------------------------------------------------------------------------
# TranscriberRegistry — the queryable, filterable collection of entries.
# ---------------------------------------------------------------------------


class TranscriberRegistry:
    """Holds every ModelEntry. Dispatch + filtering happens through here.

    Tests construct fresh registries with hand-picked entries to exercise
    the dispatch logic in isolation; production uses the module-level
    `REGISTRY` singleton built at the bottom of this file.
    """

    def __init__(self, entries: tuple[ModelEntry, ...]):
        self._entries = entries
        self._by_id: dict[str, ModelEntry] = {e.model_id: e for e in entries}
        if len(self._by_id) != len(entries):
            seen: dict[str, int] = {}
            for e in entries:
                seen[e.model_id] = seen.get(e.model_id, 0) + 1
            dups = [mid for mid, count in seen.items() if count > 1]
            raise ValueError(f"duplicate model_id(s) in registry: {dups!r}")

    def entries(self) -> tuple[ModelEntry, ...]:
        return self._entries

    def get(self, model_id: str) -> ModelEntry | None:
        return self._by_id.get(model_id)

    def require(self, model_id: str) -> ModelEntry:
        entry = self._by_id.get(model_id)
        if entry is None:
            raise KeyError(f"model_id={model_id!r} not in registry. Known: {sorted(self._by_id)!r}")
        return entry

    def for_context(self, context: Context, *, only_installed: bool = False) -> tuple[ModelEntry, ...]:
        """Entries valid for `context`. With `only_installed=True`, also
        drops entries whose adapter modules aren't importable on this
        install or whose backends aren't available on this machine — the
        filter `/api/models` applies so the dashboard reflects what the
        install picker actually pulled in."""
        out = tuple(e for e in self._entries if e.supports_context(context))
        if only_installed:
            out = tuple(e for e in out if e.is_installed())
        return out

    def resolve(self, model_id: str, preference: BackendPreference) -> ResolvedBinding:
        """Pick the loader for `model_id` given a backend preference.

        Resolution semantics:
          * preference == "auto": walk `_AUTO_RESOLUTION_ORDER` (mlx, cuda,
            cpu). For each candidate kind, return the first model binding
            that supports it AND is available on this machine. This is
            why NB-Whisper on an Apple Silicon Mac with no public MLX
            weights still routes cleanly to the CPU faster-whisper
            binding — "auto" skips over the unsupported kind silently
            (ADR-0001 §4 preserves this back-compat).
          * preference == explicit kind: the kind must be present on this
            machine AND the model must declare a binding for it. Either
            mismatch raises a `RuntimeError` with the supported list,
            so the dashboard / API caller hears about the mismatch
            directly instead of silently swapping the operator's pick.
        """
        entry = self.require(model_id)
        if not entry.available:
            raise RuntimeError(
                f"model {model_id!r} is registered but not available yet "
                f"(catalog entry marked available=False). Pick another model."
            )

        if preference == "auto":
            avail = available_backends()
            for kind in _AUTO_RESOLUTION_ORDER:
                if kind not in avail:
                    continue
                for binding in entry.backends:
                    if kind in binding.kinds:
                        return ResolvedBinding(kind=kind, loader=binding.loader)
            # No machine-available × model-supported combination exists.
            raise RuntimeError(
                f"model {model_id!r} has no backend that runs on this "
                f"machine. Model supports: {sorted(entry.supported_backend_kinds())!r}, "
                f"machine has: {sorted(avail)!r}. Install the matching "
                f"optional-dep group (pip install tapscribe[...])."
            )

        resolved_kind = resolve_backend_preference(preference)
        for binding in entry.backends:
            if resolved_kind in binding.kinds:
                return ResolvedBinding(kind=resolved_kind, loader=binding.loader)
        supported = sorted(entry.supported_backend_kinds())
        raise RuntimeError(
            f"model {model_id!r} doesn't support backend={resolved_kind!r}. "
            f"Supported backends for this model: {supported}. "
            f"Pick a different backend (UI: backend chip row)."
        )


# ---------------------------------------------------------------------------
# Default registry — every model TapScribe ships out of the box.
# ---------------------------------------------------------------------------


# Whisper-family bindings: faster-whisper handles cpu + cuda (we lean on the
# adapter to pick the right device + compute_type); mlx-whisper handles mlx.
_WHISPER_BACKENDS: tuple[BackendBinding, ...] = (
    BackendBinding(kinds=frozenset({"mlx"}), loader=_load_mlx_whisper, probe_module="mlx_whisper"),
    BackendBinding(
        kinds=frozenset({"cuda", "cpu"}),
        loader=_load_faster_whisper,
        probe_module="faster_whisper",
    ),
)


# NB-Whisper: no MLX weights exist publicly, so we drop the MLX binding and
# rely on faster-whisper for both CPU and CUDA.
_NB_WHISPER_BACKENDS: tuple[BackendBinding, ...] = (
    BackendBinding(
        kinds=frozenset({"cuda", "cpu"}),
        loader=_load_faster_whisper,
        probe_module="faster_whisper",
    ),
)


_VOXTRAL_BACKENDS: tuple[BackendBinding, ...] = (
    BackendBinding(kinds=frozenset({"mlx"}), loader=_load_voxtral_mlx, probe_module="mlx_voxtral"),
    BackendBinding(
        kinds=frozenset({"cuda", "cpu"}),
        loader=_load_voxtral_hf,
        probe_module="transformers",
    ),
)


_PARAKEET_BACKENDS: tuple[BackendBinding, ...] = (
    BackendBinding(kinds=frozenset({"mlx"}), loader=_load_parakeet_mlx, probe_module="parakeet_mlx"),
    BackendBinding(kinds=frozenset({"cuda", "cpu"}), loader=_load_parakeet_hf, probe_module="nemo"),
)


_CANARY_BACKENDS: tuple[BackendBinding, ...] = (
    BackendBinding(kinds=frozenset({"mlx"}), loader=_load_canary_mlx, probe_module="mlx_audio"),
    BackendBinding(kinds=frozenset({"cuda", "cpu"}), loader=_load_canary_nemo, probe_module="nemo"),
)


_BATCH_AND_LIVE = frozenset({"batch", "live"})
_BATCH_ONLY = frozenset({"batch"})


def _whisper(model_id: str, display: str, description: str, *, en_only: bool) -> ModelEntry:
    return ModelEntry(
        model_id=model_id,
        family="whisper",
        display_name=display,
        description=description,
        languages=("en",) if en_only else ("auto",),
        contexts=_BATCH_AND_LIVE,
        backends=_WHISPER_BACKENDS,
        inputs=WHISPER_INPUTS,
    )


def _nb(model_id: str, display: str, description: str) -> ModelEntry:
    return ModelEntry(
        model_id=model_id,
        family="nb-whisper",
        display_name=display,
        description=description,
        languages=("no",),
        contexts=_BATCH_AND_LIVE,
        backends=_NB_WHISPER_BACKENDS,
        inputs=WHISPER_INPUTS,
    )


_DEFAULT_ENTRIES: tuple[ModelEntry, ...] = (
    # ── Whisper variants (English-only suffixes first, then multilingual) ──
    _whisper("tiny.en", "tiny.en", "Whisper · English · fastest", en_only=True),
    _whisper("base.en", "base.en", "Whisper · English · fast", en_only=True),
    _whisper("small.en", "small.en", "Whisper · English · balanced", en_only=True),
    _whisper("medium.en", "medium.en", "Whisper · English · better", en_only=True),
    _whisper("tiny", "tiny", "Whisper · multilingual · fastest", en_only=False),
    _whisper("base", "base", "Whisper · multilingual · fast", en_only=False),
    _whisper("small", "small", "Whisper · multilingual · balanced", en_only=False),
    _whisper("medium", "medium", "Whisper · multilingual · better", en_only=False),
    _whisper("large-v3", "large-v3", "Whisper · multilingual · slow but accurate", en_only=False),
    _whisper(
        "large-v3-turbo",
        "large-v3-turbo",
        "Whisper · multilingual · turbo (faster than large-v3)",
        en_only=False,
    ),
    # ── NB-Whisper (Nasjonalbiblioteket — Norwegian) ──
    _nb("nb-whisper-tiny", "nb-whisper-tiny", "NB-AiLab · Norwegian-tuned · fastest"),
    _nb("nb-whisper-base", "nb-whisper-base", "NB-AiLab · Norwegian-tuned · fast"),
    _nb("nb-whisper-small", "nb-whisper-small", "NB-AiLab · Norwegian-tuned · balanced"),
    _nb("nb-whisper-medium", "nb-whisper-medium", "NB-AiLab · Norwegian-tuned · better"),
    _nb("nb-whisper-large", "nb-whisper-large", "NB-AiLab · Norwegian-tuned · slow"),
    # ── Voxtral (Mistral) — audio LLM, no prompt/hotwords ──
    ModelEntry(
        model_id="voxtral-mini",
        family="voxtral",
        display_name="voxtral-mini",
        description="Mistral Voxtral 3B · 8 langs (EN/ES/FR/PT/HI/DE/NL/IT) · no Norwegian",
        languages=("en", "es", "fr", "pt", "hi", "de", "nl", "it"),
        contexts=_BATCH_AND_LIVE,
        backends=_VOXTRAL_BACKENDS,
        inputs=NO_INPUTS,
    ),
    # ── Parakeet (NVIDIA via parakeet-mlx OR transformers) — batch-only ──
    ModelEntry(
        model_id="parakeet-tdt-0.6b-v3",
        family="parakeet",
        display_name="parakeet-tdt-0.6b-v3",
        description="NVIDIA Parakeet TDT 0.6B v3 · 25 EU langs · top of HF Open ASR · no Norwegian",
        languages=_PARAKEET_LANG_CODES,
        contexts=_BATCH_ONLY,
        backends=_PARAKEET_BACKENDS,
        inputs=NO_INPUTS,
    ),
    # ── Canary (NVIDIA via mlx-audio OR NeMo) — batch-only, supports translation ──
    ModelEntry(
        model_id="canary-1b-v2",
        family="canary",
        display_name="canary-1b-v2",
        description="NVIDIA Canary 1B v2 · 25 EU langs · transcription + X↔English translation",
        languages=_CANARY_LANG_CODES,
        contexts=_BATCH_ONLY,
        backends=_CANARY_BACKENDS,
        inputs=CANARY_INPUTS,
    ),
)


REGISTRY: TranscriberRegistry = TranscriberRegistry(_DEFAULT_ENTRIES)
