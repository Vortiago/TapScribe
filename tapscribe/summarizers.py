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

import shlex
import subprocess
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
# Factory — dispatch on source
# ---------------------------------------------------------------------------


def load_summarizer(*, source: str, command: str = "", timeout_s: float | None = None) -> Summarizer:
    """Return a `Summarizer` for the chosen source, dispatching on `source`
    exactly as `load_transcriber` resolves a backend.

    Today only the `command` source is implemented; `api` (#85) and `local`
    (#86) raise `SummarizerUnavailable` until their slices land, so the view can
    show those options disabled and a stray request fails with a clear 400."""
    src = (source or "").strip().lower()
    if src == "command":
        return CommandSummarizer(command, timeout_s=timeout_s)
    if src in ("api", "local"):
        raise SummarizerUnavailable(f"the {src!r} summarizer source isn't wired yet")
    raise SummarizerUnavailable(f"unknown summarizer source: {source!r}")
