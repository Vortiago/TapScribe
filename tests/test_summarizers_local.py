"""Direct tests for the Local summarizer source (tapscribe.summarizers.LocalSummarizer).

The Local source is the bundled, offline, hardware-routed summarizer (#86): an
MLX backend (`mlx_lm`) on Apple Silicon, a GGUF/CPU backend (`llama_cpp`)
elsewhere. The heavy backends are lazy-imported, so these tests drive the
adapter through an **injected `generate_fn`** — the same testability seam the
MLX transcriber adapters use (`transcribe_fn`) — and never import mlx_lm /
llama_cpp or download a 4.5 GB model. The routing decision is exercised by
forcing `available_backends()` via the runtime probe's test hook.

The two `importorskip`-gated smoke tests at the bottom pin the upstream symbols
the real backends import lazily (`mlx_lm.load`/`generate`,
`llama_cpp.Llama.from_pretrained`/`create_chat_completion`). They no-op on hosts
where the `[summarize]` extra isn't installed (the whole Linux CI matrix) and
run in full wherever the package is present — the same convention as
`test_transformers_parakeet_upstream_contract`.
"""

from __future__ import annotations

import pytest

# Internal helpers live in their submodules; import them from where they're
# defined (the package __init__ re-exports only the public interface).
from tapscribe.runtime_probe import set_available_backends_for_testing
from tapscribe.summarizers import (
    DEFAULT_SUMMARY_PROMPT,
    ENV_LOCAL_GGUF_MODEL,
    ENV_LOCAL_MLX_MODEL,
    LOCAL_GGUF_MODEL,
    LOCAL_MLX_MODEL,
    LocalSummarizer,
    SummarizerFailed,
    SummarizerUnavailable,
    SummaryResult,
    load_summarizer,
    summary_model_catalog,
)
from tapscribe.summarizers.catalog import MAX_TOKENS_BOUNDS, clamp_max_tokens
from tapscribe.summarizers.local import _build_local_messages


def test_catalog_cross_module_names_are_public():
    """These 10 names are consumed by local.py (9 of them) and text.py (3,
    including the security allowlist) across the module/package boundary
    (#262) — underscore-private was a false signal that they were safe to
    change without checking outside catalog.py."""
    from tapscribe.summarizers import catalog

    for name in (
        "BACKENDS",
        "resolve_local_backend",
        "env_model_default",
        "is_allowed_local_model",
        "unknown_model_message",
        "default_max_tokens",
        "clamp_max_tokens",
        "backend_module_available",
        "default_gguf_ctx",
        "MAX_TOKENS_BOUNDS",
    ):
        assert hasattr(catalog, name), f"catalog.{name} should be public"


@pytest.fixture
def reset_available_backends():
    """Restore the catalog's auto-probe after a test forces the backend set,
    so a forced {'mlx'}/{'cpu'} can't leak into another test's routing."""
    yield
    set_available_backends_for_testing(None)


@pytest.fixture
def extra_present(monkeypatch):
    """Pretend the `[summarize]` backend module IS importable, so a no-generate_fn
    construction doesn't fail the fast dependency probe. Deterministic regardless
    of whether this dev box happens to have mlx_lm / llama_cpp installed."""
    monkeypatch.setattr("tapscribe.summarizers.catalog.backend_module_available", lambda backend: True)


@pytest.fixture
def extra_missing(monkeypatch):
    """Pretend the `[summarize]` backend module is NOT importable — the fresh-box
    case the runtime-deps step is meant to fix, and the 'degrade clearly' path."""
    monkeypatch.setattr("tapscribe.summarizers.catalog.backend_module_available", lambda backend: False)


# ---------------------------------------------------------------------------
# Adapter behaviour — driven through an injected generate_fn (no heavy import)
# ---------------------------------------------------------------------------


def test_local_summarizer_returns_result_with_source_and_model(reset_available_backends):
    s = LocalSummarizer(backend="gguf", generate_fn=lambda transcript, prompt: f"S[{prompt}]:{transcript}")
    res = s.summarize("the merged transcript", prompt="Summarize it")
    assert isinstance(res, SummaryResult)
    assert res.summary == "S[Summarize it]:the merged transcript"
    assert res.source == "local"
    assert res.prompt == "Summarize it"
    assert res.model == LOCAL_GGUF_MODEL  # the gguf default — surfaced in the result
    assert res.command == ""  # the local source has no CLI command
    assert res.took_ms >= 0
    assert res.created_at  # ISO timestamp recorded


def test_local_summarizer_passes_transcript_and_prompt_to_backend(reset_available_backends):
    """The transcript and prompt reach the backend verbatim — the adapter is a
    thin wrapper around generate_fn(transcript, prompt)."""
    seen: dict[str, str] = {}

    def spy(transcript: str, prompt: str) -> str:
        seen["transcript"] = transcript
        seen["prompt"] = prompt
        return "a summary"

    LocalSummarizer(backend="gguf", generate_fn=spy).summarize("transcript body", prompt="do the thing")
    assert seen == {"transcript": "transcript body", "prompt": "do the thing"}


def test_local_summarizer_empty_output_raises_failed(reset_available_backends):
    """A blank generation is a useless summary — same contract as the command
    source: surface a failure rather than hand the operator an empty panel."""
    s = LocalSummarizer(backend="mlx", generate_fn=lambda transcript, prompt: "   \n  ")
    with pytest.raises(SummarizerFailed):
        s.summarize("x", prompt="y")


def test_local_summarizer_mlx_backend_uses_mlx_default_model(reset_available_backends):
    s = LocalSummarizer(backend="mlx", generate_fn=lambda transcript, prompt: "ok")
    assert s.model == LOCAL_MLX_MODEL


def test_local_summarizer_gguf_backend_uses_gguf_default_model(reset_available_backends):
    s = LocalSummarizer(backend="gguf", generate_fn=lambda transcript, prompt: "ok")
    assert s.model == LOCAL_GGUF_MODEL


def test_local_summarizer_model_override_wins(reset_available_backends):
    s = LocalSummarizer(backend="mlx", model="me/custom-4bit", generate_fn=lambda t, p: "ok")
    assert s.model == "me/custom-4bit"


# ---------------------------------------------------------------------------
# Model catalog + allowlist — the dropdown's source of truth (the curated, per-
# backend table `GET /api/summarize/models` serialises) DOUBLES as the security
# allowlist: a model id arriving in a POST body is untrusted (CodeQL treats it
# as external input), so only catalog members — plus the operator's env override
# / the bundled default — may reach `mlx_lm.load` / a Hub download.
# ---------------------------------------------------------------------------


def test_summary_model_catalog_shape_and_default(reset_available_backends):
    cat = summary_model_catalog("gguf")
    assert cat["backend"] == "gguf"
    assert cat["default"] == LOCAL_GGUF_MODEL
    assert cat["models"], "the gguf catalog must list at least the bundled default"
    # Exactly the default row is flagged is_default, and it matches the top-level.
    assert [m["repo_id"] for m in cat["models"] if m["is_default"]] == [LOCAL_GGUF_MODEL]
    assert {"repo_id", "label", "approx_gb", "context_tokens", "note", "is_default"} <= set(cat["models"][0])
    # The output-cap knob the dashboard's number input seeds + bounds.
    assert cat["max_tokens_min"] == MAX_TOKENS_BOUNDS[0]
    assert cat["max_tokens_max"] == MAX_TOKENS_BOUNDS[1]
    assert MAX_TOKENS_BOUNDS[0] <= cat["max_tokens_default"] <= MAX_TOKENS_BOUNDS[1]


def test_clamp_max_tokens_bounds():
    lo, hi = MAX_TOKENS_BOUNDS
    assert clamp_max_tokens(hi + 100_000) == hi  # runaway decode capped
    assert clamp_max_tokens(0) == lo  # zero/negative floored
    assert clamp_max_tokens(lo + 1) == lo + 1  # in-range passes through


def test_local_summarizer_clamps_caller_max_tokens(reset_available_backends):
    """A UI-supplied output cap is clamped to the env knob's bounds, so a typo in
    the number input can't ask for a runaway (or zero-length) decode."""
    hi = MAX_TOKENS_BOUNDS[1]
    s = LocalSummarizer(backend="gguf", max_tokens=hi + 50_000, generate_fn=lambda t, p: "ok")
    assert s._max_tokens == hi


def test_load_summarizer_local_threads_and_clamps_max_tokens(reset_available_backends, extra_present):
    hi = MAX_TOKENS_BOUNDS[1]
    s = load_summarizer(source="local", max_tokens=hi + 1)
    assert isinstance(s, LocalSummarizer)
    assert s._max_tokens == hi


def test_summary_model_catalog_unknown_backend_raises():
    with pytest.raises(SummarizerUnavailable):
        summary_model_catalog("tpu")


def test_local_summarizer_rejects_unknown_model(reset_available_backends, extra_present):
    """A model that's neither in the catalog nor the env override is rejected at
    construction (→ 400) BEFORE any Hub access — names the model + the override
    env var, so the operator either picks a listed model or sets the knob."""
    with pytest.raises(SummarizerUnavailable) as ei:
        LocalSummarizer(backend="gguf", model="evil/not-in-catalog")
    msg = str(ei.value)
    assert "evil/not-in-catalog" in msg  # names the rejected repo
    assert ENV_LOCAL_GGUF_MODEL in msg  # points at the override knob


def test_local_summarizer_accepts_catalog_model(reset_available_backends, extra_present):
    s = LocalSummarizer(backend="gguf", model=LOCAL_GGUF_MODEL)
    assert s.model == LOCAL_GGUF_MODEL


def test_local_summarizer_env_override_model_always_allowed(
    reset_available_backends, extra_present, monkeypatch
):
    """The operator's env override is operator-controlled (not external input),
    so an unlisted repo set via it bypasses the catalog allowlist."""
    monkeypatch.setenv(ENV_LOCAL_GGUF_MODEL, "me/custom-gguf")
    s = LocalSummarizer(backend="gguf")  # model resolves from the env override
    assert s.model == "me/custom-gguf"


def test_local_summarizer_injected_fn_bypasses_allowlist(reset_available_backends):
    """The injected-generate_fn unit-test seam skips the allowlist exactly as it
    skips the missing-extra probe — tests can drive any repo id."""
    s = LocalSummarizer(backend="gguf", model="anything/at-all", generate_fn=lambda t, p: "ok")
    assert s.model == "anything/at-all"


def test_load_summarizer_local_rejects_unknown_model(reset_available_backends, extra_present):
    with pytest.raises(SummarizerUnavailable):
        load_summarizer(source="local", model="evil/not-in-catalog")


# ---------------------------------------------------------------------------
# Hardware routing — reuses the catalog's available_backends() probe so there's
# ONE source of truth for "is this an Apple-Silicon-MLX box" (no shadow probe).
# ---------------------------------------------------------------------------


def test_local_summarizer_auto_routes_to_mlx_when_available(reset_available_backends):
    set_available_backends_for_testing(frozenset({"cpu", "mlx"}))
    s = LocalSummarizer(generate_fn=lambda transcript, prompt: "ok")
    assert s.model == LOCAL_MLX_MODEL


def test_local_summarizer_auto_routes_to_gguf_without_mlx(reset_available_backends):
    set_available_backends_for_testing(frozenset({"cpu"}))
    s = LocalSummarizer(generate_fn=lambda transcript, prompt: "ok")
    assert s.model == LOCAL_GGUF_MODEL


def test_local_summarizer_auto_routes_to_gguf_on_cuda(reset_available_backends):
    """CUDA is the GGUF/CPU path too — only Apple-Silicon MLX gets the mlx_lm
    backend; everything else goes through llama_cpp."""
    set_available_backends_for_testing(frozenset({"cpu", "cuda"}))
    s = LocalSummarizer(generate_fn=lambda transcript, prompt: "ok")
    assert s.model == LOCAL_GGUF_MODEL


# ---------------------------------------------------------------------------
# Degrade clearly when the [summarize] extra isn't installed
# ---------------------------------------------------------------------------


def test_local_summarizer_missing_extra_raises_unavailable(reset_available_backends, extra_missing):
    """No injected generate_fn + the backend package missing → a clear
    SummarizerUnavailable (→ 400) naming the extra, raised at construction so it
    fails fast BEFORE the disk read + JobTracker claim — never a mid-request
    crash deep in a lazy import."""
    with pytest.raises(SummarizerUnavailable) as ei:
        LocalSummarizer(backend="gguf")
    msg = str(ei.value).lower()
    assert "summarize" in msg  # points the operator at the extra


def test_local_summarizer_injected_fn_skips_dependency_probe(reset_available_backends, extra_missing):
    """An injected generate_fn means the heavy backend is never imported, so the
    missing-extra probe must NOT fire — this is exactly how the unit tests run on
    a box without the extra."""
    s = LocalSummarizer(backend="gguf", generate_fn=lambda transcript, prompt: "ok")
    assert s.summarize("t", prompt="p").summary == "ok"


# ---------------------------------------------------------------------------
# Degrade clearly when the backend imports but the model won't LOAD — the
# mlx_lm "Received N parameters not in model" weight/arch skew (the one that
# sank gemma-4-e4b on Apple Silicon), plus corrupt downloads / OOM. These must
# map to a clear SummarizerUnavailable (→ 400) naming the model + the override
# env var, never a raw 500 mid-request — and must NOT swallow the distinct
# "install the [summarize] extra" ImportError path.
# ---------------------------------------------------------------------------


def test_local_summarizer_mlx_load_failure_raises_unavailable(
    reset_available_backends, extra_present, monkeypatch
):
    """mlx_lm.load raising (the 'Received 126 parameters not in model' skew) maps
    to SummarizerUnavailable with the model repo + TAPSCRIBE_SUMMARIZE_MLX_MODEL
    override in the message. The failure is lazy (first summarize), so
    construction must still pass."""

    def boom(model_repo, max_tokens):
        raise ValueError(
            "Received 126 parameters not in model: "
            "language_model.model.layers.24.self_attn.k_norm.weight, ..."
        )

    monkeypatch.setattr("tapscribe.summarizers.local._build_mlx_generate", boom)
    s = LocalSummarizer(backend="mlx")  # no generate_fn → builds the (patched) backend
    with pytest.raises(SummarizerUnavailable) as ei:
        s.summarize("the transcript", prompt="Summarize it")
    msg = str(ei.value)
    assert LOCAL_MLX_MODEL in msg  # names the model that failed to load
    assert ENV_LOCAL_MLX_MODEL in msg  # points at the override knob


def test_local_summarizer_gguf_load_failure_raises_unavailable(
    reset_available_backends, extra_present, monkeypatch
):
    """Same graceful mapping for the GGUF backend — any load-time failure
    (corrupt download, OOM, unsupported arch) becomes a clear 400 naming the
    GGUF model + its override env var."""

    def boom(model_repo, gguf_file, *, max_tokens, n_ctx):
        raise RuntimeError("failed to load model from file")

    monkeypatch.setattr("tapscribe.summarizers.local._build_gguf_generate", boom)
    s = LocalSummarizer(backend="gguf")
    with pytest.raises(SummarizerUnavailable) as ei:
        s.summarize("t", prompt="p")
    msg = str(ei.value)
    assert LOCAL_GGUF_MODEL in msg
    assert ENV_LOCAL_GGUF_MODEL in msg


def test_local_summarizer_lazy_import_error_still_names_extra(
    reset_available_backends, extra_present, monkeypatch
):
    """A lazy ImportError (extra vanished after the construction probe) must keep
    steering the operator to the [summarize] extra — the broadened load-failure
    catch must not swallow the install path."""

    def boom(model_repo, max_tokens):
        raise ImportError("No module named 'mlx_lm'")

    monkeypatch.setattr("tapscribe.summarizers.local._build_mlx_generate", boom)
    s = LocalSummarizer(backend="mlx")
    with pytest.raises(SummarizerUnavailable) as ei:
        s.summarize("t", prompt="p")
    assert "summarize" in str(ei.value).lower()  # the [summarize] extra message


# ---------------------------------------------------------------------------
# Factory dispatch — load_summarizer(source="local")
# ---------------------------------------------------------------------------


def test_load_summarizer_dispatches_local_when_extra_present(reset_available_backends, extra_present):
    s = load_summarizer(source="local")
    assert isinstance(s, LocalSummarizer)
    assert s.source == "local"


def test_load_summarizer_local_is_case_insensitive(reset_available_backends, extra_present):
    assert isinstance(load_summarizer(source="LOCAL"), LocalSummarizer)


def test_load_summarizer_local_without_extra_raises_unavailable(reset_available_backends, extra_missing):
    with pytest.raises(SummarizerUnavailable):
        load_summarizer(source="local")


# ---------------------------------------------------------------------------
# Prompt assembly — single user turn (Gemma's chat template rejects a system
# role; folding everything into one user turn keeps both backends happy).
# ---------------------------------------------------------------------------


def test_build_local_messages_single_user_turn_with_prompt_and_transcript():
    msgs = _build_local_messages("the transcript text", "Summarize this")
    assert len(msgs) == 1
    assert msgs[0]["role"] == "user"
    assert "Summarize this" in msgs[0]["content"]
    assert "the transcript text" in msgs[0]["content"]


def test_build_local_messages_blank_prompt_falls_back_to_default():
    msgs = _build_local_messages("t", "   ")
    assert DEFAULT_SUMMARY_PROMPT in msgs[0]["content"]


# ---------------------------------------------------------------------------
# Upstream API smoke tests — only run where the [summarize] extra is installed.
# These catch an upstream rename/restructure (the symbol-rename class of bug)
# the moment a dependency bump lands, before an operator hits the regression at
# request time. The pyproject upper bounds are the primary defence; these are
# the secondary signal. Mirrors test_transformers_parakeet_upstream_contract.
# ---------------------------------------------------------------------------


def test_mlx_lm_upstream_contract():
    """If mlx-lm is installed, the symbols the MLX backend imports lazily must
    exist with the expected shape — `load` returns (model, tokenizer) and
    `generate(model, tokenizer, prompt=…, max_tokens=…)` is the text-gen entry
    point the adapter calls."""
    pytest.importorskip("mlx_lm")
    import inspect

    from mlx_lm import generate, load

    assert callable(load), "mlx_lm.load is the model loader the adapter calls"
    assert callable(generate), "mlx_lm.generate is the text-gen entry point the adapter calls"
    params = set(inspect.signature(generate).parameters)
    assert {"model", "tokenizer"} <= params, (
        f"mlx_lm.generate(model, tokenizer, …) signature changed; saw {sorted(params)}"
    )
    assert "prompt" in params, f"mlx_lm.generate lost the prompt kwarg; saw {sorted(params)}"
    assert "max_tokens" in params, f"mlx_lm.generate lost the max_tokens kwarg; saw {sorted(params)}"


def test_llama_cpp_upstream_contract():
    """If llama-cpp-python is installed, the symbols the GGUF backend imports
    lazily must exist — `Llama.from_pretrained` (HF GGUF download/load) and
    `Llama.create_chat_completion` (the generate entry point)."""
    pytest.importorskip("llama_cpp")

    from llama_cpp import Llama

    assert hasattr(Llama, "from_pretrained"), "Llama.from_pretrained is the GGUF loader the adapter calls"
    assert hasattr(Llama, "create_chat_completion"), (
        "Llama.create_chat_completion is the generate entry point the adapter calls"
    )


# ---------------------------------------------------------------------------
# Known-people hint (name-correction). The hint travels INSIDE the prompt the
# generate seam receives (it owns the chat-template wrapping); a nameless call
# passes the prompt untouched, and the persisted result keeps the operator prompt.
# ---------------------------------------------------------------------------


def test_local_summarizer_injects_names_into_the_generate_prompt(reset_available_backends):
    seen: dict[str, str] = {}

    def spy(transcript: str, prompt: str) -> str:
        seen["prompt"] = prompt
        return "ok"

    LocalSummarizer(backend="gguf", generate_fn=spy).summarize(
        "t", prompt="Summarize", names=["Alice Havso", "Bob Smith"]
    )
    assert "Alice Havso" in seen["prompt"]
    assert "Bob Smith" in seen["prompt"]
    assert "Summarize" in seen["prompt"]  # the operator instruction survives alongside the hint


def test_local_summarizer_no_names_leaves_instruction_hint_free(reset_available_backends):
    """With no names the generate seam receives the resolved operator prompt with
    no hint appended — the model input is byte-for-byte the pre-feature text."""
    seen: dict[str, str] = {}

    def spy(transcript: str, prompt: str) -> str:
        seen["prompt"] = prompt
        return "ok"

    LocalSummarizer(backend="gguf", generate_fn=spy).summarize("t", prompt="Summarize")
    assert seen["prompt"] == "Summarize"


def test_local_summarizer_blank_prompt_with_names_keeps_default_instruction(reset_available_backends):
    """An empty operator prompt + names must still carry the summarize instruction
    (the DEFAULT), not collapse to the hint alone."""
    seen: dict[str, str] = {}

    def spy(transcript: str, prompt: str) -> str:
        seen["prompt"] = prompt
        return "ok"

    LocalSummarizer(backend="gguf", generate_fn=spy).summarize("t", prompt="", names=["Carol Nguyen"])
    assert DEFAULT_SUMMARY_PROMPT in seen["prompt"]
    assert "Carol Nguyen" in seen["prompt"]


def test_local_summarizer_persists_operator_prompt_not_the_hint(reset_available_backends):
    res = LocalSummarizer(backend="gguf", generate_fn=lambda t, p: "ok").summarize(
        "t", prompt="Summarize", names=["Alice Havso"]
    )
    assert res.prompt == "Summarize"
    assert "Alice Havso" not in res.prompt


# ---------------------------------------------------------------------------
# GGUF context window — advertise the EFFECTIVE one, and translate an overflow
# ---------------------------------------------------------------------------
#
# The GGUF/CPU path loads `n_ctx = default_gguf_ctx()` (8192 by default) while
# the catalog row advertises the model's NATIVE 128K window. On every
# non-Apple-Silicon host the dropdown therefore promised "128K ctx" for a run
# that actually had 8192 — and llama-cpp's overflow raises a bare `ValueError`
# from INSIDE `_generate`, outside `_build_generate_fn`'s try, so it reached the
# route as an untyped 500 instead of a domain error with remediation.


def test_gguf_catalog_advertises_the_effective_window_not_the_native_one(
    reset_available_backends, monkeypatch
):
    monkeypatch.setenv("TAPSCRIBE_SUMMARIZE_GGUF_CTX", "8192")
    cat = summary_model_catalog("gguf")
    assert cat["models"], "the gguf catalog must list at least the bundled default"
    for row in cat["models"]:
        assert row["context_tokens"] == 8192


def test_gguf_catalog_window_follows_the_operator_env_knob(reset_available_backends, monkeypatch):
    """Raising TAPSCRIBE_SUMMARIZE_GGUF_CTX is the documented remediation, so
    the dropdown has to reflect it — and must never exceed the model's native
    window either."""
    monkeypatch.setenv("TAPSCRIBE_SUMMARIZE_GGUF_CTX", "32768")
    assert all(row["context_tokens"] == 32768 for row in summary_model_catalog("gguf")["models"])


def test_mlx_catalog_still_reports_the_full_native_window(reset_available_backends, monkeypatch):
    """The MLX path feeds the model its whole prompt (no n_ctx cap), so capping
    its advertised window would be a regression — the 128K rows exist precisely
    for genuinely long meetings."""
    monkeypatch.setenv("TAPSCRIBE_SUMMARIZE_GGUF_CTX", "8192")
    windows = {row["repo_id"]: row["context_tokens"] for row in summary_model_catalog("mlx")["models"]}
    assert max(windows.values()) > 8192


def test_gguf_context_overflow_becomes_a_domain_error_not_a_bare_500(reset_available_backends):
    """llama-cpp raises `ValueError: Requested tokens (N) exceed context window
    of 8192` from inside generation. Unmapped it is a 500; as `SummarizerFailed`
    it is a 502 whose message tells the operator which knob to raise."""

    def _overflows(transcript, prompt):  # noqa: ARG001
        raise ValueError("Requested tokens (11003) exceed context window of 8192")

    s = LocalSummarizer(backend="gguf", generate_fn=_overflows)
    with pytest.raises(SummarizerFailed) as excinfo:
        s.summarize("a very long transcript", prompt="Summarize")
    message = str(excinfo.value)
    assert "TAPSCRIBE_SUMMARIZE_GGUF_CTX" in message
    assert "8192" in message  # the window actually in force


def test_mlx_generation_valueerror_is_not_relabelled_as_a_context_overflow(reset_available_backends):
    """The translation is GGUF-specific — the MLX path has no n_ctx cap, so a
    ValueError there means something else and must not be blamed on the window."""

    def _boom(transcript, prompt):  # noqa: ARG001
        raise ValueError("something else entirely")

    s = LocalSummarizer(backend="mlx", generate_fn=_boom)
    with pytest.raises(ValueError) as excinfo:
        s.summarize("t", prompt="p")
    assert "TAPSCRIBE_SUMMARIZE_GGUF_CTX" not in str(excinfo.value)
