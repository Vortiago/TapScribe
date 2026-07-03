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
from conftest import make_api_post_stub, py_cmd  # type: ignore[import-not-found]

from tapscribe.summarizers import (
    ApiSummarizer,
    CommandSummarizer,
    SummarizerFailed,
    SummarizerUnavailable,
    SummaryResult,
    load_summarizer,
)
from tapscribe.summarizers.base import (
    DEFAULT_SUMMARY_PROMPT,
    SUMMARY_SYSTEM_FRAMING,
    build_model_input,
    build_names_hint,
    fold_hint,
    resolve_prompt,
)
from tapscribe.summarizers.command import build_command_argv
from tapscribe.summarizers.local import _build_local_messages

# Echoes "<last argv>|<stdin>" so one command proves BOTH the prompt-as-argv and
# the transcript-on-stdin contracts at once. The adapter appends the prompt as
# the LAST positional (after a `--` separator), so sys.argv[-1] is that prompt
# whether or not the interpreter keeps the `--` in argv.
_ECHO_BOTH = py_cmd("import sys; sys.stdout.write(sys.argv[-1] + '|' + sys.stdin.read())")
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


def test_command_summarizer_runs_in_isolated_empty_cwd(tmp_path, monkeypatch):
    """The child runs in a throwaway EMPTY directory, never the recorder's cwd.
    An agentic CLI (Claude Code, OpenCode) discovers project config by walking
    up from its working directory; running from a TapScribe checkout made
    `claude -p` adopt this repo's ruff Stop hook, whose blocked-lint reply — not
    a meeting summary — got saved. Regression for that bug: the probe reports
    its cwd + a directory listing; both must show a fresh, empty, non-recorder
    directory even when the process cwd is a populated checkout."""
    monkeypatch.chdir(tmp_path)
    # A stand-in for the project markers (.claude/, CLAUDE.md) an agentic CLI
    # would latch onto — the child must NOT see it in its working directory.
    (tmp_path / "CLAUDE.md").write_text("marker", encoding="utf-8")
    probe = py_cmd("import os, sys; sys.stdout.write(os.getcwd() + '|' + repr(sorted(os.listdir('.'))))")
    result = CommandSummarizer(probe).summarize("transcript", prompt="")
    child_cwd, listing = result.summary.split("|", 1)
    assert child_cwd != str(tmp_path)  # not the recorder's cwd
    assert listing == "[]"  # a fresh, empty directory — no project to attach to


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
    assert argv == ["claude", "-p", "--tools", "", "--bare", "--", "Sum it"]


def test_build_command_argv_empty_prompt_appends_nothing():
    # No prompt → no trailing positional AND no `--`, so a stdin-only tool sees
    # exactly the template it was given.
    assert build_command_argv("opencode run", "") == ["opencode", "run"]


def test_build_command_argv_separates_prompt_with_double_dash_after_variadic_flag():
    """The `--` end-of-options separator keeps a trailing variadic flag in the
    template (here, Claude Code's `--tools <tools...>`) from swallowing the
    appended prompt as one more tool name — the bug that made the preset's
    prompt vanish. The prompt must be the LAST element, immediately after `--`."""
    argv = build_command_argv('claude -p --tools ""', "Summarize this")
    assert argv == ["claude", "-p", "--tools", "", "--", "Summarize this"]
    assert argv[-2:] == ["--", "Summarize this"]


def test_build_command_argv_empty_template_raises_unavailable():
    with pytest.raises(SummarizerUnavailable):
        build_command_argv("   ", "prompt")


# ---------------------------------------------------------------------------
# Known-people hint — the ONE renderer (base.build_names_hint) folded into every
# adapter's instruction, plus the command adapter's argv injection. The hint is
# model-input only: the persisted SummaryResult.prompt stays the operator's.
# ---------------------------------------------------------------------------


def test_build_names_hint_renders_a_bulleted_reference_block():
    hint = build_names_hint(["Alice Havso", "Bob Smith"])
    assert "- Alice Havso" in hint
    assert "- Bob Smith" in hint
    # Framed as a spelling reference, NOT an attendance claim, so a listed name
    # that never appears can't be hallucinated into the summary as a speaker.
    assert "attendance list" in hint.lower()


def test_build_names_hint_empty_when_no_usable_names():
    assert build_names_hint([]) == ""
    assert build_names_hint(["", "   "]) == ""  # blank-only → empty, not a stray header


def test_resolve_prompt_applies_default_fallback():
    assert resolve_prompt("Summarize it") == "Summarize it"
    assert resolve_prompt("  Summarize it  ") == "Summarize it"  # stripped
    assert resolve_prompt("") == DEFAULT_SUMMARY_PROMPT  # blank → the default
    assert resolve_prompt("   ") == DEFAULT_SUMMARY_PROMPT


def test_fold_hint_composes_base_and_hint():
    # No names → base unchanged (byte-for-byte the pre-feature instruction).
    assert fold_hint("Summarize", []) == "Summarize"
    # Names + base → base, blank line, then the rendered block.
    folded = fold_hint("Summarize", ["Alice Havso"])
    assert folded.startswith("Summarize\n\n")
    assert "- Alice Havso" in folded
    # Names + empty base → the hint alone (no leading blank line).
    hint_only = fold_hint("", ["Alice Havso"])
    assert hint_only.startswith("Known people")
    assert "- Alice Havso" in hint_only
    # A whitespace-only base (the command source folds a RAW, unresolved prompt)
    # counts as empty — the hint alone, not a stray leading blank line.
    assert fold_hint("   ", ["Alice Havso"]).startswith("Known people")


def test_summary_system_framing_and_build_model_input():
    """base.py owns the system-framing string + the transcript-join
    convention (#261) — local.py and api.py compose from these rather than
    each spelling their own copy."""
    assert "meeting-summarisation assistant" in SUMMARY_SYSTEM_FRAMING
    assert build_model_input("INSTR", "TRANSCRIPT") == "INSTR\n\n--- TRANSCRIPT ---\nTRANSCRIPT"


def test_local_and_api_share_the_same_system_framing_no_drift():
    """Pin against the #261 drift: both adapters must compose from base's ONE
    constant, not their own copy that can silently diverge."""
    msgs = _build_local_messages("T", "")
    assert SUMMARY_SYSTEM_FRAMING in msgs[0]["content"]

    rec: list[tuple] = []
    ApiSummarizer(base_url="http://x/v1", model="m", post_fn=make_api_post_stub(rec)).summarize("T", prompt="p")
    _, _, body = rec[0]
    assert body["messages"][0]["content"] == SUMMARY_SYSTEM_FRAMING


def test_command_summarizer_injects_names_into_the_prompt_argv():
    s = CommandSummarizer(_ECHO_BOTH)
    result = s.summarize("the transcript", prompt="Summarize", names=["Alice Havso", "Bob Smith"])
    argv_prompt, stdin = result.summary.split("|", 1)
    # The hint rides in the prompt positional (argv), alongside the operator prompt…
    assert "Alice Havso" in argv_prompt
    assert "Bob Smith" in argv_prompt
    assert "Summarize" in argv_prompt
    # …the transcript stays clean on stdin (unchanged by the hint)…
    assert stdin == "the transcript"
    # …and the PERSISTED prompt is the operator's, never the augmented argv.
    assert result.prompt == "Summarize"
    assert "Alice Havso" not in result.prompt


def test_command_summarizer_no_names_is_byte_for_byte_pre_feature():
    s = CommandSummarizer(_ECHO_BOTH)
    result = s.summarize("body", prompt="Summarize")
    assert result.summary == "Summarize|body"


def test_command_summarizer_names_with_blank_prompt_sends_only_the_hint():
    """An empty operator prompt + names must still pass the hint (the operator's
    CLI keeps its own default instruction), not a stray leading blank line."""
    s = CommandSummarizer(_ECHO_BOTH)
    result = s.summarize("body", prompt="", names=["Carol Nguyen"])
    argv_prompt, _ = result.summary.split("|", 1)
    assert argv_prompt.startswith("Known people")
    assert "Carol Nguyen" in argv_prompt
