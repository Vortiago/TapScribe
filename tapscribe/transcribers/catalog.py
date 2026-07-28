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
and Parakeet each get one small block below.

Loader thunks are lazy: importing `catalog` never imports `faster_whisper`,
`parakeet_mlx`, `transformers`, or any heavy adapter module. The thunk
imports its adapter only when the operator actually picks that backend.

Hardware/runtime backend detection (MLX / CUDA / CPU probing, adapter-module
`find_spec` checks, and their caches/test-overrides) lives in
`tapscribe.runtime_probe` — a leaf module with consumers outside this
registry (`summarizers.catalog`, `live.py`, `setup_state.py`). This module
imports the probe primitives it needs to resolve a model's backend; import
`tapscribe.runtime_probe` directly for anything that doesn't also need the
model registry. See CONTEXT.md's `BackendKind / BackendPreference /
available_backends` entry.
"""

from __future__ import annotations

import functools
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Literal

from ..runtime_probe import (
    AUTO_RESOLUTION_ORDER,
    available_backends,
    is_module_available,
    resolve_backend_preference,
)
from .base import (
    BackendKind,
    BackendPreference,
    ModelInput,
    TextInput,
    Transcriber,
    default_language_for,
)

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


# Parakeet's 25 supported languages (parakeet-tdt-0.6b-v3) — English first so
# it's the natural default. No Norwegian. Declared as the entry-level
# `languages` set; Parakeet has no source/target split (it doesn't translate).
_PARAKEET_LANG_PAIRS: tuple[tuple[str, str], ...] = (
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
_PARAKEET_LANG_CODES: tuple[str, ...] = tuple(code for code, _ in _PARAKEET_LANG_PAIRS)


# ── Candidate languages (ADR-0010) ──────────────────────────────────────────
# The operator declares a *candidate-language set* per meeting; the catalog's
# language vocabulary is the allowlist that set validates against. The bundled
# default is the catch-all {da, no, en} so a fresh install handles the
# motivating mixed Danish/Norwegian/English meeting with zero configuration.
DEFAULT_CANDIDATE_LANGUAGES: tuple[str, ...] = ("da", "no", "en")


# ── Specialist table (ADR-0010 slice 2) ─────────────────────────────────────
# A `language → purpose-built model` map for languages where a specialist beats
# the generalist. v1 has one entry: Norwegian routes to NB-Whisper (Whisper
# finetuned on Norwegian by Nasjonalbiblioteket), which disambiguates the
# confusable da/no pair the generalist flips on. `cover_models` unions the
# generalist with the specialists for a meeting's declared languages; the
# selector (`tapscribe.language_select`) then picks the best transcript per
# region. This map IS the seam — repoint Norwegian at a different checkpoint, or
# add a row for another language, with no pipeline change. `nb-whisper-large` is
# the default because a 20-clip FLEURS benchmark (vs a `large-v3-turbo`
# generalist) showed it win-or-tie 19/20 on Norwegian (+0.07 word-recall, ~40%
# lower WER), whereas `nb-whisper-medium` only TIED the generalist — i.e. medium
# didn't earn the extra decode, large does. It is operator-tunable in spirit (a
# later issue surfaces it), so keep it the single source of truth.
def specialist_table_with_env_overrides(base: dict[str, str], environ: Mapping[str, str]) -> dict[str, str]:
    """A copy of `base` with `TAPSCRIBE_SPECIALIST_<LANG>=<model id>` overrides
    applied — the "operator-tunable" seam the comment above promises (e.g. a fast
    nb-whisper-tiny in a bridge E2E, or a future better Norwegian model, with no code
    change). Only EXISTING rows are repointed; adding a language is ADR-0010
    territory, not a knob. Env is operator-controlled (not request input), and the
    chosen model is still registry-validated by `cover_models` before it loads.

    Read once at import into `SPECIALIST_MODELS` — a launch-time knob, unlike the
    use-time TAPSCRIBE_SUMMARIZE_* overrides. So an in-process test must
    `monkeypatch.setitem(SPECIALIST_MODELS, ...)`, NOT `setenv`, to take effect;
    `setenv` only lands in a fresh process (the bridge E2Es' recorder subprocess)."""
    table = dict(base)
    for lang in base:
        if override := environ.get(f"TAPSCRIBE_SPECIALIST_{lang.upper()}", "").strip():
            table[lang] = override
    return table


SPECIALIST_MODELS: dict[str, str] = specialist_table_with_env_overrides(
    {"no": "nb-whisper-large"}, os.environ
)

# Display names for every concrete language code that appears across the
# catalog. The Parakeet pairs cover most; nb-whisper contributes Norwegian and
# Voxtral contributes Hindi. Single source of truth for the picker's labels.
_LANGUAGE_NAMES: dict[str, str] = {
    **dict(_PARAKEET_LANG_PAIRS),
    "no": "Norwegian",
    "hi": "Hindi",
}


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


# ── Moonshine (live-only — see PRD #120) ─────────────────────────────────────
# Moonshine is deliberately NOT a batch Transcriber (see PRD #120 "Out of
# Scope") — its value is low-latency LIVE captioning via
# `tapscribe.moonshine_live.MoonshineLiveChannel`, which builds its own
# engine directly (`transcribers.moonshine_mlx` / `transcribers.moonshine_onnx`)
# rather than going through this registry's Transcriber-shaped loaders. These
# two bindings exist only so `resolve()` / `is_installed()` (backend
# availability, `/api/models?context=live` surfacing) work uniformly across
# every family; a stray `/api/transcribe` request naming a Moonshine model
# hits one of these and gets a clear, permanent refusal rather than a batch
# adapter that doesn't exist.


def _load_moonshine_mlx(model_id: str, kind: BackendKind) -> Transcriber:  # noqa: ARG001
    raise NotImplementedError(
        "Moonshine has no batch Transcriber adapter — it's a live-only engine "
        "(see tapscribe.moonshine_live.MoonshineLiveChannel and PRD #120)."
    )


def _load_moonshine_onnx(model_id: str, kind: BackendKind) -> Transcriber:  # noqa: ARG001
    raise NotImplementedError(
        "Moonshine has no batch Transcriber adapter — it's a live-only engine "
        "(see tapscribe.moonshine_live.MoonshineLiveChannel and PRD #120)."
    )


# ---------------------------------------------------------------------------
# Core data types
# ---------------------------------------------------------------------------


Family = Literal["whisper", "nb-whisper", "voxtral", "parakeet", "moonshine"]
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
    `"mlx_whisper"`, `"transformers"`). The registry uses `find_spec(probe_module)`
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
        return is_module_available(self.probe_module)


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
    repos: dict[str, str] = field(default_factory=dict, compare=False)

    def supports_context(self, context: Context) -> bool:
        return context in self.contexts

    def supported_backend_kinds(self) -> frozenset[BackendKind]:
        out: set[BackendKind] = set()
        for b in self.backends:
            out |= b.kinds
        return frozenset(out)

    def fixed_language(self) -> str | None:
        """The one concrete language this model is pinned to (#206: the
        registry row, not the model's name, is the source), or None when
        the entry is multilingual or auto-detecting — a multi-language
        model must never be handed a spurious fixed language. Pure
        function of the frozen row, no module state: a test swapping the
        module-level `REGISTRY` can never change an entry's answer."""
        if len(self.languages) == 1 and self.languages[0] != "auto":
            return self.languages[0]
        return None

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

    def fixed_language_for(self, model_name: str) -> str | None:
        """This registry's declared fixed language for `model_name`.

        An entry that declares exactly one concrete language returns it —
        including `available=False` placeholders (a "coming soon" model's
        metadata is still authoritative), which is why this consults the
        unfiltered entries, never the installed-only view. A multilingual /
        auto entry returns None. Names with no entry fall back to the
        catalog-free name heuristic (`base.default_language_for`) so ad-hoc
        checkpoints (e.g. `nb-*` finetunes not in the catalog) keep their
        hint.
        """
        entry = self.get(model_name)
        if entry is not None:
            return entry.fixed_language()
        return default_language_for(model_name)

    def resolve(self, model_id: str, preference: BackendPreference) -> ResolvedBinding:
        """Pick the loader for `model_id` given a backend preference.

        Resolution semantics:
          * preference == "auto": walk `AUTO_RESOLUTION_ORDER` (mlx, cuda,
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
            for kind in AUTO_RESOLUTION_ORDER:
                if kind not in avail:
                    continue
                for binding in entry.backends:
                    if kind in binding.kinds and binding.is_installed():
                        return ResolvedBinding(kind=kind, loader=binding.loader)
            # No installed × machine-available combination exists.
            raise RuntimeError(
                f"model {model_id!r} has no backend that runs on this "
                f"machine. Model supports: {sorted(entry.supported_backend_kinds())!r}, "
                f"machine has: {sorted(avail)!r}. Install the matching "
                f"optional-dep group (pip install tapscribe[...])."
            )

        resolved_kind = resolve_backend_preference(preference)
        for binding in entry.backends:
            if resolved_kind in binding.kinds:
                # Deliberately NOT install-gated (unlike the auto walk):
                # an explicit pick of an uninstalled adapter resolves and
                # fails loudly at load time with the adapter's own
                # actionable error — see
                # test_explicit_preference_returns_uninstalled_binding_unchanged.
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
        # `mistral_common` rather than `transformers`: `transformers` is
        # heavy enough that other extras (and indirect deps) sometimes
        # pull it in transitively, which would falsely advertise Voxtral
        # on installs that didn't ask for it. `mistral_common` is only
        # listed in the `voxtral` extra and is required for the Voxtral
        # processor's apply_transcription_request path, so its presence
        # is a reliable signal that the operator opted into Voxtral.
        probe_module="mistral_common",
    ),
)


_PARAKEET_BACKENDS: tuple[BackendBinding, ...] = (
    BackendBinding(kinds=frozenset({"mlx"}), loader=_load_parakeet_mlx, probe_module="parakeet_mlx"),
    BackendBinding(
        kinds=frozenset({"cuda", "cpu"}),
        loader=_load_parakeet_hf,
        # `librosa` rather than `transformers`, for the reason spelled out on
        # the Voxtral binding above: `transformers` is pulled in by OTHER
        # extras (voxtral-cpu declares it, and plenty of indirect deps carry
        # it), so probing it advertised Parakeet as INSTALLED on a
        # Voxtral-only venv — /api/models and /setup said "ready", the
        # explicit-preference branch of `resolve()` handed back this binding,
        # and `from transformers import AutoModelForTDT` only blew up at the
        # first transcribe. `librosa` backs the Parakeet feature extractor's
        # log-mel and is declared by `parakeet-cpu` ALONE, so its presence is
        # a reliable signal that the operator opted into Parakeet.
        probe_module="librosa",
    ),
)


_MOONSHINE_BACKENDS: tuple[BackendBinding, ...] = (
    # `mlx_audio`: the same MLX-audio library this repo already uses for
    # Canary — its `mlx_audio.stt.models.moonshine` port is what
    # `transcribers.moonshine_mlx` imports lazily.
    BackendBinding(kinds=frozenset({"mlx"}), loader=_load_moonshine_mlx, probe_module="mlx_audio"),
    # `moonshine_onnx`: the actual top-level module `useful-moonshine-onnx`
    # installs (NOT `optimum`, a generic HF companion package many
    # transformers installs carry for unrelated reasons — probing it would
    # falsely advertise Moonshine as ready on installs that never asked for
    # it, the exact trap the Voxtral binding's probe-selection rationale
    # above documents avoiding).
    BackendBinding(
        kinds=frozenset({"cuda", "cpu"}),
        loader=_load_moonshine_onnx,
        probe_module="moonshine_onnx",
    ),
)


def _moonshine(model_id: str, display: str, description: str) -> ModelEntry:
    return ModelEntry(
        model_id=model_id,
        family="moonshine",
        display_name=display,
        description=description,
        languages=("en",),
        contexts=_LIVE_ONLY,
        backends=_MOONSHINE_BACKENDS,
        inputs=NO_INPUTS,
        # Real inference lands via MoonshineLiveChannel (PRD #120) — no
        # longer a "coming soon" placeholder. `is_installed()` now gates
        # purely on the probe modules above, same as every other family.
        # Registry-carried upstream names per #206: the HF repo mlx-audio's
        # `load()` resolves, and the bare model name `MoonshineOnnxModel`
        # wants (the ONNX weights live under onnx/merged/<name>/ in the
        # upstream repo — not an HF repo id).
        repos={
            "moonshine-mlx": f"UsefulSensors/{model_id}",
            "moonshine-onnx": model_id.removeprefix("moonshine-"),
        },
    )


_BATCH_AND_LIVE = frozenset({"batch", "live"})
_BATCH_ONLY = frozenset({"batch"})
_LIVE_ONLY = frozenset({"live"})


def _whisper(
    model_id: str, display: str, description: str, *, en_only: bool, repos: dict[str, str] | None = None
) -> ModelEntry:
    return ModelEntry(
        model_id=model_id,
        family="whisper",
        display_name=display,
        description=description,
        languages=("en",) if en_only else ("auto",),
        contexts=_BATCH_AND_LIVE,
        backends=_WHISPER_BACKENDS,
        inputs=WHISPER_INPUTS,
        repos=repos or {},
    )


def _nb(model_id: str, display: str, description: str, *, repos: dict[str, str] | None = None) -> ModelEntry:
    return ModelEntry(
        model_id=model_id,
        family="nb-whisper",
        display_name=display,
        description=description,
        languages=("no",),
        contexts=_BATCH_AND_LIVE,
        backends=_NB_WHISPER_BACKENDS,
        inputs=WHISPER_INPUTS,
        repos=repos or {},
    )


_DEFAULT_ENTRIES: tuple[ModelEntry, ...] = (
    # ── Whisper variants (English-only suffixes first, then multilingual) ──
    _whisper(
        "tiny.en",
        "tiny.en",
        "Whisper · English · fastest",
        en_only=True,
        repos={"mlx-whisper": "mlx-community/whisper-tiny.en-mlx"},
    ),
    _whisper(
        "base.en",
        "base.en",
        "Whisper · English · fast",
        en_only=True,
        repos={"mlx-whisper": "mlx-community/whisper-base.en-mlx"},
    ),
    _whisper(
        "small.en",
        "small.en",
        "Whisper · English · balanced",
        en_only=True,
        repos={"mlx-whisper": "mlx-community/whisper-small.en-mlx"},
    ),
    _whisper(
        "medium.en",
        "medium.en",
        "Whisper · English · better",
        en_only=True,
        repos={"mlx-whisper": "mlx-community/whisper-medium.en-mlx"},
    ),
    _whisper(
        "tiny",
        "tiny",
        "Whisper · multilingual · fastest",
        en_only=False,
        repos={"mlx-whisper": "mlx-community/whisper-tiny-mlx"},
    ),
    _whisper(
        "base",
        "base",
        "Whisper · multilingual · fast",
        en_only=False,
        repos={"mlx-whisper": "mlx-community/whisper-base-mlx"},
    ),
    _whisper(
        "small",
        "small",
        "Whisper · multilingual · balanced",
        en_only=False,
        repos={"mlx-whisper": "mlx-community/whisper-small-mlx"},
    ),
    _whisper(
        "medium",
        "medium",
        "Whisper · multilingual · better",
        en_only=False,
        repos={"mlx-whisper": "mlx-community/whisper-medium-mlx"},
    ),
    _whisper(
        "large-v3",
        "large-v3",
        "Whisper · multilingual · slow but accurate",
        en_only=False,
        repos={"mlx-whisper": "mlx-community/whisper-large-v3-mlx"},
    ),
    _whisper(
        "large-v3-turbo",
        "large-v3-turbo",
        "Whisper · multilingual · turbo (faster than large-v3)",
        en_only=False,
        # NO `-mlx` suffix — mlx-community publishes turbo without it, so the
        # construct-by-convention `whisper-<name>-mlx` pattern would 404 here.
        repos={"mlx-whisper": "mlx-community/whisper-large-v3-turbo"},
    ),
    # ── NB-Whisper (Nasjonalbiblioteket — Norwegian) ──
    # Deliberately NO `mlx-whisper` repo on any NB entry: there are no public
    # MLX conversions (NbAiLabBeta/*-mlx and mlx-community/nb-whisper-*-mlx
    # all 404 when probed). NB-Whisper runs via faster-whisper on the CT2
    # weights inside NbAiLab/nb-whisper-<size>/, even on Apple Silicon —
    # adding an mlx repo here would 404 at load time.
    _nb(
        "nb-whisper-tiny",
        "nb-whisper-tiny",
        "NB-AiLab · Norwegian-tuned · fastest",
        repos={"nb-whisper": "NbAiLab/nb-whisper-tiny"},
    ),
    _nb(
        "nb-whisper-base",
        "nb-whisper-base",
        "NB-AiLab · Norwegian-tuned · fast",
        repos={"nb-whisper": "NbAiLab/nb-whisper-base"},
    ),
    _nb(
        "nb-whisper-small",
        "nb-whisper-small",
        "NB-AiLab · Norwegian-tuned · balanced",
        repos={"nb-whisper": "NbAiLab/nb-whisper-small"},
    ),
    _nb(
        "nb-whisper-medium",
        "nb-whisper-medium",
        "NB-AiLab · Norwegian-tuned · better",
        repos={"nb-whisper": "NbAiLab/nb-whisper-medium"},
    ),
    _nb(
        "nb-whisper-large",
        "nb-whisper-large",
        "NB-AiLab · Norwegian-tuned · slow",
        repos={"nb-whisper": "NbAiLab/nb-whisper-large"},
    ),
    # ── Voxtral (Mistral) — audio LLM, no prompt/hotwords; batch-only ──
    # batch-only because `build_live_cmd` (live.py) only spawns
    # whisperlivekit-server with `--backend faster-whisper|mlx-whisper` (plus the
    # NB-Whisper `--model-path` route) — there is no Voxtral backend, so a live
    # selection could never be launched. Flip back to `_BATCH_AND_LIVE` only once
    # a Voxtral live channel is actually wired (cf. the planned ParakeetLiveChannel).
    ModelEntry(
        model_id="voxtral-mini",
        family="voxtral",
        display_name="voxtral-mini",
        description="Mistral Voxtral 3B · 8 langs (EN/ES/FR/PT/HI/DE/NL/IT) · no Norwegian",
        languages=("en", "es", "fr", "pt", "hi", "de", "nl", "it"),
        contexts=_BATCH_ONLY,
        backends=_VOXTRAL_BACKENDS,
        inputs=NO_INPUTS,
        repos={
            "voxtral-hf": "mistralai/Voxtral-Mini-3B-2507",
            "voxtral-mlx": "mlx-community/Voxtral-Mini-3B-2507-bf16",
        },
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
        repos={
            "parakeet-mlx": "mlx-community/parakeet-tdt-0.6b-v3",
            "parakeet-hf": "nvidia/parakeet-tdt-0.6b-v3",
        },
    ),
    # ── Moonshine (live-only, English) — MoonshineLiveChannel, see PRD #120 ──
    _moonshine("moonshine-tiny", "moonshine-tiny", "Moonshine Tiny · English · ultra-fast live"),
    _moonshine("moonshine-base", "moonshine-base", "Moonshine Base · English · fast live"),
)


REGISTRY: TranscriberRegistry = TranscriberRegistry(_DEFAULT_ENTRIES)


def repo_for(model_name: str, backend: str) -> str | None:
    """The registry-carried HF repo for `model_name` on `backend`, or None when
    the model has no registry entry or the entry carries no repo for that
    backend. The single lookup seam the per-adapter resolvers share, each
    supplying its own construct-by-convention fallback via `repo_for(...) or …`.
    """
    entry = REGISTRY.get(model_name)
    return entry.repos.get(backend) if entry is not None else None


def resolve_repo(model_name: str, backend_key: str, fallback: Callable[[str], str | None]) -> str | None:
    """Registry-first repo resolution with a per-adapter fallback.

    The `repo_for(...) or <fallback>` tail every adapter used to carry. It lives
    here, beside `repo_for`, rather than in `base`: `catalog` already imports
    `base`, so putting it the other way round would open the `base` <-> `catalog`
    cycle `base` is deliberately kept free of.

    `fallback` builds the construct-by-convention repo for an off-registry name;
    it may return None (the Moonshine adapters map a fixed set and raise on a
    miss), so the result is Optional.
    """
    return repo_for(model_name, backend_key) or fallback(model_name)


def fixed_language_for(model_name: str) -> str | None:
    """The registry-carried fixed language for `model_name` — the language
    twin of `repo_for`. The single lookup seam the adapters' `load()`s
    resolve through at CONSTRUCTION time (mirroring how they resolve their
    HF repo), so a loaded adapter carries its registry language on itself
    instead of re-reading mutable module state on every transcribe."""
    return REGISTRY.fixed_language_for(model_name)


# The bundled fallback batch model — what a transcribe runs with when neither
# the request body nor the operator's batch-model.txt default names one. The
# single source the /api/transcribe* routes and the end-of-meeting pipeline
# all resolve through.
#
# Multilingual by design (ADR-0010): the default SpecialistRoutingSelector
# routes by the generalist's DETECTED language, so an English-only default
# would always report "en" and the Norwegian specialist would never fire —
# da AND no would both silently fall back to English on a zero-config install.
# `large-v3-turbo` is the generalist ADR-0010 names; its weights download on
# the first transcribe (not at install).
DEFAULT_BATCH_MODEL: str = "large-v3-turbo"


@functools.lru_cache(maxsize=1)
def candidate_language_codes() -> tuple[str, ...]:
    """Every concrete language a catalog model declares (the "auto" auto-detect
    sentinel dropped) — the allowlist a candidate-language set
    (config/languages.txt or a per-meeting override) validates against, and the
    option list the dashboard picker offers. Ordered with the default set
    {da, no, en} first, then the rest alphabetically by display name.

    Memoised: `REGISTRY` is an immutable module-level singleton and the languages
    are static, so this is a pure constant — computing it once spares the walk +
    sort on every `is_candidate_language` call (which the validators run per code
    and the `/api/state` poll reaches via `read_languages`)."""
    codes: set[str] = set()
    for entry in REGISTRY.entries():
        for code in entry.languages:
            if code != "auto":
                codes.add(code)
    primary = [c for c in DEFAULT_CANDIDATE_LANGUAGES if c in codes]
    rest = sorted((c for c in codes if c not in primary), key=language_display_name)
    return tuple(primary + rest)


def language_display_name(code: str) -> str:
    """Human-readable label for a language code; falls back to the code itself
    for anything the catalog declares without a name."""
    return _LANGUAGE_NAMES.get(code, code)


def is_candidate_language(code: str) -> bool:
    """True iff `code` is a selectable candidate language (in the catalog
    vocabulary). The single membership check the config + session-meta writers
    validate against."""
    return code in candidate_language_codes()


def cover_models(candidate_languages: tuple[str, ...], *, generalist: str) -> tuple[str, ...]:
    """The set of models that COVER a candidate-language set (ADR-0010 slice 2):
    `{generalist} ∪ {SPECIALIST_MODELS[l] for l in candidate_languages}`.

    The generalist leads, then each declared language's specialist in the order
    the language first appears — so the generalist is the tie-break default when
    the selector can't separate two transcripts. Deduped (a specialist already
    equal to the generalist, or shared by two declared languages, runs once) and
    registry-validated (a specialist id absent from the catalog is dropped
    rather than handed to a loader — the same defensive guard the batch-model
    resolver applies). A set with no specialist language returns just
    `(generalist,)`, i.e. the slice-1 generalist-only behaviour."""
    models: list[str] = [generalist]
    for lang in candidate_languages:
        specialist = SPECIALIST_MODELS.get(lang)
        if specialist and specialist not in models and REGISTRY.get(specialist) is not None:
            models.append(specialist)
    return tuple(models)
