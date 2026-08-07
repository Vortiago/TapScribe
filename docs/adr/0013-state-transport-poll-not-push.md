---
status: accepted
date: 2026-07-07
---

# State transport: a 500 ms poll + weak ETag + visibility gating, not SSE/WS push

## Decision

The dashboard's live-state transport is **poll + weak ETag + visibility
gating + interaction hold**, plus an **adaptive idle backoff**:

- A 500 ms `setTimeout` (`FAST_MS`) fires after each completed tick
  (`web/js/next/main.js`); the loop is visibility-gated (a hidden tab stops
  polling entirely), and the server answers a conditional `GET /api/state`
  with a weak ETag, so an idle tick returns `304` and skips the body
  transfer + JSON parse + state-object allocation (`web/js/api.js`).
- The poll backs off to 2 s (`SLOW_MS`) once it has been idle-**and**-unchanged
  (a `304`) for `IDLE_STREAK` consecutive ticks with **no job or tap active**,
  and snaps straight back to fast on the first real change or operator
  interaction (click / keypress / tab re-show). The rule lives in the pure,
  timer-free `web/js/next/poll-pacer.js` (`createPollPacer`), unit-tested
  without wall-clock; `main.js` owns the `setTimeout` and feeds the pacer each
  tick's `{changed, active}` signal.
- The interaction hold (ADR-0004) rides this cadence, deferring a per-tick
  render past an operator's focus/selection.

Rationale: 1–2 LAN clients, where a `304` twice a second is negligible; the
per-tick semantics the interaction hold depends on; and no reconnect/ordering
machinery to own. The backoff removes the "idle tab polls forever" cost
without changing any of that.

## Considered alternatives

**SSE / WebSocket push (`/api/events` + `EventSource`).** Tempting because
vanilla-web's own convention makes SSE the default live-data transport — but
it needs a server-side change signal TapScribe does not have: the state blob
is rebuilt by walking disk (`gather_sessions` → `_build_state_blob`), and
there is no event bus, file watcher, or mutation broadcast anywhere (the only
`text/event-stream` in the repo is the one-shot setup-install progress
stream). Push therefore means building either an internal-poll→push loop
(still walks disk per build) or a real mutation event-bus (touches every
write site), plus reconnect / initial-snapshot / ordering handling. It would
change the *cadence*, not the interaction-hold *rule* (which is
transport-agnostic), and it does **not** address the payload/signature-
hygiene findings that usually motivate reaching for it. Net: real new
machinery for a marginal gain at LAN scale.

**Pause the poll while the operator interacts.** Rejected by ADR-0004: it
freezes every panel globally for one local interaction.

## Consequences

- An idle dashboard's poll/CPU/network cost drops to a 2 s cadence, restored
  instantly the moment anything changes or the operator touches the page.
- The interaction hold (ADR-0004) and render-signature hygiene (CLAUDE.md)
  remain the load-bearing rules — the backoff only changes *when* the next
  poll fires, never *how* a tick renders.
- **Revisit triggers**: many concurrent dashboard clients, a WAN deployment,
  or capture-time payload growth that survives the ETag / catalog-split
  fixes. Any of those shifts the balance toward a push channel; short of
  them, poll + backoff is the call.
