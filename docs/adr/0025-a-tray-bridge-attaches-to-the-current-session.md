---
status: accepted
date: 2026-08-28
---

# A tray Bridge attaches to the current session by following it, not by pinning it

The [Tray Bridge](../../CONTEXT.md#tray-bridge) gains **Connect / Disconnect**
beside Start / End meeting: an [attached tap](../../CONTEXT.md#current-session--attached-tap)
streams into the Recorder's current session — the one the dashboard badges
`● live` — instead of a bridge-minted detached one. The use it exists for is a
mic in a meeting room feeding live transcription with nobody at the keyboard.

It needs **no Recorder change**: `routes/tap.py` already routes a `/tap` WS with
no `?session=` to `recorder.session_start`. The mode is the Bridge omitting a
query parameter.

Shape, all Bridge-side:

- **One-shot, not latched.** Connect is a click; nothing is persisted and a tray
  restart comes up idle. Rejected: a persisted latch that re-attaches on launch —
  better for an unattended box, but it is only worth building alongside
  launch-at-login, which does not exist on either shell.
- **Attached or bracketed, never both.** One device is one speaker; one identity
  feeding two sessions at once splits that speaker across them. Start meeting
  from Attached is a TAKEOVER: drain the attached taps (so the last utterance's
  WAV is finalised in the current session), mint, start. End meeting returns to
  Idle.
- **The same `EffectiveDevices` as a meeting**, with the same `SelectionVerdict`
  refusal. A second device list would express what "pin the mic in Settings"
  already expresses.
- **`ConnectionTester` is the pre-flight.** Start meeting gets one free — its
  mint round-trips the Recorder — and an attached tap has no mint, so an
  unreachable Recorder or a rejected token would otherwise stay silent until the
  first person spoke.
- **Disconnect drains and closes, and triggers nothing** — the same effect as
  `ProcessOnEnd: false`, but NOT that code path. `RunPipelineFlowAsync` builds a
  `MeetingController`, which requires a session id and throws without one, and
  every other thing End does — the trigger, the poll, the Past-meetings entry,
  the restart-resume state — is keyed on one too. An attached tap has no session
  id, so Disconnect is the simpler path: take, drain, say so, idle.

## Consequences

**A rotation splits an attached conversation.** Session affiliation is
snapshotted per WS and one WS is one Utterance, so a `/api/new-session` from the
dashboard (or a Recorder restart) sends the NEXT utterance to the new session
while the current one finishes in the old. That is the intended reading — a
rotation means "a new meeting starts now" and a room mic should follow it — but
it means an attached tap has no single durable session id, unlike a bracketed
meeting, whose detached id is its handle.

**Processing an attached conversation means rotating first.**
`guards.refuse_current_or_busy` refuses every destructive/processing route on
the current session, and the dashboard disables Process on it. So the workflow
is rotate, then process the session just archived — which is the dashboard's
existing flow, not a new limitation, but it is why the tray offers no
End-and-summarise for an attached tap.

**A room mic declares itself single-person.** `TapModeForFlow` maps a capture
device to `tap_mode=single`, which is right for a laptop mic and wrong for a
room. The operator flips that identity to multi once in the dashboard
(operator override › bridge declaration, ADR-0021); no code change.

## Considered options

**Pin the current session id at Connect** (resolve it once, then pass
`?session=<id>` like a detached tap). It fixes both consequences above — the
conversation never splits and can be processed without rotating. Rejected
because it stops being "connect to live": a rotation is the operator saying the
meeting changed, and a room mic that kept feeding the previous session would be
recording the new meeting into the old one, which is a worse and quieter
failure than a split.

**A distinct identity per mode** (`windows-tray` vs `windows-tray-live`), which
would let a tray attach and bracket at once. Rejected: it splits one speaker
across two entries in the People registry to enable a combination no machine
wants — a room box never brackets, a laptop never attaches.
