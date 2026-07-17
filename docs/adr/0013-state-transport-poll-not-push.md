---
status: accepted
date: 2026-07-07
---

# State transport: a 500 ms poll + weak ETag + visibility gating, not SSE/WS push

The Stages dashboard learns about server state by polling `GET /api/state`:
a 500 ms `setTimeout` fires after each completed tick (`web/js/next/main.js`),
the loop is visibility-gated so a hidden tab stops polling entirely, and the
server answers a conditional GET with a weak ETag so an idle tick returns `304`
and skips the body transfer + JSON parse + state-object allocation
(`web/js/api.js`). The interaction hold (ADR-0004) is built on top of this
cadence — it defers a per-tick render past an operator's focus/selection.

The design is coherent and, for a 1–2-client LAN operator dashboard, correct.
But the poll-**vs-push** choice was never recorded: ADR 0001–0010 don't cover
it, and ADR-0004 explicitly rejects *pausing* the poll without ever writing
down *why polling at all*. The gap is a live hazard — vanilla-web's own
convention makes SSE the default live-data transport (interval polling is its
fallback), so a contributor hitting a payload-size finding could reasonably
"fix" it by reaching for SSE, when the transport was a deliberate choice and
the real fix is payload/signature hygiene. This ADR records the decision so
that reversal is a conscious one.

## Decision

The dashboard's live-state transport is **poll + weak ETag + visibility
gating + interaction hold**, plus an **adaptive idle backoff**:

- The poll runs at 500 ms (`FAST_MS`) while anything is moving.
- It backs off to 2 s (`SLOW_MS`) once the poll has been idle-**and**-unchanged
  (a `304`) for `IDLE_STREAK` consecutive ticks with **no job or tap active**,
  and snaps straight back to fast on the first real change or operator
  interaction (click / keypress / tab re-show). The rule lives in the pure,
  timer-free `web/js/next/poll-pacer.js` (`createPollPacer`), unit-tested
  without wall-clock; `main.js` owns the `setTimeout` and feeds the pacer each
  tick's `{changed, active}` signal.

Rationale: 1–2 LAN clients, where a `304` twice a second is negligible; the
per-tick semantics the interaction hold depends on; and no reconnect/ordering
machinery to own. The backoff is the cheap win that removes the "idle tab polls
forever" cost without changing any of that.

## Considered alternatives

**SSE / WebSocket push (`/api/events` + `EventSource`).** Rejected for this
scale. It needs a server-side change signal TapScribe does not have — the state
blob is rebuilt by walking disk (`gather_sessions` → `_build_state_blob`), and
there is no event bus, file watcher, or mutation broadcast anywhere (the only
`text/event-stream` in the repo is the one-shot setup-install progress stream).
Push therefore means building either an internal-poll→push loop (still walks
disk per build) or a real mutation event-bus (touches every write site), plus
reconnect / initial-snapshot / ordering handling. It would change the *cadence*,
not the interaction-hold *rule* (which is transport-agnostic — `renderRegion` /
`reconcileList` work identically for pushed or polled data), and it does **not**
address the payload/signature-hygiene findings that usually motivate reaching
for it. Net: real new machinery for a marginal gain at LAN scale.

**Pause the poll while the operator interacts.** Already rejected by ADR-0004:
it freezes every panel globally for one local interaction, so the operator
loses live awareness (tap levels, captions) because they selected a line.

## Consequences

- An idle dashboard's poll/CPU/network cost drops to a 2 s cadence, restored
  instantly the moment anything changes or the operator touches the page.
- The interaction hold (ADR-0004) and render-signature hygiene (CLAUDE.md) are
  unchanged and remain the load-bearing rules — the backoff only changes *when*
  the next poll fires, never *how* a tick renders.
- **Revisit triggers** — reopen this decision if any of these land: many
  concurrent dashboard clients, a WAN deployment, or capture-time payload
  growth that survives the ETag / catalog-split fixes. Any of those shifts the
  balance toward a push channel; short of them, poll + backoff is the call.
