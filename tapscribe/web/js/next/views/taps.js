// @ts-check
// gate-allow: signal-listener — the one handler here is delegated onto a host this view builds and owns, so an evicted or rebuilt view drops it with the subtree (no document/window targets). Revisit if views gain a mount AbortSignal.
// Stages · Taps (GLOBAL · Ingress). The global ingress stage: the connected
// /tap streams (level/lag/gate/in-flight buffer + rec/live toggles), the
// speech-gate LiveConfig (gate_kind/threshold/hangover/pre-roll/min-speech +
// confidence), and the per-identity single/multi-person control (ADR-0021).
//
// This stage holds the MODE and not the voice list: mode is a property of a live
// source, the Voices belong to a finished recording and live on Transcript.
//
// REUSES the classic dashboard components verbatim (imported, not copied):
//   - active-taps.js   → the connected-tap rows + rec/live toggle DOM
//   - live-channel.js  → the gate LiveConfig form + start/stop (→ /api/live/start)
//
// Built once for the page; `update(j, session)` re-runs the live-data
// components each poll tick — live-channel renders through renderRegion
// (focus-guarded, per-host signature), so an in-progress gate edit survives
// the tick and this view's host caches independently of Capture's.
// live-channel reads its form back out of the bodyEl it was rendered into
// (not the global document — see #254), so this view's instance and
// Capture's don't collide even though both host elements can be alive in
// main.js's viewCache at once.

import { tpl, pick, renderList } from "../../templates.js";
import { header, strong, inline, wireRecPill, paintRecPill } from "../shell.js";
import { mutateButton, putJson } from "../../api.js";
import * as activeTaps from "../../components/active-taps.js";
import * as liveChannel from "../../components/live-channel.js";

/** Everything a mode row displays, in one spelling: the list gate and the row
 * gate are the same tuple, and hand-maintaining two of them is how a list goes
 * silently stale when a field is added to one.
 * @param {import('../../types.js').ActiveStream} a */
const modeSig = (a) => `${a.identity}§${a.name}§${a.mode || "single"}`;

/**
 * What a click on a mode button means, or null when it means nothing (the mode
 * it names is already the effective one). Exported and DOM-only so the node test
 * can drive it without a browser — the same split `active-taps.js`'s
 * `toggleIntent` uses.
 * @param {HTMLElement | null} btn
 * @returns {{ identity: string, mode: "single" | "multi" } | null}
 */
export function modeIntent(btn) {
  const row = btn?.closest(".moderow");
  const identity = /** @type {HTMLElement | null} */ (row)?.dataset.identity || "";
  const mode = btn?.dataset.mode;
  if (!identity || (mode !== "single" && mode !== "multi")) return null;
  if (btn?.getAttribute("aria-pressed") === "true") return null;
  return { identity, mode };
}

/**
 * @param {{
 *   liveCatalog: import('../../types.js').ModelCatalog,
 *   onLiveStart: (host: HTMLElement) => void,
 *   onLiveStop: () => void,
 *   afterMutate: () => void,
 * }} ctx
 * @returns {{ node: DocumentFragment, update: (j: import('../../types.js').AppState, session: import('../../types.js').Session | null) => void }}
 */
export function build(ctx) {
  const { liveCatalog, onLiveStart, onLiveStop, afterMutate } = ctx;
  const frag = tpl("tpl-next-view-taps");

  const headHost = pick(frag, "head");
  const recPill = /** @type {HTMLButtonElement} */ (pick(frag, "recPill"));
  const activeTapsCtx = {
    countEl: pick(frag, "activeCount"),
    badgeEl: pick(frag, "activeTapsBadge"),
    bodyEl: pick(frag, "activeTapsBody"),
  };
  const modeList = pick(frag, "modeList");
  const modeEmpty = pick(frag, "modeEmpty");
  const liveChannelHosts = {
    stateEl: pick(frag, "liveStateBadge"),
    mlxEl: pick(frag, "liveMlxNote"),
    bodyEl: pick(frag, "liveChannelBody"),
  };

  /** @type {import('../../types.js').AppState | null} */
  let latest = null;

  activeTaps.wireToggles(activeTapsCtx.bodyEl, { afterMutate });

  // ONE delegated listener on the rows host, bound once: `renderList` swaps rows
  // under it every time a tap connects or drops, and a per-row listener would be
  // re-bound on each of those. The class flip is optimistic so the click feels
  // immediate; the next poll repaints from the authoritative mode.
  modeList.addEventListener("click", (ev) => {
    const btn = /** @type {HTMLButtonElement | null} */ (
      /** @type {Element | null} */ (ev.target)?.closest(".tap-mode")
    );
    const intent = modeIntent(btn);
    if (!btn || !intent) return;
    const seg = /** @type {HTMLElement} */ (btn.parentElement);
    const was = modeOf(seg);
    paintMode(seg, intent.mode);
    mutateButton(
      btn,
      () =>
        putJson("/api/tap-mode", intent).catch((e) => {
          // Put the rejected paint back. Unlike `active-taps.js`' toggle — an
          // in-place per-tick updater that self-heals — these rows go through
          // `renderList`, and a rejected PUT moves no server value, so neither
          // the list `sig` nor the row's `itemSig` changes and `paintMode` is
          // never re-run from /api/state. The row would claim a mode the server
          // refused, and `modeIntent` then refuses the retry click because the
          // button already reads as pressed.
          paintMode(seg, was);
          throw e;
        }),
      { afterMutate, failMessage: (e) => `Tap mode change failed: ${e}` },
    );
  });
  wireRecPill(recPill, () => latest, { afterMutate });

  /**
   * @param {import('../../types.js').AppState} j
   * @param {import('../../types.js').Session | null} _session
   */
  const update = (j, _session) => {
    latest = j;
    const active = j.active || [];
    const liveCount = active.filter((a) => a.live !== false).length;
    const recEnabled = j.recording_enabled !== false;

    header(headHost, {
      eyebrow: "Global · Ingress",
      title: "Taps",
      sub: {
        sig: `${active.length}§${liveCount}`,
        build: () =>
          inline(
            `${active.length} connected · `,
            strong(`${liveCount} live`),
            " · per-tap gate & Person mapping",
          ),
      },
    });

    paintRecPill(recPill, recEnabled);

    // Reused components — each touches only its passed hosts.
    activeTaps.render(j, activeTapsCtx);

    liveChannel.render(j, {
      ...liveChannelHosts,
      liveCatalog,
      onAction: { start: onLiveStart, stop: onLiveStop },
    });

    // A keyed list: rows are mounted once per identity and mutated in place, so
    // a tap connecting does not churn every other row's buttons.
    // Write-only-when-changed: this is a per-tick in-place updater, and assigning
    // `textContent` replaces the node's children whether or not anything moved.
    const emptyText = active.length ? "" : "No taps connected.";
    if (modeEmpty.textContent !== emptyText) modeEmpty.textContent = emptyText;
    modeEmpty.hidden = active.length > 0;
    renderList(modeList, active, {
      key: (a) => a.identity,
      create: () => tpl("tpl-next-moderow").firstElementChild || document.createElement("div"),
      update: (node, a) => {
        const row = /** @type {HTMLElement} */ (node);
        row.dataset.identity = a.identity;
        pick(row, "mIdent").textContent = a.name || a.identity;
        pick(row, "mSrc").textContent = a.mode === "multi" ? "diarized" : "one voice";
        paintMode(pick(row, "mSeg"), a.mode === "multi" ? "multi" : "single");
      },
      itemSig: modeSig,
      sig: active.map(modeSig).join("|"),
    });
  };

  return { node: frag, update };
}

/** The mode a `.segctl` currently reads as — off `aria-pressed`, the state the
 * control actually exposes, not the class that styles it.
 * @param {Element} seg
 * @returns {"single" | "multi"} */
function modeOf(seg) {
  const on = /** @type {HTMLElement | null} */ (seg.querySelector('.tap-mode[aria-pressed="true"]'));
  return on?.dataset.mode === "multi" ? "multi" : "single";
}

/** Mark one mode as effective on a `.segctl`, in place.
 * @param {Element} seg
 * @param {"single" | "multi"} mode */
function paintMode(seg, mode) {
  for (const opt of seg.querySelectorAll(".tap-mode")) {
    const on = /** @type {HTMLElement} */ (opt).dataset.mode === mode;
    opt.classList.toggle("is-on", on);
    opt.setAttribute("aria-pressed", on ? "true" : "false");
  }
}
