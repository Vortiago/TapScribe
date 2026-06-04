// @ts-check
// Stages · Taps (GLOBAL · Ingress). The global ingress stage: the connected
// /tap streams (level/lag/gate/in-flight buffer + rec/live toggles) and the
// speech-gate LiveConfig (gate_kind/threshold/hangover/pre-roll/min-speech +
// confidence), plus a clearly-tagged MOCK strip for the net-new Tap-model
// concepts (single/multi, voice→Person mapping, per-identity persistence)
// that the prototype designs but the backend doesn't yet expose.
//
// REUSES the classic dashboard components verbatim (imported, not copied):
//   - active-taps.js   → the connected-tap rows + rec/live toggle DOM
//   - live-channel.js  → the gate LiveConfig form + start/stop (→ /api/live/start)
//
// Built once for the page; `update(j, session)` re-runs the live-data
// components each poll tick — live-channel renders through renderRegion
// (focus-guarded, per-host signature), so an in-progress gate edit survives
// the tick and this view's host caches independently of Capture's. Only one
// view is mounted at a time, so reusing live-channel's fixed element ids here
// doesn't collide with Capture's.

import { tpl, pick } from "../../templates.js";
import { putJson, postJson } from "../../api.js";
import { header, strong, inline } from "../shell.js";
import * as activeTaps from "../../components/active-taps.js";
import * as liveChannel from "../../components/live-channel.js";

/**
 * @param {{
 *   liveCatalog: import('../../types.js').ModelCatalog,
 *   onLiveStart: () => void,
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
  const liveChannelHosts = {
    stateEl: pick(frag, "liveStateBadge"),
    mlxEl: pick(frag, "liveMlxNote"),
    bodyEl: pick(frag, "liveChannelBody"),
  };

  /** @type {import('../../types.js').AppState | null} */
  let latest = null;

  // Delegated rec/live toggle → PUT /api/tap-settings (same contract as the
  // classic dashboard's #activeTapsBody handler + Capture's). Bound once.
  activeTapsCtx.bodyEl.addEventListener("click", async (ev) => {
    const btn = /** @type {HTMLButtonElement | null} */ (
      /** @type {Element | null} */ (ev.target)?.closest(".tap-toggle"));
    if (!btn || btn.disabled) return;
    const identity = btn.dataset.identity;
    const which = btn.dataset.toggle;
    if (!identity || !which) return;
    const next = btn.dataset.state !== "1";
    btn.dataset.state = next ? "1" : "0";
    btn.classList.toggle("on", next);
    btn.disabled = true;
    try { await putJson("/api/tap-settings", { identity, [which]: next }); }
    catch (e) { alert(`Tap setting toggle failed: ${e}`); }
    finally { btn.disabled = false; afterMutate(); }
  });

  recPill.addEventListener("click", async () => {
    const enabled = !((latest?.recording_enabled ?? true) !== false);
    recPill.disabled = true;
    try { await postJson("/api/recording/toggle", { enabled }); }
    catch (e) { alert(`Recording toggle failed: ${e}`); }
    finally { recPill.disabled = false; afterMutate(); }
  });

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
      sub: inline(
        `${active.length} connected · `,
        strong(`${liveCount} live`),
        " · per-tap gate & Person mapping",
      ),
    });

    recPill.textContent = recEnabled ? "● recording" : "⏸ paused";
    recPill.classList.toggle("is-on", recEnabled);
    recPill.classList.toggle("is-paused", !recEnabled);

    // Reused components — each touches only its passed hosts.
    activeTaps.render(j, activeTapsCtx);

    liveChannel.render(j, {
      ...liveChannelHosts,
      mlxAvail: !!j.mlx_available,
      liveCatalog,
      onAction: { start: onLiveStart, stop: onLiveStop },
    });
  };

  return { node: frag, update };
}
