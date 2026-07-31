"""Local adapter — a bundled, offline, hardware-routed model (#86).

Routed at construction: an MLX backend (`mlx_lm`) on Apple Silicon, a GGUF/CPU
backend (`llama_cpp`) everywhere else — the same split as the transcriber
backends, resolved through the same `available_backends()` probe (see
`catalog.resolve_local_backend`). The heavy backend is lazy-imported and the
model loaded on the first `summarize()`, so importing tapscribe never pulls MLX
/ llama.cpp in.

The catalog (model list, allowlist, backend records, env knobs, hardware
routing) lives in `catalog`; this module is the adapter + its lazy generation
thunks. Catalog helpers are called module-qualified (`catalog.X`) so the test
seam — `monkeypatch.setattr("tapscribe.summarizers.catalog.X", …)` — is
unambiguous; the `_build_*_generate` thunks stay bare here so they're patched on
THIS module.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from datetime import UTC, datetime

from . import catalog
from .base import (
    SUMMARY_SYSTEM_FRAMING,
    SummarizerError,
    SummarizerFailed,
    SummarizerUnavailable,
    SummaryResult,
    build_model_input,
    fold_hint,
    resolve_prompt,
)

# A backend's per-(transcript, prompt) text generator. The real ones (built
# lazily below) hold a loaded model; tests inject a pure function instead — the
# same testability seam as the MLX adapters' `transcribe_fn`.
LocalGenerateFn = Callable[[str, str], str]


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
    b = catalog.BACKENDS[backend]
    return (
        f"the local summarizer couldn't load the {backend} model {model_repo!r} ({why}). "
        f"This is usually a mismatch between the model and the installed {b.module} — set "
        f"{b.env_model}=<a compatible repo> to override the bundled model, or update the "
        f"[summarize] extra (pip install -U -e '.[summarize]')."
    )


def _gguf_generation_failed_message(n_ctx: int, max_tokens: int, why: object) -> str:
    """The operator-facing error when the GGUF backend refuses to generate.

    Overwhelmingly this is the context window: llama-cpp raises
    `ValueError: Requested tokens (N) exceed context window of <n_ctx>` from
    INSIDE generation — outside `_build_generate_fn`'s try, which wraps only
    the load — so it used to surface as a bare 500. The window in force is
    `catalog.default_gguf_ctx()` (8192 by default), and `max_tokens` is
    carved out of it for the OUTPUT, leaving the rest for the transcript —
    roughly 25-30 minutes of meeting at the defaults. Note the top of
    `MAX_TOKENS_BOUNDS` (8192) equals the default window, so a maxed-out
    output cap leaves zero input room. The original error is quoted so a
    NON-window ValueError is never mislabelled."""
    room = n_ctx - max_tokens
    return (
        f"the local summarizer couldn't generate a summary ({why}). This is almost always the "
        f"transcript being too long for the {n_ctx}-token window: the output cap of {max_tokens} "
        f"tokens leaves ~{room} tokens for the transcript. Raise the summarize context "
        f"window in Settings → Advanced (or {catalog.ENV_GGUF_CTX}=<tokens>, bounded by "
        f"host RAM) — or lower {catalog.ENV_MAX_TOKENS} — and summarize again."
    )


def _build_local_messages(transcript: str, prompt: str) -> list[dict[str, str]]:
    """The chat messages handed to the local model: a single user turn carrying
    the system framing, the operator's prompt, and the transcript. One turn (no
    `system` role) keeps Gemma's chat template happy and is portable across both
    backends. A blank prompt falls back to `DEFAULT_SUMMARY_PROMPT`, the same
    string the view seeds and the command source defaults to."""
    instruction = f"{SUMMARY_SYSTEM_FRAMING}\n\n{resolve_prompt(prompt)}"
    return [{"role": "user", "content": build_model_input(instruction, transcript)}]


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

    Routed at construction (MLX on Apple Silicon, GGUF/CPU elsewhere) through the
    same `available_backends()` probe as the transcriber backends. The heavy
    backend is lazy-imported and the model loaded on the first `summarize()`
    (which the orchestrator runs off the event loop, inside the JobTracker slot —
    exactly like the command subprocess), so importing tapscribe never pulls MLX
    / llama.cpp in.

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
        self._backend = (backend or catalog.resolve_local_backend()).strip().lower()
        if self._backend not in catalog.BACKENDS:
            raise SummarizerUnavailable(f"unknown local summarizer backend: {self._backend!r}")
        self.model = model or catalog.env_model_default(self._backend)
        self._gguf_file = gguf_file or (
            os.environ.get(catalog.ENV_LOCAL_GGUF_FILE) or catalog.LOCAL_GGUF_FILE
        )
        self._max_tokens = (
            catalog.default_max_tokens() if max_tokens is None else catalog.clamp_max_tokens(max_tokens)
        )
        self._generate_fn = generate_fn
        # An injected generate_fn is the unit-test seam: it drives the adapter
        # without importing the backend OR going near the Hub, so it bypasses
        # BOTH construction probes below (the allowlist + the missing-extra check)
        # exactly as it always has for the dependency probe.
        if generate_fn is None:
            # Reject an unknown (untrusted) model BEFORE the disk read + slot
            # claim, so a bad pick from the dashboard fails fast as a clear 400 —
            # and a stray repo id never reaches `mlx_lm.load` / a Hub download.
            if not catalog.is_allowed_local_model(self._backend, self.model):
                raise SummarizerUnavailable(catalog.unknown_model_message(self._backend, self.model))
            # Fail fast + clear when we'd actually need the extra but it's absent.
            # `find_spec` only — no heavy import here.
            if not catalog.backend_module_available(self._backend):
                raise SummarizerUnavailable(
                    _missing_extra_message(
                        f"the {catalog.BACKENDS[self._backend].module!r} package isn't importable"
                    )
                )

    def summarize(self, transcript: str, *, prompt: str, names: Sequence[str] = ()) -> SummaryResult:
        generate = self._generate_fn or self._build_generate_fn()
        started = datetime.now(UTC)
        # The `generate` seam owns the chat-template wrapping (it holds the
        # tokenizer), so the known-people hint has to travel INSIDE the prompt it
        # receives — `_build_local_messages` re-resolves the DEFAULT idempotently,
        # so folding the hint onto the already-resolved instruction here is
        # equivalent downstream. `SummaryResult` below persists the ORIGINAL
        # operator `prompt`, not this augmented one.
        instruction = fold_hint(resolve_prompt(prompt), names)
        summary = (self._generate_checked(generate, transcript, instruction) or "").strip()
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

    def _generate_checked(self, generate: LocalGenerateFn, transcript: str, instruction: str) -> str:
        """Run the generation seam, budgeting the GGUF backend's fixed input
        window. `_build_generate_fn`'s try wraps only the model LOAD, so an
        overflow raised during generation had no domain-error mapping at all
        and reached the route as an untyped 500 (it isn't in
        `DOMAIN_ERROR_STATUS`). Mapping it to `SummarizerFailed` (→ 502) hands
        the operator the knob to raise instead of a traceback.

        GGUF only: the MLX path has no `n_ctx` cap (it feeds the model the whole
        prompt), so a ValueError there means something else entirely and is left
        to propagate rather than be blamed on a window that doesn't exist."""
        try:
            return generate(transcript, instruction)
        except ValueError as e:
            if self._backend != "gguf":
                raise
            raise SummarizerFailed(
                _gguf_generation_failed_message(catalog.default_gguf_ctx(), self._max_tokens, e)
            ) from e

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
                self.model, self._gguf_file, max_tokens=self._max_tokens, n_ctx=catalog.default_gguf_ctx()
            )
        except ImportError as e:
            raise SummarizerUnavailable(
                _missing_extra_message(
                    f"couldn't import the {catalog.BACKENDS[self._backend].module!r} backend ({e})"
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
