"""Command adapter — summarize by piping the transcript to a CLI tool.

The tracer-bullet source (#82): no network, no heavy dependency, which is why it
proved the whole vertical seam (protocol → Batch-summarize orchestrator → route
→ wired view) first. Security: argv is built in **list form** (`shlex.split` of
the operator's template, with the prompt appended as a trailing positional after
a `--` end-of-options separator) and run with `shell=False` — never an f-string,
never `shell=True` — so a transcript or prompt can't break out into a shell. Same
subprocess-argv discipline as `tapscribe.live.build_live_cmd`. The `--` keeps a
trailing variadic flag in the template (e.g. `--tools <tools...>`) from eating
the prompt — see `build_command_argv`.

Why the transcript travels on **stdin** (not argv, not a file path): argv would
hit ARG_MAX and leak meeting content into `ps`; a file reference wouldn't
reduce prompt injection either (the model reads the same bytes regardless of
transport) while REQUIRING the tool to have file-read capability enabled —
stdin is the least-capability delivery. The injection exposure that actually
matters is the tool's own tool use (an injected transcript asking an agentic
CLI to read files / fetch URLs and exfiltrate into the summary); that
mitigation lives in `catalog.COMMAND_PRESETS`, whose templates ship hardened
flags (e.g. Claude Code with tools disabled). Don't "fix" injection by moving
the transcript to a file.
"""

from __future__ import annotations

import shlex
import subprocess
import tempfile
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


def build_command_argv(command: str, prompt: str) -> list[str]:
    """List-form argv for one summarize call: the operator's shell-style
    template split by `shlex`, with the prompt appended as ONE trailing
    positional after a ``--`` end-of-options separator (only when non-empty, so
    a stdin-only tool sees exactly the transcript). Pure — no subprocess spawn —
    the same testable-builder shape as `tapscribe.live.build_live_cmd`. Raises
    `SummarizerUnavailable` on an empty template.

    The ``--`` matters: a template whose last flag is *variadic* (e.g. Claude
    Code's ``--tools <tools...>``) would otherwise swallow the appended prompt
    as one more value of that flag, and the tool would run with no instruction.
    ``--`` is the POSIX end-of-options marker, honoured by argv parsers on both
    macOS and Windows (it's parsed by the tool, not the shell — and we never use
    a shell), so the prompt always lands as a positional regardless of what
    precedes it."""
    argv = shlex.split(command or "")
    if not argv:
        raise SummarizerUnavailable("the command source needs a non-empty command template")
    if prompt:
        argv += ["--", prompt]
    return argv


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
        # Validate the template eagerly (empty → Unavailable) by building a
        # promptless argv; summarize() rebuilds with the real prompt.
        self._argv = build_command_argv(command, "")
        self.command = (command or "").strip()
        self._timeout_s = _default_timeout_s() if timeout_s is None else timeout_s

    def summarize(self, transcript: str, *, prompt: str) -> SummaryResult:
        # List-form argv only (the operator template parsed by shlex + the
        # prompt as a trailing positional). Never interpolated into a shell
        # string — see the class docstring.
        argv = build_command_argv(self.command, prompt)
        started = datetime.now(UTC)
        try:
            # Run in a throwaway EMPTY directory, never the recorder's cwd. An
            # agentic CLI (Claude Code, OpenCode) discovers *project* config by
            # walking up from its working directory — settings, hooks,
            # CLAUDE.md/AGENTS.md. The recorder is normally launched from a
            # TapScribe checkout, so summarizing from that cwd made `claude -p`
            # adopt this repo's Stop hook (the ruff lint gate): the hook blocked
            # on an unrelated unformatted file, fed the lint failure back, and
            # with tool use disabled the model's REPLY about the lint error —
            # not a meeting summary — is what landed on stdout and got saved.
            # A fresh temp dir has no project to attach to, so the tool sees
            # only the transcript on stdin and the prompt on argv. This is the
            # tool-agnostic layer (it protects OpenCode too); the Claude preset
            # adds `--setting-sources user` on top so project/local settings
            # never load even if the tool were reached from inside a checkout by
            # some other path — see catalog.COMMAND_PRESETS. (run() has already
            # killed + reaped the child before any except fires, so the dir
            # cleans up cleanly even on timeout.)
            with tempfile.TemporaryDirectory(prefix="tapscribe-summarize-") as isolated_cwd:
                proc = subprocess.run(
                    argv,
                    input=transcript.encode("utf-8"),
                    capture_output=True,
                    timeout=self._timeout_s,
                    check=False,
                    cwd=isolated_cwd,
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
            # Some CLIs write their failure REASON to stdout, not stderr — e.g.
            # `claude -p` prints "Not logged in · Please run /login" to stdout
            # and exits 1. Reading stderr alone yields a bare "command exited 1"
            # and the dashboard surfaces an opaque 502 with no clue what broke.
            # Prefer stderr; fall back to a bounded stdout snippet so the real
            # reason reaches the operator. Cap the length so a chatty tool can't
            # blow up the error payload.
            diag = stderr or proc.stdout.decode("utf-8", "replace").strip()
            detail = f" — {diag[:500]}" if diag else ""
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
