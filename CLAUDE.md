# Pointers for Claude

This file just points at the canonical sources. The actual rules live where
they're enforced — don't restate them here.

## Glossary

`CONTEXT.md` is the project's domain glossary (Recorder, Bridge, /tap,
utterance, drain, tail flush, invariants, …). Use those names in code
and prose; don't introduce shadow vocabulary.

## Conventions

Enforced by hooks, not by prose:

- `.claude/hooks/session-start.sh` installs the test/lint deps that match
  `.github/workflows/ci.yml`. If you add a runtime dep to `pyproject.toml`,
  add it to both.
- `.claude/hooks/stop.sh` runs `ruff check tapscribe tests` at the end of
  each turn and blocks the stop on failures. Ruff config is in
  `pyproject.toml` (`[tool.ruff]`); change it there if a rule is wrong,
  not by suppressing in code.

## Tests

Canonical invocation: `pytest tests -q` (or `python3 -m pytest tests -q`
if a different `pytest` shadow-binary is on PATH). The bridge JS suite
runs via `node --test "bridges/spacialchat-bridge/tests/*.test.js"` with
no dependencies. Both are wired into CI in `.github/workflows/ci.yml`.

## Wire contract

Bridge → `/tap` is the only audio path into the Recorder. Frame format,
query parameters, and reconnect/resume semantics are documented in
`bridges/README.md` and the `Utterance` / `utterance_id` / `Drain`
entries in `CONTEXT.md`.
