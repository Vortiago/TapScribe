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
// the tick and this view's host caches independently of Capture's.
// live-channel reads its form back out of the bodyEl it was rendered into
// (not the global document — see #254), so this view's instance and
// Capture's don't collide even though both host elements can be alive in
// main.js's viewCache at once.

import { tpl, pick } from "../../templates.js";
import { header, strong, inline, wireRecPill, paintRecPill } from "../shell.js";
import * as activeTaps from "../../components/active-taps.js";
import * as liveChannel from "../../components/live-channel.js";

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
  const liveChannelHosts = {
    stateEl: pick(frag, "liveStateBadge"),
    mlxEl: pick(frag, "liveMlxNote"),
    bodyEl: pick(frag, "liveChannelBody"),
  };

  /** @type {import('../../types.js').AppState | null} */
  let latest = null;

  activeTaps.wireToggles(activeTapsCtx.bodyEl, { afterMutate });
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
  };

  return { node: frag, update };
}
