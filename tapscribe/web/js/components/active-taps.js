// @ts-check
// Active taps panel — one row per live audio source the recorder is
// currently receiving bytes from.

import { tpl, mount, pick } from "../templates.js";
import { speakerIndex } from "../speakers.js";
import { fmtBytes, fmtDur, truncMid } from "../formatters.js";

/**
 * @param {import('../types.js').AppState} j
 * @param {import('../types.js').ActiveTapsCtx} ctx
 */
export function render(j, { countEl, badgeEl, bodyEl }) {
  const list = j.active || [];
  countEl.textContent = String(list.length);

  if (!list.length) {
    mount(badgeEl, tpl("tpl-active-badge-idle"));
    mount(bodyEl, tpl("tpl-active-empty"));
    return;
  }
  mount(badgeEl, tpl("tpl-active-badge-capturing"));

  const frag = document.createDocumentFragment();
  for (const a of list) {
    const dur = (a.bytes_received || 0) / 32000;
    // Settings default to true when the server didn't include them (older
    // payload, or first-ever sighting of this identity).
    const recOn = a.record !== false;
    const liveOn = a.live !== false;
    const ident = a.identity || "";
    const filename = a.filename || "";

    const node = tpl("tpl-stream-row");
    const row = /** @type {HTMLElement} */ (node.querySelector(".stream-row"));

    const marker = pick(row, "spkMarker");
    marker.dataset.spk = String(speakerIndex(a.name || ident));

    pick(row, "name").textContent = a.name || "<anon>";
    const identEl = pick(row, "ident");
    identEl.title = filename;
    identEl.textContent = `${ident} · ${truncMid(filename, 30)}`;

    pick(row, "size").textContent = fmtBytes(a.bytes_received || 0);
    pick(row, "dur").textContent = `~${fmtDur(dur)}`;

    // Volume meter — peak amplitude of recent PCM, 0.0–1.0 from
    // `int16_peak_norm` on the server side, peak-held over ~200 ms.
    // Quiet (<5%) shows muted gray so a silent-but-open WS doesn't look
    // like it's actively streaming; the green→amber→red colour shifts
    // come from CSS via the data-zone attribute. Below ~1% we treat as
    // silent and snap the bar to zero width so it doesn't show a
    // confusing sliver under near-silent rooms.
    const level = Math.max(0, Math.min(1, Number(a.level) || 0));
    const meter = pick(row, "meter");
    const fill = pick(row, "meterFill");
    const pct = level < 0.01 ? 0 : Math.round(level * 100);
    fill.style.width = pct + "%";
    let zone = "silent";
    if (level >= 0.85) zone = "clip";
    else if (level >= 0.6) zone = "hot";
    else if (level >= 0.05) zone = "ok";
    meter.dataset.zone = zone;
    meter.setAttribute(
      "aria-label",
      `volume ${pct}% (${zone})`,
    );

    // Per-tap lag from the relay (remaining_time_transcription). Hidden
    // when live is off or the relay hasn't reported yet — there's no
    // useful value to show in those states.
    const lagEl = pick(row, "lag");
    const lag = typeof a.lag_s === "number" ? a.lag_s : null;
    if (lag === null || !liveOn) {
      lagEl.hidden = true;
    } else {
      lagEl.hidden = false;
      lagEl.textContent = `lag ${lag.toFixed(1)}s`;
      lagEl.classList.toggle("lag-warn", lag >= 0.5 && lag < 2);
      lagEl.classList.toggle("lag-bad", lag >= 2);
    }

    for (const btn of /** @type {NodeListOf<HTMLButtonElement>} */ (row.querySelectorAll(".tap-toggle"))) {
      const which = btn.dataset.toggle;
      const on = which === "record" ? recOn : liveOn;
      btn.dataset.identity = ident;
      btn.dataset.state = on ? "1" : "0";
      btn.classList.toggle("on", on);
    }

    // Three-state status line under each tap row:
    //   ⟳ <text>      — model is transcribing the latest hypothesis
    //   ⟳ listening…  — audio is being forwarded but nothing decoded yet
    //   ⏸ quiet       — gate is closed (no speech detected)
    // Hidden when LIVE is off, or when we have nothing meaningful to show
    // (e.g. backend-gate mode with no buffer text — we can't tell what
    // the backend's own VAD is doing, so we stay silent).
    const bufRow = pick(node, "bufferRow");
    const bufText = pick(node, "bufferText");
    const bufIcon = pick(node, "bufferIcon");
    const buf = (a.buffer_transcription || "").trim();
    const gateOpen = !!a.gate_open;
    if (bufRow && bufText && bufIcon) {
      let icon = "";
      let text = "";
      let cls = "";
      if (liveOn) {
        if (buf) {
          icon = "⟳"; text = buf; cls = "buf-active";
        } else if (gateOpen) {
          icon = "⟳"; text = "listening…"; cls = "buf-listening";
        } else {
          icon = "⏸"; text = "quiet"; cls = "buf-quiet";
        }
      }
      if (text) {
        bufRow.hidden = false;
        bufIcon.textContent = icon;
        bufText.textContent = text;
        bufRow.className = "stream-buffer " + cls;
      } else {
        bufRow.hidden = true;
        bufText.textContent = "";
      }
    }

    frag.appendChild(node);
  }
  bodyEl.replaceChildren(frag);
}
