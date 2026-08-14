#!/bin/bash
# Installs Python + JS test/lint deps so Claude Code on the web can run
# pytest, ruff, and node --test on a freshly-provisioned container.
# Runs only on the web (local sessions already have your dev env).
set -euo pipefail

if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

# Run the install in the background so the session can start immediately.
# Trade-off: if Claude tries to run pytest/ruff in the first ~10s of the
# session, it may race the install. The deps are deterministic and small,
# so the race window is short.
echo '{"async": true, "asyncTimeout": 300000}'

# Python: the same install ci.yml's `tests` job runs. `-e .` resolves the
# core deps and their version bounds from pyproject instead of restating
# them here, so that half cannot drift out of step with CI. hypothesis is
# imported at module scope by tests/test_tap_endpoint.py: without it
# `pytest tests` aborts during collection, which also blocks the pre-push
# gate in .claude/hooks/pre-push.sh.
pip install --quiet --disable-pip-version-check \
  -e . pytest pytest-asyncio pytest-cov httpx hypothesis ruff

# JS: bridges/local-test-bridge has no package.json and the Chrome
# extension's tests run on plain `node --test`, so no `npm install`.
# Node 22 is pre-provisioned on the web runner.

# Frontend typecheck deps (TypeScript). Install only if the package.json
# exists so this hook stays safe on branches/checkouts that pre-date it.
if [ -f frontend/package.json ]; then
  (cd frontend && npm install --silent --no-audit --no-fund)
fi
