// @ts-check
// Small shared UI helpers for the Stages views — behaviour more than one view
// needs but that belongs to neither the render seam (templates.js) nor the
// data layer (api.js).

/**
 * Build a transient status flasher bound to `el`: shows `msg`, then clears it
 * after `ms` unless a newer message superseded it in the meantime (the
 * textContent equality check). Each flasher owns its own timer slot, so two
 * status elements never clobber each other's pending clears.
 * @param {HTMLElement} el
 * @param {number} [ms]
 * @returns {(msg: string) => void}
 */
export function makeStatusFlasher(el, ms = 1500) {
  /** @type {ReturnType<typeof setTimeout> | null} */
  let timer = null;
  return (msg) => {
    if (timer != null) clearTimeout(timer);
    el.textContent = msg;
    timer = setTimeout(() => {
      if (el.textContent === msg) el.textContent = "";
      timer = null;
    }, ms);
  };
}

/**
 * Is the async Clipboard API usable here? False in a NON-SECURE context —
 * TapScribe's documented multi-machine mode is plain http over LAN
 * (start.sh --lan; TLS is opt-in), where navigator.clipboard doesn't exist.
 * Callers own the fallback UX (prompt / styled new tab): it has to run inside
 * the user-gesture window, so only the probe — the load-bearing
 * secure-context rule — is shared.
 * @returns {boolean}
 */
export const clipboardAvailable = () =>
  window.isSecureContext && typeof navigator.clipboard?.writeText === "function";
