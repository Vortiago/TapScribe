"""Command adapter — summarize by piping the transcript to a CLI tool.

The tracer-bullet source (#82): no network, no heavy dependency, which is why it
proved the whole vertical seam (protocol → Batch-summarize orchestrator → route
→ wired view) first. Security: argv is built in **list form** (`shlex.split` of
the operator's template, with the prompt appended as a trailing positional) and
run with `shell=False` — never an f-string, never `shell=True` — so a transcript
or prompt can't break out into a shell. Same subprocess-argv discipline as
`tapscribe.live.build_live_cmd`.
"""

from __future__ import annotations

import shlex
import subprocess
from datetime import UTC, datetime

from .. import config
from .base import SummarizerFailed, SummarizerUnavailable, SummaryResult

# Operator knob for the per-summarize subprocess timeout, hoisted to a module
# constant so the dashboard wiring + docs have one source of truth — same
# convention as the MLX chunk-size knobs on the transcriber adapters.
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
