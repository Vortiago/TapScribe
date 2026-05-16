#!/bin/bash
# PostToolUse hook: run the spacialchat-bridge Node test suite after any
# Edit/Write/MultiEdit under bridges/spacialchat-bridge/. Tests are plain
# `node --test` against the source — no `npm install` needed. Runs in ~2 s.
#
# Exit 2 surfaces stderr back to Claude and blocks the tool result so a
# broken bridge test gets fixed before the turn continues. Mirrors the CI
# `bridge-js` job in .github/workflows/ci.yml so a green hook implies a
# green CI run for the bridge.
set -euo pipefail

payload=$(cat)

# tool_input.file_path is set by PostToolUse for Edit / Write / MultiEdit.
# Bail silently for any other tool or missing field.
file_path=$(printf '%s' "$payload" | python3 -c 'import json,sys
try:
    d = json.load(sys.stdin)
    print(d.get("tool_input", {}).get("file_path", ""))
except Exception:
    pass
' 2>/dev/null)

case "$file_path" in
  */bridges/spacialchat-bridge/*) ;;
  *) exit 0 ;;
esac

if ! command -v node >/dev/null 2>&1; then
  exit 0
fi

cd "$CLAUDE_PROJECT_DIR" || exit 0

if ! out=$(node --test "bridges/spacialchat-bridge/tests/"*.test.js 2>&1); then
  {
    echo "bridge node --test failed — fix before continuing:"
    echo
    printf '%s\n' "$out" | tail -40
  } >&2
  exit 2
fi
