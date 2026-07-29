---
status: accepted
date: 2026-07-29
---

# The `/tap` wire constants are stamped from the Recorder, not generated

The `/tap` wire contract — subprotocol prefix, sample rate, channel count,
sample width, frame size, the reserved `__probe__` identity — was declared
independently in four languages and six prose files, with no mechanical check
that they agreed. Each language was internally single-sourced, so a v1→v2
subprotocol change was a many-place edit where a *forgotten* place produced a
Bridge that silently stopped working. `speech_gate.py` said so in a comment —
"Don't reuse constants from bridges/ — those are JS — but they MUST agree" —
and nothing enforced the second half. `tests/e2e/harness.py` carried a third
Python copy under a comment that was itself already false (#356).

## Decision

`tapscribe/` is the **source**. The Recorder serves `/tap`, so its constants
*are* the contract; a wire change is a hand edit there — a real server
behaviour change, with its own tests. `tools/stamp_tap_wire.py` then reads
those constants and writes the matching literal into every Bridge and every
doc that restates it, and `tests/test_tap_wire_contract.py` fails when a site
drifts. This is the shape `tools/bump_version.py` +
`tests/test_version_consistency.py` already use for the version string: each
file keeps its own idiomatic declaration, one tool rewrites the literals, one
test proves they agree.

Python on the Recorder's own side of the wire doesn't need stamping at all —
it imports. Only another language, or prose, gets a stamp row.

## Considered options

**Generated constants files per language, from one TOML/JSON source**, was the
issue's other suggestion. Rejected for three concrete reasons, not stylistic
ones:

- `page-script.js` runs in the MV3 **MAIN world** and is injected as a plain
  `<script src>` (`content.js:300-301`; the manifest's
  `web_accessible_resources` lists only that file). Consuming a generated
  module means converting it to `type="module"`, which changes script-execution
  timing in the one file that must not break audio capture.
- `local_test_bridge.py` is deliberately a **single file** (stdlib + numpy +
  websockets at import, `sounddevice` for capture). `bridges/README.md` sells
  it as the simplest reference implementation, the one to crib from when
  bootstrapping a new Bridge. A generated sibling module destroys that.
- Codegen cannot cover **prose**, and prose is where most of the drift sites
  turned out to be — six of the ten, including two the issue didn't name.

A gate with no stamper was also considered and rejected: it reports drift but
leaves the repair a hand edit in every language, which is the same work the
gate just proved people get wrong.

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
tray Bridge's production code never branches on the numeric (a pre-accept
refusal surfaces as an exception, so `ConnectionTester` treats any close as
rejection); and it fails loudly rather than silently, with the JS behaviour
already pinned in `spacialchat-bridge/tests/wire-contract.test.js`. The
selection rule the exclusion comes from: **cover what fails silently.** A
wrong path or close code gives a 404 or a refused upgrade; a wrong frame size
gives garbled audio, and a wrong `__probe__` quietly binds a junk Person into
the operator's global `people.json`.

**The first-connect-failure semantics stay out.** `bridges/README.md`
documents them as a deliberate per-Bridge choice, and a gate there would
freeze a divergence the docs call intentional.

**The completeness sweep is deliberately narrow, and says so.** A broad sweep
for the contract's numbers finds ~206 hits across ~74 files, nearly all
incidental prose in modules that merely handle recorder-format audio; an
exempt list that long would be worse than no gate. The tripwire therefore
looks for the two shapes that actually go stale silently — a constant *named*
like a wire constant outside a declared site, and the subprotocol *literal*
spelled out in shipped code or docs. Incidental prose describing the format is
out of scope by design.
