// @ts-check
// The ONE save-status lifecycle for the whole dashboard: `saving…` → `saved`
// (clearing itself shortly after) → or `failed: <reason>`, plus the two ways a
// save is triggered — a save BUTTON (`wireSave`) and a debounced optimistic
// field edit (`next/field-saver.js`, which calls `runSaveWithStatus` too).
//
// #355 unified three copies of the debounced-rename flavour; this module closes
// the loop on the fourth, `wireSave`, which had already drifted (its saved badge
// lingered 1500 ms against the rename path's 1400, and it promoted to "saved"
// unguarded). The strings live here as constants because a THIRD module compares
// against them (`components/config-card.js`'s unsaved-badge logic) — the
// vocabulary is load-bearing, not decoration.
//
// Lives beside api.js rather than inside it: api.js is the fetch/data layer, and
// this is the UI-side narration of a save. Importing errText/putJson FROM api.js
// keeps that dependency pointing one way (this module → api.js), which is what
// lets next/field-saver.js and next/ui.js share the lifecycle without a cycle.

import { errText, putJson } from "./api.js";

/** How long the "saved" badge lingers before clearing itself. */
export const SAVED_BADGE_MS = 1400;

export const SAVING = "saving…";
export const SAVED = "saved";
/** What a failure message starts with, so a later save can recognise (and
 * supersede) a stale one — see `runSaveWithStatus`. */
export const FAILED_PREFIX = "failed: ";

/**
 * Where a save writes its progress. `replace(from, to)` writes `to` ONLY where
 * the text still reads `from` — that's how a settled save declines to overwrite
 * a newer message. See `statusTarget` for the DOM implementation.
 * @typedef {object} StatusTarget
 * @property {(text: string) => void} set Write unconditionally.
 * @property {(from: string, to: string) => void} replace Write `to` only where
 *   the text still reads exactly `from`.
 * @property {(accepts: (current: string) => boolean, to: string) => void} replaceWhen
 *   Write `to` only where the current text satisfies `accepts` — the general
 *   form of `replace`, for a save that may legitimately supersede more than one
 *   prior message.
 */

/**
 * A `StatusTarget` over the status cells `resolve()` returns. The resolver is
 * called on EVERY write, never captured: the card holding a status cell is
 * routinely rebuilt between a PUT starting and settling (the interaction hold's
 * blur flush, or any sig change, inside a debounce window), and a captured node
 * is DETACHED by then — a `failed: …` written there is invisible and the
 * operator reads a broken save as saved. Multi-cell because one record's status
 * can be mirrored in more than one place, and each cell is guarded
 * independently so a superseded one keeps its newer text.
 * @param {() => Iterable<Element>} resolve
 * @returns {StatusTarget}
 */
export function statusTarget(resolve) {
  /** @type {StatusTarget} */
  const target = {
    set(text) {
      for (const el of resolve()) el.textContent = text;
    },
    replaceWhen(accepts, to) {
      for (const el of resolve()) if (accepts(el.textContent || "")) el.textContent = to;
    },
    replace(from, to) {
      target.replaceWhen((current) => current === from, to);
    },
  };
  return target;
}

/** A `StatusTarget` over a single, already-resolved cell. For a status element
 * that outlives the save (a button's own status line, wired once beside it) —
 * prefer `statusTarget` wherever the cell can be rebuilt mid-save.
 * @param {Element} el
 * @returns {StatusTarget} */
export function cellStatus(el) {
  return statusTarget(() => [el]);
}

/**
 * Run `put` and narrate it into `target`: `saving…` while it's in flight, then
 * `saved` (auto-clearing) or `failed: <reason>`. THE settle path — every save in
 * the dashboard that shows a status goes through here.
 *
 * The promotion to `saved` is GUARDED: if something wrote an unrelated newer
 * message while the PUT was in flight (a job's progress line, say), this save
 * doesn't stomp it. It DOES supersede a stale `failed: …`, because two saves can
 * share one status cell (summary.js wires two buttons to the same one) and a
 * success that couldn't clear an earlier failure would leave the operator
 * staring at an error for a save that worked — failures never auto-clear.
 * @param {StatusTarget} target
 * @param {() => Promise<unknown>} put
 * @param {{ onSuccess?: (() => void) | undefined, afterSettle?: (() => void) | undefined }} [hooks]
 *   `onSuccess` fires only on a successful put; `afterSettle` runs either way.
 */
export async function runSaveWithStatus(target, put, { onSuccess, afterSettle } = {}) {
  target.set(SAVING);
  try {
    await put();
    target.replaceWhen((current) => current === SAVING || current.startsWith(FAILED_PREFIX), SAVED);
    onSuccess?.();
    setTimeout(() => target.replace(SAVED, ""), SAVED_BADGE_MS);
  } catch (e) {
    target.set(`${FAILED_PREFIX}${errText(e)}`);
  } finally {
    afterSettle?.();
  }
}

/**
 * Wire a save button to an async PUT with the shared status lifecycle. The
 * button is disabled for the duration so a double-click can't double-PUT.
 * Structured saves (the #84 summarizer-default card, the Summary view's
 * per-session override) call this with their own `put`; `wireConfigSave` below
 * is the `/api/config/{key}` specialisation.
 * @param {{
 *   btn: HTMLButtonElement,
 *   status: HTMLElement | null,
 *   put: () => Promise<unknown>,
 *   onSuccess?: (() => void) | undefined,
 *   afterSettle?: (() => void) | undefined,
 * }} opts
 */
export function wireSave({ btn, status, put, onSuccess, afterSettle }) {
  btn.addEventListener("click", async () => { // gate-allow: signal-listener — wireSave wires the button once when the caller builds it; the listener dies with the button
    if (!status) return;
    btn.disabled = true;
    // The status line is wired once beside its button and isn't rebuilt under a
    // click, so the single-cell target is right here.
    await runSaveWithStatus(cellStatus(status), put, {
      onSuccess,
      afterSettle: () => {
        btn.disabled = false;
        afterSettle?.();
      },
    });
  });
}

/**
 * Wire a textarea + save button to PUT /api/config/{key}. Used by both the
 * "default config" card editors and the live-channel's init-prompt expandable.
 * The {content: textarea.value} specialisation of `wireSave`.
 * @param {{
 *   key: string,
 *   btn: HTMLButtonElement,
 *   textarea: HTMLTextAreaElement | HTMLInputElement | null,
 *   status: HTMLElement | null,
 *   onSuccess?: ((value: string) => void) | undefined,
 * }} opts
 */
export function wireConfigSave({ key, btn, textarea, status, onSuccess }) {
  if (!textarea) return;
  wireSave({
    btn,
    status,
    put: () => putJson(`/api/config/${key}`, { content: textarea.value }),
    onSuccess: () => onSuccess?.(textarea.value),
  });
}
