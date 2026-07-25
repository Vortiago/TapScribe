// @ts-check
// Small shared UI helpers for the Stages views — behaviour more than one view
// needs but that belongs to neither the render seam (templates.js) nor the
// data layer (api.js).

import { cellStatus } from "../save-status.js";

/**
 * Build a transient status flasher bound to `el`: shows `msg`, then clears it
 * after `ms` unless a newer message superseded it in the meantime.
 *
 * A specialisation of the shared save-status target (`replace(msg, "")` is the
 * "unless superseded" rule), but it still owns a timer SLOT: the common case is
 * re-flashing the SAME string (clicking Copy twice), and there the text guard
 * can't tell the old message from the new one — the first timer would blank the
 * second flash early. Cancelling the pending clear gives every message its full
 * `ms`. Unlike a save's status (`runSaveWithStatus`), EVERY message here clears
 * — these are copy/reveal confirmations, not save outcomes to be read.
 * @param {HTMLElement} el
 * @param {number} [ms]
 * @returns {(msg: string) => void}
 */
export function makeStatusFlasher(el, ms = 1500) {
  const target = cellStatus(el);
  /** @type {ReturnType<typeof setTimeout> | null} */
  let timer = null;
  return (msg) => {
    if (timer != null) clearTimeout(timer);
    target.set(msg);
    timer = setTimeout(() => {
      target.replace(msg, "");
      timer = null;
    }, ms);
  };
}

/** Is the async Clipboard API usable here? False in a NON-SECURE context —
 * TapScribe's documented multi-machine mode is plain http over LAN
 * (start.sh --lan; TLS is opt-in), where navigator.clipboard doesn't exist.
 * Module-private: copyToClipboard below owns the whole probe-then-write
 * flow — callers go through it rather than re-rolling the probe.
 * @returns {boolean} */
const clipboardAvailable = () =>
  window.isSecureContext && typeof navigator.clipboard?.writeText === "function";

/**
 * Copy `text` via the async Clipboard API when it's usable (secure context
 * with clipboard.writeText — see the probe above), else run the caller's
 * fallback UX. `onOk` fires only on a successful clipboard write;
 * `onFallback` runs both when the API is unavailable (non-secure context —
 * called SYNCHRONOUSLY, still inside the user-gesture window, so a fallback
 * window.open isn't popup-blocked) and when the write is rejected (permission
 * denied — past the gesture by then, so popup-dependent fallbacks should
 * degrade to a prompt()). Callers own the fallback because its UX differs
 * (a prompt vs a populated new tab); the probe-then-write flow itself lives
 * only here.
 * @param {string} text
 * @param {{ onOk: () => void, onFallback: () => void }} handlers
 */
export async function copyToClipboard(text, { onOk, onFallback }) {
  if (!clipboardAvailable()) {
    onFallback();
    return;
  }
  try {
    await navigator.clipboard.writeText(text);
    onOk();
  } catch {
    // Clipboard write rejected (permission denied). The text isn't lost —
    // the fallback surfaces it for manual copy.
    onFallback();
  }
}

/**
 * Set a stat/health tile's text, dimming empty/em-dash placeholders so a
 * missing metric recedes (the `.is-empty` styling in next.css) while real
 * values stay bright. Shared by the Capture health tiles and the Recordings
 * wave-stat quartet.
 * @param {HTMLElement} el
 * @param {string} value
 */
export function setDimmable(el, value) {
  el.textContent = value;
  el.classList.toggle("is-empty", value === "" || value === "—");
}
