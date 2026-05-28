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
# Bypass: set CLAUDE_SKIP_PRE_PUSH=1 in the same command when you
# genuinely need to push without the gate (mid-debug branch reset, etc.).
# That's an explicit, audible escape — not a silent one.
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

# Match `git push` only as a bare command token (boundary on either
# side), so a literal `echo "git push"` or `git push-mirror` doesn't
# trip the gate. The regex also handles chained commands
# (`git status && git push origin`).
if ! grep -Eq '(^|[[:space:];&|()])git[[:space:]]+push([[:space:]]|$)' <<<"$cmd"; then
    exit 0
fi

if [ "${CLAUDE_SKIP_PRE_PUSH:-}" = "1" ]; then
    echo "[pre-push] CLAUDE_SKIP_PRE_PUSH=1 set — gate bypassed by explicit override." >&2
    exit 0
fi

cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)"

block() {
    echo "" >&2
    echo "[pre-push] BLOCKED — fix the issue above before pushing, or rerun with" >&2
    echo "           CLAUDE_SKIP_PRE_PUSH=1 prefixed if a deliberate red push is" >&2
    echo "           genuinely needed (e.g. mid-debug branch reset)." >&2
    exit 2
}

# Mirror ci.yml's `Lint (ruff)` step. The repo's stop hook also runs
# this at end-of-turn, but a single Stop event can be racey when Claude
# pushes inside the same turn it wrote the code — gate again here.
if ! command -v ruff >/dev/null 2>&1; then
    echo "[pre-push] BLOCKED — ruff not on PATH; install dev deps (see" >&2
    echo "           .claude/hooks/session-start.sh) before pushing." >&2
    block
fi

echo "[pre-push] ruff check (CI parity)…" >&2
if ! ruff check tapscribe tools tests bridges/local-test-bridge >&2; then
    block
fi

# NB: deliberately NOT running `ruff format --check` here — CI's lint
# step only runs `ruff check`, and the Stop hook already enforces format
# on tapscribe/ + tests/ at end-of-turn. Adding it here would block
# pushes on pre-existing format drift in tools/ that CI ignores.

# Mirror ci.yml's `Run tests` step. Coverage is omitted — it doesn't
# affect pass/fail and costs ~5s we don't want on every push. The
# real_pip e2e test self-skips when its prerequisites are missing, so
# including all of tests/ is safe even in a barebones env.
if ! python3 -c "import pytest" 2>/dev/null; then
    echo "[pre-push] BLOCKED — pytest not importable. Install test deps" >&2
    echo "           (see .claude/hooks/session-start.sh) before pushing." >&2
    block
fi

echo "[pre-push] pytest tests (CI parity; usually ~30-60s)…" >&2
if ! python3 -m pytest tests >&2; then
    block
fi

echo "[pre-push] OK — lint + tests green. Push allowed." >&2
exit 0
