// @ts-check
// Isolated canvas waveform renderer for the Recordings hero.
//
// Hand it server-computed peaks (api.fetchWavePeaks → normalised [0,1]
// amplitudes) and it paints a mirrored bar waveform onto its own <canvas>
// plus a mm:ss time axis, redrawing on container resize. It knows NOTHING
// about /api/state, the poll, or fetching — the view owns the data flow and
// calls showWaveform / showMessage. Keeping it self-contained is deliberate:
// the cut overlay (a later slice) draws on top of the same canvas, and a
// renderer with no state coupling is the seam that makes that easy.

import { tpl, pick } from "../../templates.js";
import { fmtMmSs } from "../../formatters.js";

/** How many time-axis ticks to label under the waveform. */
const AXIS_TICKS = 5;

/**
 * Evenly-spaced mm:ss tick labels from 0 to `durationS` inclusive of both
 * ends. Pure (no DOM) so it's unit-testable in isolation. `count` is clamped
 * to >= 2 so there's always a start and an end label; `fmtMmSs` collapses a
 * non-positive / non-finite tick to "0:00", so a bogus duration degrades
 * cleanly.
 * @param {number} durationS
 * @param {number} count
 * @returns {string[]}
 */
export function axisTicks(durationS, count) {
  const n = Math.max(2, Math.floor(count));
  /** @type {string[]} */
  const out = [];
  for (let i = 0; i < n; i++) out.push(fmtMmSs((durationS * i) / (n - 1)));
  return out;
}

/**
 * Build an isolated waveform component. Returns the node to mount plus
 * imperative draw methods. The component retains the last peaks so a
 * ResizeObserver repaint (container width change, or the initial layout pass
 * where clientWidth is still 0) redraws without the caller re-supplying data.
 * @returns {{
 *   node: DocumentFragment,
 *   showWaveform: (peaks: number[], durationS: number, cut?: import('../../types.js').CutSpan[] | null) => void,
 *   showMessage: (text: string) => void,
 * }}
 */
export function createWaveform() {
  const frag = tpl("tpl-next-waveform");
  const canvas = /** @type {HTMLCanvasElement} */ (pick(frag, "canvas"));
  const axisHost = pick(frag, "axis");
  const msgHost = pick(frag, "msg");
  const cutBadge = pick(frag, "cutBadge");

  /** @type {number[] | null} */
  let peaks = null;
  let durationS = 0;
  /** @type {import('../../types.js').CutSpan[] | null} */
  let cutSpans = null;

  const paint = () => {
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    const dpr = window.devicePixelRatio || 1;
    const cssW = canvas.clientWidth;
    const cssH = canvas.clientHeight;
    // Not laid out yet (mount microtask) — the ResizeObserver fires again
    // once the box has a size, so bail rather than draw into a 0×0 bitmap.
    if (cssW <= 0 || cssH <= 0) return;
    canvas.width = Math.round(cssW * dpr);
    canvas.height = Math.round(cssH * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, cssW, cssH);

    const styles = getComputedStyle(canvas);
    const accent = styles.getPropertyValue("--next-accent").trim() || "#6ab0f3";
    const hair = styles.getPropertyValue("--hairline").trim() || "rgba(255,255,255,0.12)";

    const mid = cssH / 2;
    // Baseline centre line so a silent / message-state waveform still reads
    // as a waveform rather than an empty box.
    ctx.fillStyle = hair;
    ctx.fillRect(0, mid - 0.5, cssW, 1);

    if (!peaks || peaks.length === 0) return;
    ctx.fillStyle = accent;
    const n = peaks.length;
    const bw = cssW / n;
    for (let i = 0; i < n; i++) {
      const p = peaks[i] ?? 0;
      const h = Math.max(0.5, p * (mid - 1));
      const x = i * bw;
      const w = bw > 1.5 ? bw - 0.5 : bw;
      ctx.fillRect(x, mid - h, w, h * 2);
    }

    if (cutSpans && cutSpans.length && durationS > 0) {
      // Committed strip-silence cut (#90): dim the DROPPED intervals (the
      // complement of the kept spans) and mark each kept edge with a SOLID
      // 1px tick — solid = committed-on-disk; the live knob preview (#89)
      // gets dashed markers so the two read differently at a glance.
      const cutColor = styles.getPropertyValue("--rec").trim() || "#d75d6a";
      ctx.fillStyle = "rgba(0, 0, 0, 0.55)";
      let cursor = 0;
      for (const sp of cutSpans) {
        const x0 = Math.max(0, Math.min(cssW, (sp.start_s / durationS) * cssW));
        const x1 = Math.max(0, Math.min(cssW, (sp.end_s / durationS) * cssW));
        if (x0 > cursor) ctx.fillRect(cursor, 0, x0 - cursor, cssH);
        cursor = Math.max(cursor, x1);
      }
      if (cursor < cssW) ctx.fillRect(cursor, 0, cssW - cursor, cssH);
      ctx.fillStyle = cutColor;
      for (const sp of cutSpans) {
        for (const edge of [sp.start_s, sp.end_s]) {
          const x = Math.max(0, Math.min(cssW - 1, (edge / durationS) * cssW));
          ctx.fillRect(x, 0, 1, cssH);
        }
      }
    }
  };

  const renderAxis = () => {
    const out = document.createDocumentFragment();
    for (const label of axisTicks(durationS, AXIS_TICKS)) {
      const span = document.createElement("span");
      span.textContent = label;
      out.appendChild(span);
    }
    axisHost.replaceChildren(out);
  };

  /** Draw the waveform for one WAV's peaks + duration. `cut` (optional) is
   * the committed strip-silence cut to overlay — the kept {start_s, end_s}
   * spans; the canvas exposes it on data-cut-spans as a stable e2e hook. */
  /** @param {number[]} p @param {number} d @param {import('../../types.js').CutSpan[] | null} [cut] */
  const showWaveform = (p, d, cut) => {
    peaks = p;
    durationS = d;
    cutSpans = cut && cut.length ? cut : null;
    if (cutSpans) canvas.dataset.cutSpans = JSON.stringify(cutSpans);
    else delete canvas.dataset.cutSpans;
    cutBadge.hidden = !cutSpans;
    msgHost.textContent = "";
    msgHost.hidden = true;
    paint();
    renderAxis();
  };

  /** Clear the bars and show a centred message (empty / loading / error). The
   * canvas stays visible (baseline only) so the panel doesn't jump. */
  /** @param {string} text */
  const showMessage = (text) => {
    peaks = null;
    durationS = 0;
    cutSpans = null;
    delete canvas.dataset.cutSpans;
    cutBadge.hidden = true;
    msgHost.textContent = text;
    msgHost.hidden = false;
    axisHost.replaceChildren();
    paint();
  };

  // Repaint on container resize so the bitmap tracks the CSS box — this also
  // covers the initial layout pass, where clientWidth is 0 in the microtask
  // the view mounts + first draws in.
  const ro = new ResizeObserver(() => paint());
  ro.observe(canvas);

  return { node: frag, showWaveform, showMessage };
}
