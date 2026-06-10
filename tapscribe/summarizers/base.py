"""Summarizer seam — the one-verb protocol, its result, and its domain errors.

The leaf of the `summarizers/` package: everything a caller (or an adapter)
must agree on, with no dependency on any concrete source. A `Summarizer` is
"something that can summarize one transcript given a prompt" (one verb, like
`Transcriber.transcribe`); the adapters (`command`, `local`, future `api`) and
the `load_summarizer` factory in `__init__` sit on top of this.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

# The default summarization prompt. Lives here so the backend fallback and the
# dashboard's prompt placeholder share ONE source of truth (the view seeds the
# same string; the route falls back to it when the body omits a prompt).
DEFAULT_SUMMARY_PROMPT = "Summarize this meeting into decisions, action items, and open questions."


# ---------------------------------------------------------------------------
# Domain errors — the route layer maps these to HTTP codes
# ---------------------------------------------------------------------------


class SummarizerError(Exception):
    """Base class for every summarizer-adapter domain error."""


class SummarizerUnavailable(SummarizerError):
    """The chosen source is misconfigured or not wired yet — an empty command
    template, a command whose executable can't be found, or the `api` source
    before its slice lands. The operator must fix the configuration; routes map
    this to 400."""


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
    argv) or hold a client/model (LocalSummarizer / the future API adapter)."""

    source: str

    def summarize(self, transcript: str, *, prompt: str) -> SummaryResult: ...
