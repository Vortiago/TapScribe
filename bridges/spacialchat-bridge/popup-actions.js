// @ts-check
// SpatialChat Bridge — popup-actions.js (DOM-free meeting side-effects)
//
// The popup's Start / End / Dismiss effects, factored out of the DOM shell so
// they can be unit-tested with injected fakes (a fake control client + fake
// chrome.storage) instead of through a stubbed popup. The shell wires buttons
// to these and renders the returned outcome; these own no DOM and no "what to
// show" decisions (that's popup-presenter.js).

import { snapshotIsLive } from "./taps-view.js";

/**
 * @typedef {{ host: string, port: number | string, useTls?: boolean, token?: string }} Cfg
 * @typedef {{ set(items: Record<string, unknown>): Promise<void> }} StorageArea
 * @typedef {{ createDetachedSession(cfg: Cfg, opts?: { timeoutMs?: number }): Promise<{ sessionId: string }>, triggerPipeline(cfg: Cfg, sessionId: string, opts?: { timeoutMs?: number }): Promise<{ outcome: string }> }} Control
 */

/** @param {unknown} e */
function errKind(e) { return (e && typeof e === "object" && "kind" in e) ? String(/** @type {any} */ (e).kind) : null; }
/** @param {unknown} e */
function errText(e) { return String((e && /** @type {{ message?: unknown }} */ (e).message) || e); }

/**
 * Start a meeting: mint a detached session, then persist the durable meeting
 * state the content script routes on and the card polls. Never throws —
 * returns a tagged outcome the shell renders. Persist happens only after the
 * mint succeeds, and a persist failure is reported as `ok:false` so the popup
 * never shows a meeting the content script never saw.
 * @param {{ control: Control, storage: StorageArea, cfg: Cfg }} deps
 * @returns {Promise<{ ok: true, sessionId: string } | { ok: false, kind: string | null, message: string }>}
 */
export async function startMeeting({ control, storage, cfg }) {
  let sessionId;
  try {
    const res = await control.createDetachedSession(cfg, { timeoutMs: 6000 });
    sessionId = res.sessionId;
  } catch (e) {
    return { ok: false, kind: errKind(e), message: errText(e) };
  }
  try {
    // meetingEndRequestedAt is reset here too: a leftover End nonce from the
    // PREVIOUS meeting must not make the new one render as "Ending meeting…"
    // (the presenter derives the pending-End state from it).
    await storage.set({
      meetingSessionId: sessionId,
      meetingActive: true,
      meetingEnd: null,
      meetingEndRequestedAt: null,
    });
  } catch (e) {
    return { ok: false, kind: "storage", message: errText(e) };
  }
  return { ok: true, sessionId };
}

/**
 * Request End meeting: bump a nonce the content script's storage.onChanged
 * turns into drain → close-all → pipeline trigger (it owns the WebSockets and
 * outlives the popup).
 * @param {{ storage: StorageArea, now: number }} deps
 */
export async function requestEndMeeting({ storage, now }) {
  await storage.set({ meetingEndRequestedAt: now });
}

/**
 * End meeting: live-vs-stale dispatch. If a SpatialChat tab is live, delegate
 * via the nonce path (the content script owns drain → trigger → meetingEnd).
 * If stale/absent, complete the End ourselves by triggering the pipeline
 * directly so the popup doesn't wedge on "Ending meeting…".
 * @param {{ control: Control, storage: StorageArea, cfg: Cfg, sessionId: string, snapshot: any, now: number }} deps
 * @returns {Promise<void>}
 */
export async function endMeeting({ control, storage, cfg, sessionId, snapshot, now }) {
  if (snapshotIsLive(snapshot, now)) {
    await requestEndMeeting({ storage, now });
    return;
  }
  // The trigger owns phase/error; the terminal persist is deliberately OUTSIDE
  // this try (mirroring startMeeting) so a post-trigger storage failure can't be
  // caught here and mislabel an already-successful trigger as phase:"failed"
  // (it rejects instead, so onEnd re-enables the buttons — no wedge, no
  // duplicate trigger on retry). The failure text is the thrown message
  // verbatim: a mixed-content ControlError carries the client's
  // MIXED_CONTENT_BLOCKED_TEXT as its message (control-client.js throws the
  // constant), so the stale-tab failed card matches content.js's live-tab card
  // without a kind-keyed remap here — end-meeting-stale-tab.test.js pins that
  // parity against the real client.
  /** @type {string} */ let phase;
  /** @type {string | null} */ let error;
  try {
    const res = await control.triggerPipeline(cfg, sessionId, { timeoutMs: 6000 });
    phase = res.outcome === "busy" ? "busy" : "started";
    error = null;
  } catch (e) {
    phase = "failed";
    error = errText(e);
  }
  await storage.set({
    meetingActive: false,
    meetingEnd: { phase, sessionId, error, ts: now },
  });
}

/**
 * Dismiss a finished/failed meeting: clear the durable state so the card stops
 * re-deriving last meeting's result on every open.
 * @param {{ storage: StorageArea }} deps
 */
export async function dismissMeeting({ storage }) {
  await storage.set({
    meetingSessionId: null,
    meetingActive: false,
    meetingEnd: null,
    meetingEndRequestedAt: null,
  });
}
