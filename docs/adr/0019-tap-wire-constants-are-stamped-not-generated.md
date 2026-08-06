---
status: accepted
date: 2026-07-29
---

# The `/tap` wire constants are stamped from the Recorder, not generated

The `/tap` wire contract — subprotocol prefix, sample rate, channel count,
sample width, frame size, the reserved `__probe__` identity — is declared
independently in several languages and prose files. Each is internally
single-sourced, but a contract change is a many-place edit where one
*forgotten* place produces a Bridge that silently stops working.

## Decision

`tapscribe/` is the **source**. The Recorder serves `/tap`, so its constants
*are* the contract; a wire change is a hand edit there — a real server
behaviour change, with its own tests. `tools/stamp_tap_wire.py` reads those
constants and writes the matching literal into every Bridge and every doc that
restates them; `tests/test_tap_wire_contract.py` fails when a site drifts.
This is the shape `tools/bump_version.py` +
`tests/test_version_consistency.py` already use for the version string: each
file keeps its own idiomatic declaration, one tool rewrites the literals, one
test proves they agree.

Python on the Recorder's own side of the wire doesn't need stamping at all —
it imports. Only another language, or prose, gets a stamp row.

## Why a stamper, not codegen

Generated per-language constants files from one TOML/JSON source were
rejected for three concrete reasons, not stylistic ones:

- `page-script.js` runs in the MV3 **MAIN world**, injected as a plain
  `<script src>`. Consuming a generated module means `type="module"`, which
  changes script-execution timing in the one file that must not break audio
  capture.
- `local_test_bridge.py` is deliberately a **single file** — the simplest
  reference implementation, the one to crib from when bootstrapping a new
  Bridge. A generated sibling module destroys that.
- Codegen cannot cover **prose**, and prose is where most of the drift sites
  are.

A gate with no stamper was also rejected: it reports drift but leaves the
repair a hand edit in every language — the same work the gate just proved
people get wrong.

## Consequences

**Two tiers, with different authority.** The *Wire contract* (above) is
enforced and stamped. The [Blip-resilience recipe](../../CONTEXT.md) — the
backoff ladder, gap-buffer cap and drain budget — is **not** stamped: the
Recorder has no opinion on it, and a third-party Bridge may legitimately
deviate. It is pinned only among the bundled Bridges and the docs, by a golden
table in the gate. The stamper structurally cannot write it (those sites carry
a `Spelling` with no `render`).

**Close code 4401 is excluded.** It is asymmetric: the Recorder emits it as an
*unnamed literal* (`routes/tap.py`), so there is nothing to stamp from; the
tray Bridge's production code never branches on the numeric; and it fails
loudly rather than silently, with the JS behaviour already pinned in
`spacialchat-bridge/tests/wire-contract.test.js`. The selection rule the
exclusion comes from: **cover what fails silently.** A wrong path or close
code gives a 404 or a refused upgrade; a wrong frame size gives garbled audio,
and a wrong `__probe__` quietly binds a junk Person into the operator's global
`people.json`.

**The first-connect-failure semantics stay out.** `bridges/README.md`
documents them as a deliberate per-Bridge choice; a gate there would freeze a
divergence the docs call intentional.

**The completeness sweep is deliberately narrow, and says so.** A broad sweep
for the contract's numbers finds hundreds of hits, nearly all incidental prose
in modules that merely handle recorder-format audio; an exempt list that long
would be worse than no gate. The tripwire looks only for the two shapes that
go stale silently — a constant *named* like a wire constant outside a declared
site, and the subprotocol *literal* spelled out in shipped code or docs.
