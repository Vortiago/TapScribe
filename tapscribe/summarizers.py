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

# The bundled default: Gemma 4 E4B-it (Google, Apr 2026) — the local-first
# default an operator can run with no network. Same model FAMILY on both
# backends so a summary reads the same regardless of hardware:
#   * Apple Silicon       → MLX 4-bit via `mlx_lm`
#   * CPU / CUDA / other   → GGUF Q4_K_M via `llama_cpp`
# HITL decision (size/license reviewed in #86): Apache-2.0, ~4.5B effective
# params, ~5 GB RAM at 4-bit. Every value here is operator-overridable via the
# env knobs below (and, once the config slice #84 lands, from the dashboard) so
# swapping the bundled model never needs a code change.
LOCAL_MLX_MODEL = "mlx-community/gemma-4-e4b-it-4bit"
LOCAL_GGUF_MODEL = "ggml-org/gemma-4-E4B-it-GGUF"
# `Llama.from_pretrained(filename=…)` matches this against the repo's files with
# fnmatch. The ggml-org gemma-4 GGUFs are lowercased; a case-tolerant glob keeps
# a differently-cased re-upload working without an env override.
LOCAL_GGUF_FILE = "*[qQ]4_[kK]_[mM].gguf"

# Operator knobs, hoisted to module constants so the (eventual) dashboard wiring
# + docs share ONE source of truth — same convention as the MLX transcriber
# chunk-size knobs and the command-source timeout above.
ENV_LOCAL_MLX_MODEL = "TAPSCRIBE_SUMMARIZE_MLX_MODEL"
ENV_LOCAL_GGUF_MODEL = "TAPSCRIBE_SUMMARIZE_GGUF_MODEL"
ENV_LOCAL_GGUF_FILE = "TAPSCRIBE_SUMMARIZE_GGUF_FILE"
# Generation length cap. A meeting summary is short; bound it so a runaway
# decode can't spin for minutes.
ENV_MAX_TOKENS = "TAPSCRIBE_SUMMARIZE_MAX_TOKENS"
_DEFAULT_MAX_TOKENS = 1024
_MAX_TOKENS_BOUNDS = (16, 8192)
# GGUF context window. Long meetings need a wide window; the default fits a
# typical session, env-tunable for very long ones (bounded by host RAM).
ENV_GGUF_CTX = "TAPSCRIBE_SUMMARIZE_GGUF_CTX"
_DEFAULT_GGUF_CTX = 8192
_GGUF_CTX_BOUNDS = (512, 131_072)

# Each hardware-routed backend ↔ the python package that powers it. Drives both
# the default-model pick and the cheap find_spec "is the extra installed" probe.
_BACKEND_MODULE = {"mlx": "mlx_lm", "gguf": "llama_cpp"}
_BACKEND_DEFAULT_MODEL = {"mlx": LOCAL_MLX_MODEL, "gguf": LOCAL_GGUF_MODEL}

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


def _default_gguf_ctx() -> int:
    """Current GGUF context window, re-read per call (see `_default_max_tokens`)."""
    return config.env_int(
        ENV_GGUF_CTX, _DEFAULT_GGUF_CTX, min_value=_GGUF_CTX_BOUNDS[0], max_value=_GGUF_CTX_BOUNDS[1]
    )


def _env_model_default(backend: str) -> str:
    """The default model repo for `backend`, with an env override
    (`TAPSCRIBE_SUMMARIZE_{MLX,GGUF}_MODEL`) so an operator can swap the bundled
    model without a code change."""
    env_name = ENV_LOCAL_MLX_MODEL if backend == "mlx" else ENV_LOCAL_GGUF_MODEL
    return os.environ.get(env_name) or _BACKEND_DEFAULT_MODEL[backend]


def _backend_module_available(backend: str) -> bool:
    """True iff the python package powering `backend` is importable — a cheap
    `find_spec` probe (no heavy import, no torch/Metal init). A module-level
    function so a test can force it without touching the real environment, the
    same shape as the catalog's `_is_module_available`."""
    module = _BACKEND_MODULE.get(backend)
    return module is not None and importlib.util.find_spec(module) is not None


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


def _build_gguf_generate(
    model_repo: str, gguf_file: str, *, max_tokens: int, n_ctx: int
) -> LocalGenerateFn:
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
        if self._backend not in _BACKEND_MODULE:
            raise SummarizerUnavailable(f"unknown local summarizer backend: {self._backend!r}")
        self.model = model or _env_model_default(self._backend)
        self._gguf_file = gguf_file or (os.environ.get(ENV_LOCAL_GGUF_FILE) or LOCAL_GGUF_FILE)
        self._max_tokens = _default_max_tokens() if max_tokens is None else max_tokens
        self._generate_fn = generate_fn
        # Fail fast + clear when we'd actually need the extra but it's absent.
        # Skipped when a generate_fn is injected (the unit-test path, which never
        # imports the backend). `find_spec` only — no heavy import here.
        if generate_fn is None and not _backend_module_available(self._backend):
            raise SummarizerUnavailable(
                f"the local summarizer needs the [summarize] extra — the "
                f"{_BACKEND_MODULE[self._backend]!r} package isn't importable. Install it with: "
                f"pip install -e '.[summarize]' (the recorder's start.sh / start.ps1 does this at bring-up)."
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
        """Lazy-import + load the routed backend. An ImportError here (extra
        uninstalled, or a broken install `find_spec` couldn't see through) is
        re-raised as `SummarizerUnavailable` so the operator gets the 'install
        the extra' message instead of a raw traceback mid-request."""
        try:
            if self._backend == "mlx":
                return _build_mlx_generate(self.model, self._max_tokens)
            return _build_gguf_generate(
                self.model, self._gguf_file, max_tokens=self._max_tokens, n_ctx=_default_gguf_ctx()
            )
        except ImportError as e:
            raise SummarizerUnavailable(
                f"the local summarizer needs the [summarize] extra — couldn't import the "
                f"{_BACKEND_MODULE[self._backend]!r} backend ({e}). Install it with: "
                f"pip install -e '.[summarize]'."
            ) from e


# ---------------------------------------------------------------------------
# Factory — dispatch on source
# ---------------------------------------------------------------------------


def load_summarizer(*, source: str, command: str = "", timeout_s: float | None = None) -> Summarizer:
    """Return a `Summarizer` for the chosen source, dispatching on `source`
    exactly as `load_transcriber` resolves a backend.

    `command` (#82) and `local` (#86, the bundled offline default) are wired;
    `api` (#85) still raises `SummarizerUnavailable` until its slice lands, so
    the view shows it disabled and a stray request fails with a clear 400. The
    `local` branch itself raises `SummarizerUnavailable` when the `[summarize]`
    extra isn't installed — the 'degrade clearly' path."""
    src = (source or "").strip().lower()
    if src == "command":
        return CommandSummarizer(command, timeout_s=timeout_s)
    if src == "local":
        return LocalSummarizer()
    if src == "api":
        raise SummarizerUnavailable("the 'api' summarizer source isn't wired yet")
    raise SummarizerUnavailable(f"unknown summarizer source: {source!r}")
