#!/bin/bash
# Pre-push verification gate.
#
# Fires on every Bash tool call via PreToolUse. When the command Claude
# is about to run actually invokes `git push`, we re-run the same checks
# CI runs (ruff lint, ruff format --check, pytest) and block the push
# with exit code 2 if anything is red — so Claude is forced to react to
# the failure instead of shipping it.
#
# Anything that isn't a `git push` passes through with exit 0, so the
# hook adds zero overhead to the rest of the session.
#
# Bypass: prefix the push with CLAUDE_SKIP_PRE_PUSH=1 when you genuinely
# need to push without the gate (mid-debug branch reset, etc.). That's an
# explicit, audible escape — not a silent one: it is written in the command
# the user sees, and the hook says so on stderr when it fires.
set -uo pipefail

payload=$(cat 2>/dev/null || true)

# Pull the Bash command out of the PreToolUse JSON payload via stdlib
# python — we run before the test deps are installed, so jq / yq are
# not assumed to exist.
cmd=$(printf '%s' "$payload" | python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    print("")
    sys.exit(0)
print((data.get("tool_input") or {}).get("command", ""))
' 2>/dev/null || echo "")

# Match `git push` only as a bare command token (boundary on either side), so
# `git push-mirror` doesn't trip the gate, and handle chained commands
# (`git status && git push origin`).
#
# It is a text match, not a parse: a mention inside a string — `echo "git push
# later"` — does trip it. That is the safe direction to be wrong in (the gate
# runs when it needn't, costing one green run) and the alternative is parsing
# shell quoting here, so it stays. Don't "fix" it by loosening the pattern.
if ! grep -Eq '(^|[[:space:];&|()])git[[:space:]]+push([[:space:]]|$)' <<<"$cmd"; then
    exit 0
fi

# The documented escape is a command PREFIX, and a prefix never reaches this
# process: the hook is spawned by Claude Code and inherits ITS environment, so
# the env read below can only have been set by whoever launched the session.
# So the header promised an escape that silently did nothing. The command text
# is where a prefix actually is, so match there too.
#
# It must be the env prefix OF THE PUSH, not merely present in the command:
# matching it anywhere let `echo CLAUDE_SKIP_PRE_PUSH=1 && git push` open the
# gate, which is a bypass nobody wrote on purpose and a reader skimming the
# line would not see. Hence the `git push` in the pattern, and the optional run
# of further assignments between, so a second env var in the prefix still
# matches.
skip_prefix='(^|[[:space:];&|()])CLAUDE_SKIP_PRE_PUSH=1[[:space:]]+([A-Za-z_][A-Za-z0-9_]*=[^[:space:]]*[[:space:]]+)*git[[:space:]]+push([[:space:]]|$)'
if [ "${CLAUDE_SKIP_PRE_PUSH:-}" = "1" ] || grep -Eq "$skip_prefix" <<<"$cmd"; then
    echo "[pre-push] CLAUDE_SKIP_PRE_PUSH=1 set — gate bypassed by explicit override." >&2
    exit 0
fi

repo=$(git rev-parse --show-toplevel 2>/dev/null || pwd)
cd "$repo"

block() {
    echo "" >&2
    echo "[pre-push] BLOCKED — fix the issue above before pushing, or rerun with" >&2
    echo "           CLAUDE_SKIP_PRE_PUSH=1 prefixed if a deliberate red push is" >&2
    echo "           genuinely needed (e.g. mid-debug branch reset)." >&2
    exit 2
}

# Where the dev tools are, which is NOT the same question on the two machines
# this hook runs on. session-start.sh pip-installs onto PATH on the web runner;
# a local checkout keeps its dev deps in a repo virtualenv instead, and PATH's
# python3 is then the system one. A bare `command -v ruff` therefore reported
# "not installed" on a box that had ruff sitting in .venv/ the whole time, and
# the gate blocked every push on a repo that was green — with no way to correct
# it from a command prefix, since the hook's PATH is the session's, not the
# shell's. So: PATH first, then the venv, in both layouts (Scripts/ on Windows,
# bin/ on POSIX) and with the .exe suffix.
#
# `command -v` accepts an absolute path and answers for it, so one loop covers
# both cases without a separate is-it-a-path branch.
ruff=""
for candidate in ruff "$repo/.venv/Scripts/ruff.exe" "$repo/.venv/bin/ruff"; do
    if command -v "$candidate" >/dev/null 2>&1; then
        ruff="$candidate"
        break
    fi
done

# The interpreter that can actually RUN the suite, which is a different question
# from which python3 comes first: on a local checkout that one is the system
# interpreter and pytest lives in the venv. So the import probe IS the
# resolution — take the first candidate that can import pytest, rather than
# taking one and then reporting the other's absence.
py=""
for candidate in python3 python "$repo/.venv/Scripts/python.exe" "$repo/.venv/bin/python3" "$repo/.venv/bin/python"; do
    if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c "import pytest" 2>/dev/null; then
        py="$candidate"
        break
    fi
done

# Mirror ci.yml's `Lint (ruff)` step. The repo's stop hook also runs
# this at end-of-turn, but a single Stop event can be racey when Claude
# pushes inside the same turn it wrote the code — gate again here.
if [ -z "$ruff" ]; then
    echo "[pre-push] BLOCKED — no ruff on PATH or in $repo/.venv; install dev" >&2
    echo "           deps (see .claude/hooks/session-start.sh) before pushing." >&2
    block
fi

echo "[pre-push] ruff check (CI parity)…" >&2
if ! "$ruff" check tapscribe tools tests benchmarks bridges/local-test-bridge >&2; then
    block
fi

# Mirror ci.yml's `Format check (ruff)` step — same paths as the lint
# step. CI fails unformatted Python, so pushing it just moves the red
# from the terminal to the PR (this gap shipped four red PRs at once
# when parallel agents relied on this hook as their CI mirror).
echo "[pre-push] ruff format --check (CI parity)…" >&2
if ! "$ruff" format --check tapscribe tools tests benchmarks bridges/local-test-bridge >&2; then
    block
fi

# Mirror ci.yml's `Run tests` step. Coverage is omitted — it doesn't
# affect pass/fail and costs ~5s we don't want on every push. The
# real_pip e2e test self-skips when its prerequisites are missing, so
# including all of tests/ is safe even in a barebones env.
#
# `-m "not real_audio"` keeps that mirror honest on a developer box. CI's
# `tests` job installs no ASR extra, so the real_audio tests self-skip
# there; they get their own job with its own narrower deps. Without the
# marker a machine that HAS the extras runs a heavier suite than CI ever
# does, and pays minutes of real transcription on every push.
if [ -z "$py" ]; then
    echo "[pre-push] BLOCKED — no interpreter on PATH or in $repo/.venv can import" >&2
    echo "           pytest. Install test deps (see .claude/hooks/session-start.sh)" >&2
    echo "           before pushing." >&2
    block
fi

echo "[pre-push] pytest tests (CI parity; usually ~30-60s)…" >&2
if ! "$py" -m pytest tests -m "not real_audio" >&2; then
    block
fi

echo "[pre-push] OK — lint + tests green. Push allowed." >&2
exit 0
