"""Summarizer seam — the one-verb protocol, its result, and its domain errors.

The leaf of the `summarizers/` package: everything a caller (or an adapter)
must agree on, with no dependency on any concrete source. A `Summarizer` is
"something that can summarize one transcript given a prompt" (one verb, like
`Transcriber.transcribe`); the adapters (`command`, `local`, future `api`) and
the `load_summarizer` factory in `__init__` sit on top of this.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

# The default summarization prompt. Lives here so the backend fallback and the
# dashboard's prompt placeholder share ONE source of truth (the view seeds the
# same string; the route falls back to it when the body omits a prompt).
DEFAULT_SUMMARY_PROMPT = "Summarize this meeting into decisions, action items, and open questions."


# ---------------------------------------------------------------------------
# Instruction assembly — the ONE owner of the prompt fallback + hint-fold
# conventions, shared by the command / local / api adapters so none of them
# re-derive the `... or DEFAULT` fallback or the `\n\n` hint separator.
# ---------------------------------------------------------------------------


def resolve_prompt(prompt: str) -> str:
    """The operator prompt with the blank-string fallback applied: an empty or
    whitespace-only prompt becomes `DEFAULT_SUMMARY_PROMPT`. One owner for that
    `(prompt or "").strip() or DEFAULT_SUMMARY_PROMPT` idiom so the adapters (and
    `local._build_local_messages`) can't drift on it."""
    return (prompt or "").strip() or DEFAULT_SUMMARY_PROMPT


def build_names_hint(names: Sequence[str]) -> str:
    """Render the known-people correction hint, or "" when there are no names.

    The names are the operator-chosen People Registry names — this session's
    participants first, then people the registry has learned across previous
    meetings (see `tapscribe.name_resolution.known_names`). The recorder's
    merged transcript labels each line with a lossy, truncated speaker slug, and
    the ASR mangles spoken names; giving the model the canonical spellings lets
    it map both back to the right person.

    Lives here (the summarizers leaf, next to `DEFAULT_SUMMARY_PROMPT`) so the
    command / local / api adapters share ONE block — a nameless summarize
    renders "" and every adapter's model input is byte-for-byte the pre-feature
    text. Blanks are dropped; ordering + dedup are the caller's job (the policy
    owner, `known_names`). The framing is deliberately defensive: it tells the
    model the list is a spelling dictionary, NOT an attendance claim, so a name
    that never appears can't be hallucinated into the summary."""
    lines = "\n".join(f"- {s}" for n in names if (s := (n or "").strip()))
    if not lines:
        return ""
    return (
        "Known people (from this meeting and previous meetings). Speaker labels "
        "and spoken names in the transcript may be mis-transcribed or misspelled; "
        "when a name closely matches one below, use the spelling below. This is a "
        "reference list, not an attendance list — do not assume a listed person "
        "spoke or was present:\n"
        f"{lines}"
    )


# The system framing every adapter that talks to a chat-completions-style model
# (local, api) prepends to the operator's instruction — folded into local's
# single user turn (Gemma's chat template rejects a system role) or api's real
# system message. ONE owner so the two sources can't drift on the wording.
SUMMARY_SYSTEM_FRAMING = (
    "You are a meeting-summarisation assistant. Read the transcript and produce a clear, "
    "well-structured summary. Output only the summary."
)


def build_model_input(instruction: str, transcript: str) -> str:
    """Join an instruction with the transcript via the ONE `--- TRANSCRIPT ---`
    separator convention, shared by every adapter that folds the transcript
    into the model input — the join half of the drift #261 fixed (the framing
    string above is the other half)."""
    return f"{instruction}\n\n--- TRANSCRIPT ---\n{transcript}"


def fold_hint(base: str, names: Sequence[str]) -> str:
    """Compose a model instruction from a `base` instruction and the known-people
    hint for `names` — the ONE owner of the hint-join convention so the three
    adapters don't each re-spell it (and can't drift on the `\\n\\n` separator):

      - no usable names   → `base` unchanged (a nameless summarize is byte-for-
                            byte the pre-feature instruction)
      - names, empty base → the hint alone (the command source's CLI keeps its
                            own default instruction, so there's nothing to prepend)
      - names, with base  → `base`, a blank line, then the hint block

    The hint is guidance (a spelling reference — see `build_names_hint`), which
    every caller keeps ABOVE the transcript rather than inside it. A blank/
    whitespace-only `base` counts as empty (the command source folds its RAW,
    unresolved prompt, so a whitespace prompt yields the hint alone rather than a
    stray leading blank line)."""
    hint = build_names_hint(names)
    if not hint:
        return base
    return f"{base}\n\n{hint}" if base.strip() else hint


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

    def summarize(self, transcript: str, *, prompt: str, names: Sequence[str] = ()) -> SummaryResult: ...
