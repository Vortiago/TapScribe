"""Summarizer model catalog + hardware routing.

The selectable models an operator can pick from the Summarizer's model
<select>, plus the per-machine backend resolution and the operator knobs the
catalog advertises. This is the source-neutral catalog home (it serves the
local source today; #85's API source will add its own list here too), parallel
to `transcribers/catalog.py` but deliberately simpler — a flat per-backend
table, not a multi-backend registry.

The catalog DOUBLES as the security allowlist: a model id arriving in a POST
body is untrusted (CodeQL treats a request body as external input), so only
catalog members — plus the operator's env override / the bundled default — may
reach `mlx_lm.load` / a Hub download (see `_is_allowed_local_model`).
"""

from __future__ import annotations

import importlib.util
import os
from dataclasses import dataclass
from typing import Any

from .. import config
from .base import SummarizerUnavailable

# ---------------------------------------------------------------------------
# Bundled default models
# ---------------------------------------------------------------------------
#
# The local-first models an operator can run with no network. The MLX
# (Apple-Silicon) and GGUF (CPU/CUDA/other) backends currently run DIFFERENT
# models — see the divergence note — chosen so each hardware path has a model
# that actually loads.
#
# MLX → Qwen2.5 14B-Instruct, 4-bit (~8.3 GB). The previous default, Gemma 3
# 4B-it QAT, was simply a weak summariser at 4B. (It also surfaced a metadata
# quirk: the Gemma 3 MLX conversions ship a config.json missing
# `max_position_embeddings`, so some tools default the window to 4096 — that's a
# config smell, NOT a hard truncation in our direct `mlx_lm.generate` path, which
# feeds the model the whole prompt.) Qwen2.5 14B is dense (standard
# full-attention + GQA — loads cleanly under the pinned mlx-lm, no shared-KV /
# hybrid-cache landmines), uses its full 32K window, and is correctness-first
# within the 24 GB-Mac budget (~8 GB weights leave ~6-7 GB for the KV cache). The
# rest of the curated shortlist lives in `SUMMARY_MODELS` below, surfaced as the
# dashboard's model dropdown. On the MLX path the INPUT context is the model's
# native window (we never cap it); pick a 128K-window model (DeepSeek-R1-Distill,
# Mistral-Nemo) for genuinely long meetings. The tunable knob is the OUTPUT cap,
# max_tokens, below. (Gemma 4 E4B — the GGUF default — was the original
# cross-backend pick, but its E-series KV-sharing layout is unloadable by mlx-lm
# ≤0.31.3, "Received N parameters not in model"; that's why the backends diverge.)
#
# GGUF → Gemma 4 E4B-it Q4_K_M (~5 GB; HITL size/license review in #86,
# Apache-2.0, ~4.5B effective params). llama.cpp loads the E-series fine, so the
# CPU/CUDA path keeps it.
#
# Every value here is operator-overridable via the env knobs below (and, from
# the dashboard's model dropdown) so swapping a bundled model never needs a code
# change.
LOCAL_MLX_MODEL = "mlx-community/Qwen2.5-14B-Instruct-4bit"
LOCAL_GGUF_MODEL = "ggml-org/gemma-4-E4B-it-GGUF"
# `Llama.from_pretrained(filename=…)` matches this against the repo's files with
# fnmatch. The ggml-org gemma-4 GGUFs are lowercased; a case-tolerant glob keeps
# a differently-cased re-upload working without an env override.
LOCAL_GGUF_FILE = "*[qQ]4_[kK]_[mM].gguf"


# ---------------------------------------------------------------------------
# Selectable model catalog — the dashboard's per-backend model dropdown AND the
# allowlist the local source validates against.
# ---------------------------------------------------------------------------
#
# ONE declarative table per hardware backend: the models an operator can pick
# from the Summarizer's model <select>. The catalog is also the SECURITY
# allowlist — a model id arriving in a POST body (untrusted, per CodeQL) is only
# honoured if it's in this table, so a stray repo id can't flow into
# `mlx_lm.load` / a Hugging Face Hub download. The operator's env override
# (TAPSCRIBE_SUMMARIZE_{MLX,GGUF}_MODEL) and the bundled default are always
# allowed — those are operator-controlled, not external input.
#
# Footprints are the on-disk 4-bit weight size; a 24 GB Mac wants the weights +
# KV cache for a long meeting to stay well under the unified-RAM budget (the MLX
# list is curated for <~15 GB). `context_tokens` is the model's NATIVE window —
# on the MLX path mlx-lm uses it in full (we never cap the input), so a long
# transcript is bounded by the model, not by us. The first entry per backend is
# the recommended default; `LOCAL_*_MODEL` above points at it.
#
# Adding a model = adding one `SummaryModel` row here (and, for a different repo
# the operator wants to try once, the env override exists). The mlx-community
# 4-bit builds are chosen so each loads cleanly under the pinned mlx-lm — the
# Gemma-4 E-series shared-KV layout that mlx-lm ≤0.31.3 can't load is
# deliberately excluded (see the LOCAL_MLX_MODEL note above).


@dataclass(frozen=True)
class SummaryModel:
    """One selectable local-summarizer model: the HF repo id plus the metadata
    the dashboard dropdown shows. Frozen — the catalog is a constant table."""

    repo_id: str  # canonical Hugging Face repo id used everywhere (API, UI, load)
    label: str  # human-friendly dropdown label
    approx_gb: float  # on-disk weight size at this quant (GB)
    context_tokens: int  # native context window (tokens) — uncapped on the MLX path
    note: str = ""  # one-line "why pick this" shown under the dropdown

    def to_mapping(self) -> dict[str, Any]:
        """JSON-friendly view used by `GET /api/summarize/models`."""
        return {
            "repo_id": self.repo_id,
            "label": self.label,
            "approx_gb": self.approx_gb,
            "context_tokens": self.context_tokens,
            "note": self.note,
        }


# Apple-Silicon MLX models (mlx-community 4-bit unless noted), correctness-first
# within a ~15 GB unified-RAM budget on a 24 GB Mac (weights + KV cache for a
# long meeting). Curated from a verified research pass (existence, on-disk size,
# native context, mlx-lm load-compatibility, summarisation reputation). Every row
# loads cleanly under the pinned mlx-lm OR degrades to a clean 400 (the load
# fallback) — the Gemma 3 family is deliberately limited to the baseline because
# mlx-lm caps its context to 4096 tokens (fatal for transcripts). The first row
# is the default and must match LOCAL_MLX_MODEL. Footprints are on-disk weights;
# context_tokens is the EFFECTIVE window under mlx-lm.
_MLX_MODELS: tuple[SummaryModel, ...] = (
    SummaryModel(
        "mlx-community/Qwen2.5-14B-Instruct-4bit",
        "Qwen2.5 14B Instruct (4-bit) — recommended",
        approx_gb=8.3,
        context_tokens=32_768,
        note="best correctness that reliably loads + fits with KV headroom. The default.",
    ),
    SummaryModel(
        "mlx-community/Mistral-Small-24B-Instruct-2501-4bit",
        "Mistral Small 24B Instruct (4-bit)",
        approx_gb=13.3,
        context_tokens=32_000,
        note="biggest dense model in budget — more horsepower; thin KV margin on very long meetings.",
    ),
    SummaryModel(
        "Qwen/Qwen3-14B-MLX-4bit",
        "Qwen3 14B (4-bit)",
        approx_gb=7.9,
        context_tokens=32_768,
        note="newer-generation 14B. Thinking mode is on by default — raise max-tokens or disable it.",
    ),
    SummaryModel(
        "mlx-community/DeepSeek-R1-Distill-Qwen-14B-4bit",
        "DeepSeek-R1 Distill 14B (4-bit) — 128K context",
        approx_gb=8.3,
        context_tokens=131_072,
        note="widest context in budget, for genuinely long transcripts. Verbose — raise max-tokens.",
    ),
    SummaryModel(
        "mlx-community/Phi-4-reasoning-plus-4bit",
        "Phi-4 Reasoning Plus (4-bit)",
        approx_gb=9.2,
        context_tokens=32_768,
        note="reasoning-tuned — strong at structured extraction (decisions, action items, owners).",
    ),
    SummaryModel(
        "mlx-community/Mistral-Nemo-Instruct-2407-4bit",
        "Mistral Nemo 12B Instruct (4-bit)",
        approx_gb=6.9,
        context_tokens=128_000,
        note="lighter step-up with a 128K window and generous KV headroom.",
    ),
    SummaryModel(
        "mlx-community/Qwen2.5-7B-Instruct-4bit",
        "Qwen2.5 7B Instruct (4-bit)",
        approx_gb=4.3,
        context_tokens=32_768,
        note="small + fast, still a clear jump over the 4B baseline.",
    ),
    SummaryModel(
        "mlx-community/Mistral-7B-Instruct-v0.2-4bit",
        "Mistral 7B Instruct v0.2 (4-bit)",
        approx_gb=4.3,
        context_tokens=32_000,
        note="battle-tested, maximally-compatible 'always works' fallback.",
    ),
    SummaryModel(
        "mlx-community/gemma-3-4b-it-qat-4bit",
        "Gemma 3 4B-it (QAT 4-bit) — baseline",
        approx_gb=3.0,
        context_tokens=128_000,
        note="the previous default — small 4B model, weaker summaries. Kept for comparison.",
    ),
)

# GGUF / CPU·CUDA models (llama.cpp). The CPU path keeps the Gemma-4 E-series
# build (llama.cpp loads it fine). The first row is the default and must match
# LOCAL_GGUF_MODEL.
_GGUF_MODELS: tuple[SummaryModel, ...] = (
    SummaryModel(
        LOCAL_GGUF_MODEL,
        "Gemma 4 E4B-it (Q4_K_M)",
        approx_gb=5.0,
        context_tokens=128_000,
        note="bundled CPU/CUDA default — Apache-2.0, ~4.5B effective params.",
    ),
)

SUMMARY_MODELS: dict[str, tuple[SummaryModel, ...]] = {
    "mlx": _MLX_MODELS,
    "gguf": _GGUF_MODELS,
}


# ---------------------------------------------------------------------------
# Command-source presets — known CLI tools the dashboard offers as one-click
# templates. UNLIKE the local-model catalog above, this is NOT an allowlist:
# the command source is operator-trusted free text by design (the operator
# already controls which binaries exist on the host), so a preset only SEEDS
# the editable template field. What the rows add is hardening-by-default —
# flags an operator wouldn't know to write: the Claude row disables tool use
# so a prompt-injected transcript can't make the tool read files or fetch
# URLs. Add a tool = add a row; never narrow CommandSummarizer to one tool's
# quirks.
@dataclass(frozen=True)
class CommandPreset:
    """One dashboard-offered command template: a known CLI summarizer tool."""

    key: str  # stable identity, the dropdown <option> value
    label: str  # human-friendly dropdown label
    template: str  # the command template a pick seeds into the field
    note: str = ""  # one-line caveat/why shown under the dropdown

    def to_mapping(self) -> dict[str, Any]:
        """JSON-ready dict for `GET /api/summarize/models`."""
        return {"key": self.key, "label": self.label, "template": self.template, "note": self.note}


COMMAND_PRESETS: tuple[CommandPreset, ...] = (
    CommandPreset(
        key="claude",
        label="Claude Code",
        template='claude -p --tools "" --bare',
        note="tools disabled — a prompt-injected transcript can't read files or fetch URLs",
    ),
    CommandPreset(
        key="opencode",
        label="OpenCode",
        template="opencode run",
        note="runs your configured agent WITH its tools — prefer Claude Code for untrusted transcripts",
    ),
)

# Operator knobs, hoisted to module constants so the dashboard wiring + docs
# share ONE source of truth — same convention as the MLX transcriber chunk-size
# knobs and the command-source timeout.
ENV_LOCAL_MLX_MODEL = "TAPSCRIBE_SUMMARIZE_MLX_MODEL"
ENV_LOCAL_GGUF_MODEL = "TAPSCRIBE_SUMMARIZE_GGUF_MODEL"
ENV_LOCAL_GGUF_FILE = "TAPSCRIBE_SUMMARIZE_GGUF_FILE"
# Generation length cap (OUTPUT tokens). This is the OTHER half of the "summary
# feels truncated / too small" complaint: 1024 tokens cut off long structured
# summaries (decisions + action items + open questions over a 1-2 hr meeting),
# which reads as a context problem even though the INPUT isn't capped. 2048 fits
# a thorough summary; bounded so a runaway decode can't spin for minutes, and
# env-tunable higher for the reasoning models (Phi-4-reasoning, DeepSeek-R1
# distill) and Qwen3's thinking mode, which spend tokens on internal traces.
ENV_MAX_TOKENS = "TAPSCRIBE_SUMMARIZE_MAX_TOKENS"
_DEFAULT_MAX_TOKENS = 2048
_MAX_TOKENS_BOUNDS = (16, 8192)
# GGUF context window. Long meetings need a wide window; the default fits a
# typical session, env-tunable for very long ones (bounded by host RAM).
ENV_GGUF_CTX = "TAPSCRIBE_SUMMARIZE_GGUF_CTX"
_DEFAULT_GGUF_CTX = 8192
_GGUF_CTX_BOUNDS = (512, 131_072)


# One declarative record per hardware-routed backend (mirrors the catalog's
# table-driven REGISTRY): the python package that powers it (find_spec probe +
# lazy import), the bundled default model repo, and the env var that overrides
# that repo. Keeping "everything about a backend" in one keyed record means
# adding a backend is a single new entry — not edits spread across parallel
# dicts plus a branch.
@dataclass(frozen=True)
class _LocalBackend:
    module: str  # python package powering this backend (find_spec + lazy import)
    default_model: str  # bundled default model repo id
    env_model: str  # env var that overrides default_model


_BACKENDS: dict[str, _LocalBackend] = {
    "mlx": _LocalBackend(module="mlx_lm", default_model=LOCAL_MLX_MODEL, env_model=ENV_LOCAL_MLX_MODEL),
    "gguf": _LocalBackend(module="llama_cpp", default_model=LOCAL_GGUF_MODEL, env_model=ENV_LOCAL_GGUF_MODEL),
}


def _default_max_tokens() -> int:
    """Current generation cap, re-read per call so an operator can retune
    `TAPSCRIBE_SUMMARIZE_MAX_TOKENS` without a restart."""
    return config.env_int(
        ENV_MAX_TOKENS, _DEFAULT_MAX_TOKENS, min_value=_MAX_TOKENS_BOUNDS[0], max_value=_MAX_TOKENS_BOUNDS[1]
    )


def _clamp_max_tokens(value: int) -> int:
    """Clamp a caller-supplied output cap to the same bounds the env knob uses,
    so a UI-entered max_tokens can't ask for a runaway decode (or zero). ONE
    source of truth for the bounds — the dashboard's number input advertises the
    same min/max via `summary_model_catalog`."""
    lo, hi = _MAX_TOKENS_BOUNDS
    return max(lo, min(hi, value))


def _default_gguf_ctx() -> int:
    """Current GGUF context window, re-read per call (see `_default_max_tokens`)."""
    return config.env_int(
        ENV_GGUF_CTX, _DEFAULT_GGUF_CTX, min_value=_GGUF_CTX_BOUNDS[0], max_value=_GGUF_CTX_BOUNDS[1]
    )


def _env_model_default(backend: str) -> str:
    """The default model repo for `backend`, with an env override
    (`TAPSCRIBE_SUMMARIZE_{MLX,GGUF}_MODEL`) so an operator can swap the bundled
    model without a code change."""
    b = _BACKENDS[backend]
    return os.environ.get(b.env_model) or b.default_model


def _is_allowed_local_model(backend: str, model: str) -> bool:
    """True iff `model` may be loaded for `backend`. A model id arriving from the
    dashboard is untrusted (CodeQL treats a request body as external input), so
    only catalog members are honoured — a stray repo id can't flow into
    `mlx_lm.load` / a Hub download. The operator's env override and the bundled
    default (both surfaced by `_env_model_default`) are always allowed; they're
    operator-controlled, not external input."""
    if model == _env_model_default(backend):
        return True
    return any(m.repo_id == model for m in SUMMARY_MODELS.get(backend, ()))


def _unknown_model_message(backend: str, model: str) -> str:
    """The operator-facing error when a requested local model isn't in the
    backend's catalog allowlist. Names the rejected repo + the override env var
    so the operator either picks a listed model or sets the env knob for a
    one-off custom repo — same remediation shape as the load-failure message."""
    b = _BACKENDS[backend]
    listed = ", ".join(m.repo_id for m in SUMMARY_MODELS.get(backend, ())) or "(none)"
    return (
        f"the local summarizer model {model!r} isn't a known {backend} model. "
        f"Pick one of: {listed} — or set {b.env_model}=<repo> to run a custom model."
    )


def summary_model_catalog(backend: str | None = None) -> dict[str, Any]:
    """The selectable local-summarizer models for `backend` (default: the one
    this machine routes to), plus which repo is the active default. Drives the
    dashboard's `GET /api/summarize/models` picker — ONE source of truth with the
    `_is_allowed_local_model` allowlist the local source validates against."""
    b = (backend or _resolve_local_backend()).strip().lower()
    if b not in _BACKENDS:
        raise SummarizerUnavailable(f"unknown local summarizer backend: {b!r}")
    default = _env_model_default(b)
    return {
        "backend": b,
        "default": default,
        "models": [{**m.to_mapping(), "is_default": m.repo_id == default} for m in SUMMARY_MODELS.get(b, ())],
        # The OUTPUT-length knob the dashboard's number input seeds + bounds. The
        # input window isn't a knob (MLX uses the model's native window; GGUF's is
        # the separate TAPSCRIBE_SUMMARIZE_GGUF_CTX), so only max_tokens is here.
        "max_tokens_default": _default_max_tokens(),
        "max_tokens_min": _MAX_TOKENS_BOUNDS[0],
        "max_tokens_max": _MAX_TOKENS_BOUNDS[1],
        # Command-source presets ride along so the view needs ONE catalog
        # fetch. NOT an allowlist — see the CommandPreset block above.
        "command_presets": [p.to_mapping() for p in COMMAND_PRESETS],
    }


def _backend_module_available(backend: str) -> bool:
    """True iff the python package powering `backend` is importable — a cheap
    `find_spec` probe (no heavy import, no torch/Metal init). A module-level
    function so a test can force it without touching the real environment, the
    same shape as the transcriber catalog's `_is_module_available`."""
    b = _BACKENDS.get(backend)
    return b is not None and importlib.util.find_spec(b.module) is not None


def _resolve_local_backend() -> str:
    """Pick the backend for this machine, reusing the transcriber catalog's
    hardware probe so 'is this an Apple-Silicon MLX box' has ONE source of truth
    (no shadow detection). MLX on Apple Silicon; the GGUF/CPU path everywhere
    else — CPU and CUDA alike, since there's no MLX off Apple Silicon."""
    from ..transcribers.catalog import available_backends

    return "mlx" if "mlx" in available_backends() else "gguf"
