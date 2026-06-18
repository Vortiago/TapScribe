"""Direct tests for tapscribe.summarizers — the Summarizer seam.

These drive `CommandSummarizer` against REAL, deterministic subprocesses and
assert what comes out, so they survive a refactor of the implementation. The
commands are built from `sys.executable` (the running interpreter) rather than
`echo`/`cat` so the suite is identical on Linux, macOS, AND Windows — `cat` and
co. aren't on PATH as executables on Windows, but `python -c …` always is.

The two channels under test:
- the **prompt** reaches the command as a trailing argv element (list-form),
- the **transcript** reaches the command on stdin, and stdout becomes the
  summary.

`load_summarizer`'s source dispatch is asserted directly.
"""

from __future__ import annotations

import pytest
from conftest import py_cmd  # type: ignore[import-not-found]

from tapscribe.summarizers import (
    ApiSummarizer,
    CommandSummarizer,
    SummarizerFailed,
    SummarizerUnavailable,
    SummaryResult,
    load_summarizer,
)
from tapscribe.summarizers.command import build_command_argv

# Echoes "<argv[1]>|<stdin>" so one command proves BOTH the prompt-as-argv and
# the transcript-on-stdin contracts at once. `python -c` puts the trailing args
# at sys.argv[1:], so sys.argv[1] is the prompt the adapter appended.
_ECHO_BOTH = py_cmd("import sys; sys.stdout.write(sys.argv[1] + '|' + sys.stdin.read())")
# cat-equivalent: stdin straight to stdout (used with an empty prompt → no
# trailing arg, so it's a clean transcript-on-stdin probe).
_CAT = py_cmd("import sys; sys.stdout.write(sys.stdin.read())")


def test_command_summarizer_pipes_transcript_to_stdin_and_prompt_as_argv():
    s = CommandSummarizer(_ECHO_BOTH)
    result = s.summarize("the merged transcript text", prompt="Summarize please")
    assert isinstance(result, SummaryResult)
    assert result.summary == "Summarize please|the merged transcript text"
    assert result.source == "command"
    assert result.prompt == "Summarize please"
    assert result.command == _ECHO_BOTH
    assert result.took_ms >= 0
    assert result.created_at  # ISO timestamp recorded


def test_command_summarizer_empty_prompt_appends_no_argv():
    """An empty prompt must NOT append a trailing argv element — so a stdin-only
    tool (here, cat) sees exactly the transcript and nothing else."""
    s = CommandSummarizer(_CAT)
    result = s.summarize("only the transcript on stdin", prompt="")
    assert result.summary == "only the transcript on stdin"


def test_command_summarizer_argv_is_list_form_not_shell():
    """Shell metacharacters in the transcript/prompt stay inert — they reach
    the child as literal stdin/argv bytes, never interpreted by a shell. If the
    adapter shelled out, `; touch …` would run; here it round-trips verbatim."""
    s = CommandSummarizer(_CAT)
    injection = "innocent; rm -rf / && echo pwned `whoami`"
    result = s.summarize(injection, prompt="")
    assert result.summary == injection


def test_command_summarizer_nonzero_exit_raises_failed_with_stderr():
    cmd = py_cmd("import sys; sys.stderr.write('boom detail'); sys.exit(2)")
    with pytest.raises(SummarizerFailed) as ei:
        CommandSummarizer(cmd).summarize("x", prompt="")
    msg = str(ei.value)
    assert "2" in msg and "boom detail" in msg


def test_command_summarizer_nonzero_exit_falls_back_to_stdout_when_stderr_empty():
    """A CLI that prints its failure reason to STDOUT (not stderr) and exits
    non-zero must still surface that reason — else the operator gets a bare
    'command exited N' and an opaque 502. `claude -p` does exactly this with its
    'Not logged in' auth prompt; reading stderr alone would lose it."""
    cmd = py_cmd("import sys; sys.stdout.write('Not logged in - run /login'); sys.exit(1)")
    with pytest.raises(SummarizerFailed) as ei:
        CommandSummarizer(cmd).summarize("x", prompt="")
    msg = str(ei.value)
    assert "exited 1" in msg and "Not logged in" in msg


def test_command_summarizer_empty_output_raises_failed():
    """Exit 0 but nothing on stdout is a useless summary — treat it as a
    failure so the operator isn't handed a blank panel."""
    with pytest.raises(SummarizerFailed):
        CommandSummarizer(py_cmd("pass")).summarize("x", prompt="")


def test_command_summarizer_timeout_raises_failed():
    slow = py_cmd("import time; time.sleep(5)")
    with pytest.raises(SummarizerFailed) as ei:
        CommandSummarizer(slow, timeout_s=0.3).summarize("x", prompt="")
    assert "timed out" in str(ei.value)


def test_command_summarizer_missing_executable_raises_unavailable():
    with pytest.raises(SummarizerUnavailable):
        CommandSummarizer("definitely-not-a-real-binary-xyzzy").summarize("x", prompt="")


def test_command_summarizer_empty_template_raises_unavailable():
    for blank in ("", "   ", "\t"):
        with pytest.raises(SummarizerUnavailable):
            CommandSummarizer(blank)


def test_load_summarizer_dispatches_command_source():
    s = load_summarizer(source="command", command=_CAT)
    assert isinstance(s, CommandSummarizer)
    assert s.source == "command"


def test_load_summarizer_command_source_is_case_insensitive():
    assert isinstance(load_summarizer(source="Command", command=_CAT), CommandSummarizer)


# `local` is no longer here — it's wired in #86 (see tests/test_summarizers_local.py,
# where its missing-extra → Unavailable path is forced deterministically). `api`
# is now wired (#85) and tested against a stub post_fn in test_summarizers_api.py.


def test_load_summarizer_api_source_returns_api_summarizer():
    s = load_summarizer(source="api", base_url="http://h:1/v1")
    assert isinstance(s, ApiSummarizer)
    assert s.source == "api"


def test_load_summarizer_api_source_empty_base_url_raises_unavailable():
    with pytest.raises(SummarizerUnavailable):
        load_summarizer(source="api", base_url="")


def test_load_summarizer_unknown_source_raises_unavailable():
    with pytest.raises(SummarizerUnavailable):
        load_summarizer(source="telepathy", command="")


def test_load_summarizer_command_source_empty_command_raises_unavailable():
    with pytest.raises(SummarizerUnavailable):
        load_summarizer(source="command", command="")


# ---------------------------------------------------------------------------
# build_command_argv — the pure argv builder (same shape as build_live_cmd)
# ---------------------------------------------------------------------------


def test_build_command_argv_splits_template_and_appends_prompt():
    argv = build_command_argv('claude -p --tools "" --bare', "Sum it")
    assert argv == ["claude", "-p", "--tools", "", "--bare", "Sum it"]


def test_build_command_argv_empty_prompt_appends_nothing():
    assert build_command_argv("opencode run", "") == ["opencode", "run"]


def test_build_command_argv_empty_template_raises_unavailable():
    with pytest.raises(SummarizerUnavailable):
        build_command_argv("   ", "prompt")
