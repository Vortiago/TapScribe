#!/bin/bash
# Installs Python + JS test/lint deps so Claude Code on the web can run
# pytest, ruff, and node --test on a freshly-provisioned container.
# Runs only on the web (local sessions already have your dev env).
set -euo pipefail

if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

# Python: install the runtime + dev deps the CI matrix uses. Mirrors
# .github/workflows so a green hook implies a green CI install step.
pip install --quiet --disable-pip-version-check \
  fastapi uvicorn python-multipart numpy websockets \
  pytest pytest-asyncio pytest-cov httpx ruff cryptography

# JS: bridges/local-test-bridge has no package.json and the Chrome
# extension's tests run on plain `node --test`, so no `npm install`.
# Node 22 is pre-provisioned on the web runner.
