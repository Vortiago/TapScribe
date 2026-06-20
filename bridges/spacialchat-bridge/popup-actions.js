// @ts-check
// SpatialChat Bridge — popup-actions.js (DOM-free meeting side-effects)
//
// The popup's Start / End / Dismiss effects, factored out of the DOM shell so
// they can be unit-tested with injected fakes (a fake control client + fake
// chrome.storage) instead of through a stubbed popup. The shell wires buttons
// to these and renders the returned outcome; these own no DOM and no "what to
// show" decisions (that's popup-presenter.js).

/**
 * @typedef {{ host: string, port: number | string, useTls?: boolean, token?: string }} Cfg
 * @typedef {{ set(items: Record<string, unknown>): Promise<void> }} StorageArea
 * @typedef {{ createDetachedSession(cfg: Cfg, opts?: { timeoutMs?: number }): Promise<{ sessionId: string }> }} Control
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
    await storage.set({ meetingSessionId: sessionId, meetingActive: true, meetingEnd: null });
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
 * Dismiss a finished/failed meeting: clear the durable state so the card stops
 * re-deriving last meeting's result on every open.
 * @param {{ storage: StorageArea }} deps
 */
export async function dismissMeeting({ storage }) {
  await storage.set({ meetingSessionId: null, meetingActive: false, meetingEnd: null });
}
