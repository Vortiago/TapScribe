"""Direct tests for the Local summarizer source (tapscribe.summarizers.LocalSummarizer).

The Local source is the bundled, offline, hardware-routed summarizer (#86): an
MLX backend (`mlx_lm`) on Apple Silicon, a GGUF/CPU backend (`llama_cpp`)
elsewhere. The heavy backends are lazy-imported, so these tests drive the
adapter through an **injected `generate_fn`** — the same testability seam the
MLX transcriber adapters use (`transcribe_fn`) — and never import mlx_lm /
llama_cpp or download a 4.5 GB model. The routing decision is exercised by
forcing `available_backends()` via the catalog's test hook.

The two `importorskip`-gated smoke tests at the bottom pin the upstream symbols
the real backends import lazily (`mlx_lm.load`/`generate`,
`llama_cpp.Llama.from_pretrained`/`create_chat_completion`). They no-op on hosts
where the `[summarize]` extra isn't installed (the whole Linux CI matrix) and
run in full wherever the package is present — the same convention as
`test_mlx_audio_canary_upstream_contract`.
"""

from __future__ import annotations

import pytest

from tapscribe.summarizers import (
    DEFAULT_SUMMARY_PROMPT,
    LOCAL_GGUF_MODEL,
    LOCAL_MLX_MODEL,
    LocalSummarizer,
    SummarizerFailed,
    SummarizerUnavailable,
    SummaryResult,
    _build_local_messages,
    load_summarizer,
)
from tapscribe.transcribers.catalog import set_available_backends_for_testing


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
    monkeypatch.setattr("tapscribe.summarizers._backend_module_available", lambda backend: True)


@pytest.fixture
def extra_missing(monkeypatch):
    """Pretend the `[summarize]` backend module is NOT importable — the fresh-box
    case the runtime-deps step is meant to fix, and the 'degrade clearly' path."""
    monkeypatch.setattr("tapscribe.summarizers._backend_module_available", lambda backend: False)


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
# These catch an upstream rename/restructure (the mlx-audio `Canary`→`Model`
# class of bug) the moment a dependency bump lands, before an operator hits the
# regression at request time. The pyproject upper bounds are the primary
# defence; these are the secondary signal. Mirrors
# test_mlx_audio_canary_upstream_contract.
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
