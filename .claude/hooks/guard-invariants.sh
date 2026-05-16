#!/bin/bash
# PreToolUse hook: refuse to edit the load-bearing invariant files (see
# CONTEXT.md "Invariants" section) and the ADRs unless the user prompt
# carries an explicit ACK-INVARIANT token. The intent is to make accidental
# "cleanup" of WS-lifecycle code a deliberate choice, not a silent one.
#
# Guarded paths:
#   tapscribe/tap_fan_out.py    — fan-out lifecycle / utterance bookkeeping
#   tapscribe/live_relay.py     — WlK relay drain + tail flush
#   tapscribe/live.py           — live channel supervisor
#   docs/adr/*.md               — architectural decisions
#
# Override: include `ACK-INVARIANT` anywhere in the most recent user prompt.
# Exit-code contract (Claude Code hooks docs):
#   exit 0 — allow the tool call
#   exit 2 — block; stderr is surfaced to Claude as the refusal reason
set -euo pipefail

payload=$(cat)

# Extract tool_input.file_path and the last user prompt from the hook
# payload. Falls back to env vars if the JSON shape ever changes.
read -r file_path user_prompt <<<"$(printf '%s' "$payload" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
    fp = d.get("tool_input", {}).get("file_path", "") or ""
    # user_prompt is exposed on PreToolUse via context; harness versions
    # vary, so we check a few likely shapes.
    up = d.get("user_prompt", "") or d.get("prompt", "") or ""
    # Newlines would break our `read` split; collapse to spaces.
    up = " ".join(up.split())
    print(fp, up)
except Exception:
    print("", "")
' 2>/dev/null)"

# If we cannot determine the file path, do not block.
if [[ -z "$file_path" ]]; then
  exit 0
fi

case "$file_path" in
  */tapscribe/tap_fan_out.py|*/tapscribe/live_relay.py|*/tapscribe/live.py) ;;
  */docs/adr/*.md) ;;
  *) exit 0 ;;
esac

ack="${user_prompt}${CLAUDE_PROMPT:-}${CLAUDE_USER_PROMPT:-}"
if [[ "$ack" == *ACK-INVARIANT* ]]; then
  exit 0
fi

{
  echo "Refusing edit to invariant-bearing file: $file_path"
  echo
  echo "Files under tapscribe/tap_fan_out.py, tapscribe/live_relay.py,"
  echo "tapscribe/live.py, and docs/adr/*.md encode the CONTEXT.md"
  echo "invariants (one /tap WS = one speaker, one utterance = one WAV,"
  echo "drain bounded, Bridge -> /tap is the only audio path)."
  echo
  echo "If the edit is intentional, ask the user to re-issue the request"
  echo "with the token ACK-INVARIANT in the prompt."
} >&2
exit 2
