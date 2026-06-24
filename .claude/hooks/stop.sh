#!/bin/bash
# Lint gate: fail the turn if `ruff check` or `ruff format --check`
# finds anything in tapscribe/ or tests/. Exit code 2 with output on
# stderr tells the harness to feed the findings back to Claude so it
# can fix them before ending the turn.
set -euo pipefail

# Don't loop forever — `stop_hook_active` is set when the hook already
# blocked once on this turn, meaning Claude is now reacting to our
# previous output. Let the stop succeed so progress can be made.
payload=$(cat)
if grep -q '"stop_hook_active":[[:space:]]*true' <<<"$payload"; then
  exit 0
fi

# Anchor to the repo root so the lint/typecheck targets (tapscribe/, tests/,
# frontend/) resolve no matter what cwd the harness launches the hook from. A
# Stop hook invoked from a subdirectory (e.g. a git worktree session) would
# otherwise fail `ruff check` with "E902 No such file or directory" and block
# every turn. Fail open if neither anchor is usable — same philosophy as the
# ruff/tsc skips below.
cd "${CLAUDE_PROJECT_DIR:-$(dirname "$0")/../..}" || exit 0

if ! command -v ruff >/dev/null 2>&1; then
  exit 0
fi

if ! ruff_out=$(ruff check tapscribe tests 2>&1); then
  {
    echo "ruff check failed — fix the issues below before ending the turn:"
    echo
    echo "$ruff_out"
  } >&2
  exit 2
fi

if ! fmt_out=$(ruff format --check tapscribe tests 2>&1); then
  {
    echo "ruff format --check found unformatted files — run \`ruff format tapscribe tests\` and re-stage:"
    echo
    echo "$fmt_out"
  } >&2
  exit 2
fi

# Frontend typecheck. Skipped when tsc isn't installed yet (fresh container
# whose session-start `npm install` hasn't finished) so the hook stays
# non-flaky; CI's `frontend-typecheck` job is the source of truth.
if [ -f frontend/package.json ] && [ -x frontend/node_modules/.bin/tsc ]; then
  if ! tsc_out=$(cd frontend && npm run --silent typecheck 2>&1); then
    {
      echo "tsc --noEmit failed — fix the type errors below before ending the turn:"
      echo
      echo "$tsc_out"
    } >&2
    exit 2
  fi
fi
