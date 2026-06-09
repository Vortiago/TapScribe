"""Summarizers — adapters that turn a session's merged transcript into a summary.

The post-transcription mirror of the Transcriber seam (`tapscribe.transcribers`),
one altitude up: a `Summarizer` is "something that can summarize one transcript
given a prompt" (one verb, like `Transcriber.transcribe`), and
`load_summarizer(source=…)` is the factory that picks the adapter — the same
shape as `load_transcriber(name, backend=…)`.

The first adapter is `CommandSummarizer`: it pipes the transcript to an
operator-supplied CLI tool (e.g. `claude -p`) on **stdin** and reads the summary
from **stdout**. No network, no heavy dependency — which is exactly why it's the
tracer-bullet source (#82) that proves the whole vertical seam (protocol →
Batch-summarize orchestrator → route → wired view). The API and Local sources
(#85 / #86) land behind this same one-method protocol as new adapters, not a
rewrite.

Security: `CommandSummarizer` builds its argv in **list form** (`shlex.split`
of the operator's template, with the prompt appended as a trailing positional
arg) and runs with `shell=False` — never an f-string, never `shell=True` — so a
transcript or prompt can't break out into a shell. Same subprocess-argv
discipline as `tapscribe.live.build_live_cmd`.
"""

from __future__ import annotations

import importlib.util
import os
import shlex
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from . import config

# The default summarization prompt. Lives here so the backend fallback and the
# dashboard's prompt placeholder share ONE source of truth (the view seeds the
# same string; the route falls back to it when the body omits a prompt).
DEFAULT_SUMMARY_PROMPT = "Summarize this meeting into decisions, action items, and open questions."

# Operator knob for the per-summarize subprocess timeout, hoisted to a module
# constant so the (eventual) dashboard wiring + docs have one source of truth —
# same convention as the MLX chunk-size knobs on the transcriber adapters.
ENV_TIMEOUT_S = "TAPSCRIBE_SUMMARIZE_TIMEOUT_S"
_DEFAULT_TIMEOUT_S = 120.0
# A summarize is one short subprocess call; bound the timeout between 1 s and an
# hour so a typo can't wedge a job forever or fail a slow local model instantly.
_TIMEOUT_BOUNDS = (1.0, 3600.0)


def _default_timeout_s() -> float:
    """Current per-summarize subprocess timeout (seconds), re-read per call so
    an operator can retune `TAPSCRIBE_SUMMARIZE_TIMEOUT_S` without a restart —
    same as how the transcriber idle-TTL knob is read per call."""
    return config.env_float(
        ENV_TIMEOUT_S,
        _DEFAULT_TIMEOUT_S,
        min_value=_TIMEOUT_BOUNDS[0],
        max_value=_TIMEOUT_BOUNDS[1],
    )


# ---------------------------------------------------------------------------
# Domain errors — the route layer maps these to HTTP codes
# ---------------------------------------------------------------------------


class SummarizerError(Exception):
    """Base class for every summarizer-adapter domain error."""


class SummarizerUnavailable(SummarizerError):
    """The chosen source is misconfigured or not wired yet — an empty command
    template, a command whose executable can't be found, or the `api`/`local`
    source before its slice lands. The operator must fix the configuration;
    routes map this to 400."""


class SummarizerFailed(SummarizerError):
    """The summarizer ran but failed: the command exited non-zero, timed out,
    or produced no output. The configuration was fine — the tool itself failed.
    Routes map this to 502 (we proxied to an external tool and it failed)."""


# ---------------------------------------------------------------------------
# Result — frozen, parallel to TranscriptionResult
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SummaryResult:
    """The output of `Summarizer.summarize(...)`: the summary text plus the
    metadata that says which source / engine / prompt produced it and how long
    it took. Parallel to `TranscriptionResult`; frozen so a caller can't mutate
    a result in place."""

    summary: str
    source: str  # "command" | "api" | "local"
    prompt: str  # the prompt that was in effect
    model: str = ""  # api/local engine label; empty for the command source
    command: str = ""  # command source: the CLI template that produced it
    took_ms: int = 0
    created_at: str = ""  # ISO-8601 UTC, when the run started

    def to_mapping(self) -> dict[str, Any]:
        """Wire shape the route returns to the dashboard. Stable key set across
        sources so the view renders the same fields regardless of which adapter
        produced the summary."""
        return {
            "summary": self.summary,
            "source": self.source,
            "prompt": self.prompt,
            "model": self.model,
            "command": self.command,
            "took_ms": self.took_ms,
            "created_at": self.created_at,
        }


# ---------------------------------------------------------------------------
# Protocol — the one-verb seam every adapter satisfies
# ---------------------------------------------------------------------------


@runtime_checkable
class Summarizer(Protocol):
    """Summarize one transcript given a prompt. Mirrors `Transcriber`'s single
    verb. Adapters may be stateless (CommandSummarizer holds only its parsed
    argv) or hold a client/model (the future API / Local adapters)."""

    source: str

    def summarize(self, transcript: str, *, prompt: str) -> SummaryResult: ...


# ---------------------------------------------------------------------------
# Command adapter — pipe the transcript to a CLI tool
# ---------------------------------------------------------------------------


class CommandSummarizer:
    """Summarize by piping the transcript to an operator-supplied CLI tool.

    `command` is a shell-style template (e.g. `claude -p`) parsed with
    `shlex.split` into **list-form** argv. The merged transcript is written to
    the child's **stdin**; the summary is read from its **stdout**. The prompt
    is appended as a trailing positional argument when non-empty, so `claude -p`
    becomes `["claude", "-p", "<prompt>"]` with the transcript piped in — the
    canonical `cat transcript | claude -p "<prompt>"` shape. Bounded by
    `timeout_s`.

    Never uses `shell=True` / an f-string, so transcript or prompt content can't
    inject a shell command — the same argv discipline as `build_live_cmd`.
    """

    source = "command"

    def __init__(self, command: str, *, timeout_s: float | None = None) -> None:
        argv = shlex.split(command or "")
        if not argv:
            raise SummarizerUnavailable("the command source needs a non-empty command template")
        self._argv = argv
        self.command = (command or "").strip()
        self._timeout_s = _default_timeout_s() if timeout_s is None else timeout_s

    def summarize(self, transcript: str, *, prompt: str) -> SummaryResult:
        # List-form argv only (the operator template parsed by shlex + the
        # prompt as a trailing positional). Never interpolated into a shell
        # string — see the class docstring.
        argv = list(self._argv)
        if prompt:
            argv.append(prompt)
        started = datetime.now(UTC)
        try:
            proc = subprocess.run(
                argv,
                input=transcript.encode("utf-8"),
                capture_output=True,
                timeout=self._timeout_s,
                check=False,
            )
        except FileNotFoundError as e:
            # The configured executable isn't on PATH — operator misconfig, not
            # a transient failure. Surface as Unavailable (→ 400) so the message
            # points at the command, not at the transcript.
            raise SummarizerUnavailable(f"command not found: {self._argv[0]!r}") from e
        except subprocess.TimeoutExpired as e:
            # run() has already killed + reaped the child by the time this fires.
            raise SummarizerFailed(f"command timed out after {self._timeout_s:g}s") from e
        except OSError as e:
            raise SummarizerFailed(f"could not run command: {e}") from e

        took_ms = int((datetime.now(UTC) - started).total_seconds() * 1000)
        if proc.returncode != 0:
            stderr = proc.stderr.decode("utf-8", "replace").strip()
            detail = f" — {stderr}" if stderr else ""
            raise SummarizerFailed(f"command exited {proc.returncode}{detail}")
        summary = proc.stdout.decode("utf-8", "replace").strip()
        if not summary:
            raise SummarizerFailed("command produced no output on stdout")
        return SummaryResult(
            summary=summary,
            source=self.source,
            prompt=prompt,
            command=self.command,
            took_ms=took_ms,
            created_at=started.isoformat(),
        )


# ---------------------------------------------------------------------------
# Local adapter — a bundled, offline, hardware-routed model (#86)
# ---------------------------------------------------------------------------

# The bundled defaults: the local-first models an operator can run with no
# network. The MLX (Apple-Silicon) and GGUF (CPU/CUDA/other) backends currently
# run DIFFERENT models — see the divergence note — chosen so each hardware path
# has a model that actually loads.
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
# ONE declarative table per hardware backend (mirrors the transcriber catalog's
# REGISTRY): the models an operator can pick from the Summarizer's model
# <select>. The catalog is also the SECURITY allowlist — a model id arriving in
# a POST body (untrusted, per CodeQL) is only honoured if it's in this table, so
# a stray repo id can't flow into `mlx_lm.load` / a Hugging Face Hub download.
# The operator's env override (TAPSCRIBE_SUMMARIZE_{MLX,GGUF}_MODEL) and the
# bundled default are always allowed — those are operator-controlled, not
# external input.
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

# Operator knobs, hoisted to module constants so the (eventual) dashboard wiring
# + docs share ONE source of truth — same convention as the MLX transcriber
# chunk-size knobs and the command-source timeout above.
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

# A backend's per-(transcript, prompt) text generator. The real ones (built
# lazily below) hold a loaded model; tests inject a pure function instead — the
# same testability seam as the MLX adapters' `transcribe_fn`.
LocalGenerateFn = Callable[[str, str], str]

# Folded into the single user turn rather than a `system` message: Gemma's chat
# template raises on a system role, and one user turn is portable across every
# instruct model AND both backends.
_LOCAL_SYSTEM = (
    "You are a meeting-summarisation assistant. Read the transcript and produce a clear, "
    "well-structured summary. Output only the summary."
)


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
    }


def _backend_module_available(backend: str) -> bool:
    """True iff the python package powering `backend` is importable — a cheap
    `find_spec` probe (no heavy import, no torch/Metal init). A module-level
    function so a test can force it without touching the real environment, the
    same shape as the catalog's `_is_module_available`."""
    b = _BACKENDS.get(backend)
    return b is not None and importlib.util.find_spec(b.module) is not None


def _missing_extra_message(why: str) -> str:
    """The operator-facing 'install the [summarize] extra' error, in ONE place so
    the construction-time probe and the lazy-import handler can't drift on the
    install recipe. `why` is the site-specific reason clause."""
    return (
        f"the local summarizer needs the [summarize] extra — {why}. Install it with: "
        f"pip install -e '.[summarize]' (the recorder's start.sh / start.ps1 does this at bring-up)."
    )


def _model_load_failed_message(backend: str, model_repo: str, why: object) -> str:
    """The operator-facing error when the backend imports fine but the model
    itself won't load — the mlx_lm 'Received N parameters not in model'
    weight/arch skew, a corrupt/incompatible Hub download, or an OOM. Names the
    failed repo and the override env var so the operator can swap to a model that
    loads (or update the extra) instead of staring at a raw 500."""
    b = _BACKENDS[backend]
    return (
        f"the local summarizer couldn't load the {backend} model {model_repo!r} ({why}). "
        f"This is usually a mismatch between the model and the installed {b.module} — set "
        f"{b.env_model}=<a compatible repo> to override the bundled model, or update the "
        f"[summarize] extra (pip install -U -e '.[summarize]')."
    )


def _resolve_local_backend() -> str:
    """Pick the backend for this machine, reusing the transcriber catalog's
    hardware probe so 'is this an Apple-Silicon MLX box' has ONE source of truth
    (no shadow detection). MLX on Apple Silicon; the GGUF/CPU path everywhere
    else — CPU and CUDA alike, since there's no MLX off Apple Silicon."""
    from .transcribers.catalog import available_backends

    return "mlx" if "mlx" in available_backends() else "gguf"


def _build_local_messages(transcript: str, prompt: str) -> list[dict[str, str]]:
    """The chat messages handed to the local model: a single user turn carrying
    the system framing, the operator's prompt, and the transcript. One turn (no
    `system` role) keeps Gemma's chat template happy and is portable across both
    backends. A blank prompt falls back to `DEFAULT_SUMMARY_PROMPT`, the same
    string the view seeds and the command source defaults to."""
    instruction = (prompt or "").strip() or DEFAULT_SUMMARY_PROMPT
    content = f"{_LOCAL_SYSTEM}\n\n{instruction}\n\n--- TRANSCRIPT ---\n{transcript}"
    return [{"role": "user", "content": content}]


def _build_mlx_generate(model_repo: str, max_tokens: int) -> LocalGenerateFn:
    """Load the MLX model once and return a `(transcript, prompt) -> summary`
    closure. `mlx_lm` is imported lazily so importing tapscribe never pulls MLX
    in — and so the unit tests (which inject a generate_fn) don't need it."""
    from mlx_lm import generate, load  # lazy: heavy, Apple-Silicon-only

    model, tokenizer = load(model_repo)

    def _generate(transcript: str, prompt: str) -> str:
        chat = tokenizer.apply_chat_template(
            _build_local_messages(transcript, prompt),
            add_generation_prompt=True,
            tokenize=False,
        )
        return generate(model, tokenizer, prompt=chat, max_tokens=max_tokens)

    return _generate


def _build_gguf_generate(model_repo: str, gguf_file: str, *, max_tokens: int, n_ctx: int) -> LocalGenerateFn:
    """Download (first call, then HF-cached) + load the GGUF model once and
    return a `(transcript, prompt) -> summary` closure. `llama_cpp` is imported
    lazily for the same reason as the MLX builder."""
    from llama_cpp import Llama  # lazy: heavy native extension

    llm = Llama.from_pretrained(repo_id=model_repo, filename=gguf_file, n_ctx=n_ctx, verbose=False)

    def _generate(transcript: str, prompt: str) -> str:
        out = llm.create_chat_completion(
            messages=_build_local_messages(transcript, prompt), max_tokens=max_tokens
        )
        return out["choices"][0]["message"].get("content") or ""

    return _generate


class LocalSummarizer:
    """Summarize with a bundled, offline, hardware-routed model.

    Routed at construction: an MLX backend (`mlx_lm`) on Apple Silicon, a
    GGUF/CPU backend (`llama_cpp`) everywhere else — the same split as the
    transcriber backends, resolved through the same `available_backends()`
    probe. The heavy backend is lazy-imported and the model loaded on the first
    `summarize()` (which the orchestrator runs off the event loop, inside the
    JobTracker slot — exactly like the command subprocess), so importing
    tapscribe never pulls MLX / llama.cpp in.

    `generate_fn` is the testability seam (mirrors the MLX adapters'
    `transcribe_fn`): inject a `(transcript, prompt) -> summary` callable to
    drive the adapter without the extra installed; production leaves it None and
    builds the real backend lazily.

    A missing `[summarize]` extra degrades CLEARLY: with no injected generate_fn
    and the backend package not importable, construction raises
    `SummarizerUnavailable` (→ 400) naming the extra — fail-fast, before the
    orchestrator's disk read + slot claim, never a crash deep in a lazy import
    mid-request.
    """

    source = "local"

    def __init__(
        self,
        *,
        backend: str | None = None,
        model: str = "",
        gguf_file: str = "",
        max_tokens: int | None = None,
        generate_fn: LocalGenerateFn | None = None,
    ) -> None:
        self._backend = (backend or _resolve_local_backend()).strip().lower()
        if self._backend not in _BACKENDS:
            raise SummarizerUnavailable(f"unknown local summarizer backend: {self._backend!r}")
        self.model = model or _env_model_default(self._backend)
        self._gguf_file = gguf_file or (os.environ.get(ENV_LOCAL_GGUF_FILE) or LOCAL_GGUF_FILE)
        self._max_tokens = _default_max_tokens() if max_tokens is None else _clamp_max_tokens(max_tokens)
        self._generate_fn = generate_fn
        # An injected generate_fn is the unit-test seam: it drives the adapter
        # without importing the backend OR going near the Hub, so it bypasses
        # BOTH construction probes below (the allowlist + the missing-extra check)
        # exactly as it always has for the dependency probe.
        if generate_fn is None:
            # Reject an unknown (untrusted) model BEFORE the disk read + slot
            # claim, so a bad pick from the dashboard fails fast as a clear 400 —
            # and a stray repo id never reaches `mlx_lm.load` / a Hub download.
            if not _is_allowed_local_model(self._backend, self.model):
                raise SummarizerUnavailable(_unknown_model_message(self._backend, self.model))
            # Fail fast + clear when we'd actually need the extra but it's absent.
            # `find_spec` only — no heavy import here.
            if not _backend_module_available(self._backend):
                raise SummarizerUnavailable(
                    _missing_extra_message(
                        f"the {_BACKENDS[self._backend].module!r} package isn't importable"
                    )
                )

    def summarize(self, transcript: str, *, prompt: str) -> SummaryResult:
        generate = self._generate_fn or self._build_generate_fn()
        started = datetime.now(UTC)
        summary = (generate(transcript, prompt) or "").strip()
        took_ms = int((datetime.now(UTC) - started).total_seconds() * 1000)
        if not summary:
            # Exit-0-but-blank is a useless summary — same contract as the
            # command source, so the operator isn't handed an empty panel.
            raise SummarizerFailed("the local model produced an empty summary")
        return SummaryResult(
            summary=summary,
            source=self.source,
            prompt=prompt,
            model=self.model,
            took_ms=took_ms,
            created_at=started.isoformat(),
        )

    def _build_generate_fn(self) -> LocalGenerateFn:
        """Lazy-import + load the routed backend. An ImportError (extra
        uninstalled, or a broken install `find_spec` couldn't see through) maps
        to `SummarizerUnavailable` with the 'install the extra' message; any
        OTHER construction failure — the model imports but won't load (mlx_lm's
        'Received N parameters not in model' weight/arch skew, a corrupt Hub
        download, OOM) — maps to `SummarizerUnavailable` naming the model + the
        override env var. Either way the operator gets a clear 400, never a raw
        traceback mid-request."""
        try:
            if self._backend == "mlx":
                return _build_mlx_generate(self.model, self._max_tokens)
            return _build_gguf_generate(
                self.model, self._gguf_file, max_tokens=self._max_tokens, n_ctx=_default_gguf_ctx()
            )
        except ImportError as e:
            raise SummarizerUnavailable(
                _missing_extra_message(
                    f"couldn't import the {_BACKENDS[self._backend].module!r} backend ({e})"
                )
            ) from e
        except SummarizerError:
            # Already a domain error with its own HTTP status — let it through
            # rather than re-wrapping it as a generic load failure.
            raise
        except Exception as e:
            # The extra IS importable (ImportError handled above) but the backend
            # couldn't construct the model: the mlx_lm "Received N parameters not
            # in model" weight/arch skew, a corrupt/incompatible Hub download, or
            # an OOM. The try wraps only the cheap load() call (the returned
            # closure isn't invoked here), so this can't mask a generation bug.
            # Map to a clear SummarizerUnavailable (→ 400) naming the model + the
            # override env var so the operator gets remediation, not a raw 500.
            raise SummarizerUnavailable(_model_load_failed_message(self._backend, self.model, e)) from e


# ---------------------------------------------------------------------------
# Factory — dispatch on source
# ---------------------------------------------------------------------------


def load_summarizer(
    *,
    source: str,
    command: str = "",
    model: str = "",
    max_tokens: int | None = None,
    timeout_s: float | None = None,
) -> Summarizer:
    """Return a `Summarizer` for the chosen source, dispatching on `source`
    exactly as `load_transcriber` resolves a backend.

    `command` (#82) and `local` (#86, the bundled offline default) are wired;
    `api` (#85) still raises `SummarizerUnavailable` until its slice lands, so
    the view shows it disabled and a stray request fails with a clear 400. The
    `local` branch itself raises `SummarizerUnavailable` when the `[summarize]`
    extra isn't installed — the 'degrade clearly' path — or when `model` isn't in
    the backend's catalog allowlist.

    `model` selects which bundled model the `local` source loads (the dashboard's
    model dropdown sends it); empty falls back to the backend default. `max_tokens`
    caps the OUTPUT length (the dashboard's number input sends it); None falls back
    to the env default. Both are ignored by the `command` source (an external CLI
    owns its own model + length)."""
    src = (source or "").strip().lower()
    if src == "command":
        return CommandSummarizer(command, timeout_s=timeout_s)
    if src == "local":
        return LocalSummarizer(model=model, max_tokens=max_tokens)
    if src == "api":
        raise SummarizerUnavailable("the 'api' summarizer source isn't wired yet")
    raise SummarizerUnavailable(f"unknown summarizer source: {source!r}")
