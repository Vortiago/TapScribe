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
