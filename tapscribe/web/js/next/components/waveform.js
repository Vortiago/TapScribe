// @ts-check
// gate-allow: signal-listener — the seek listener attaches to the <canvas> THIS component builds and owns. It has no lifetime of its own: dropping the component's subtree drops the listener with it, and there is no document/window target here. Same reasoning as the host view's file-level allowance.
// Isolated canvas waveform renderer for the Recordings hero.
//
// Hand it server-computed peaks (api.wavePeaks.fetch → normalised [0,1]
// amplitudes) and it paints a mirrored bar waveform onto its own <canvas>
// plus a mm:ss time axis, redrawing on container resize. It knows NOTHING
// about /api/state, the poll, or fetching — the view owns the data flow and
// calls showWaveform / showMessage. Keeping it self-contained is deliberate:
// the cut overlay (a later slice) draws on top of the same canvas, and a
// renderer with no state coupling is the seam that makes that easy.

import { tpl, mount, pick } from "../../templates.js";
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
 * Where the playhead sits, as a percentage of the drawn width, or null when
 * there is no honest answer (no duration yet, no position). Pure: the playhead
 * is a transform-driven overlay ELEMENT, so its geometry needs no canvas — which
 * is the point, since repainting O(bins) peaks four times a second is the stall
 * `lastWaveSig` exists to prevent (ADR-0017).
 * @param {number} offsetS
 * @param {number} durationS
 * @returns {number | null}
 */
export function playheadPercent(offsetS, durationS) {
  if (!Number.isFinite(offsetS) || !Number.isFinite(durationS) || durationS <= 0) return null;
  return Math.min(100, Math.max(0, (offsetS / durationS) * 100));
}

/**
 * A click's x offset within the canvas box as a 0..1 fraction of its width, or
 * null when the box has no width (never laid out). Clamped, because a click on
 * the border can report a pixel outside.
 * @param {number} offsetX
 * @param {number} width
 * @returns {number | null}
 */
export function seekFractionFromClick(offsetX, width) {
  if (!Number.isFinite(offsetX) || !Number.isFinite(width) || width <= 0) return null;
  return Math.min(1, Math.max(0, offsetX / width));
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
 *   setPreview: (preview: { spans: import('../../types.js').CutSpan[], speech_floor_db: number } | null) => void,
 *   setPlayhead: (offsetS: number | null) => void,
 *   onSeek: (cb: (offsetS: number) => void) => void,
 * }}
 */
export function createWaveform() {
  const frag = tpl("tpl-next-waveform");
  const canvas = /** @type {HTMLCanvasElement} */ (pick(frag, "canvas"));
  const playhead = /** @type {HTMLElement} */ (pick(frag, "playhead"));
  const axisHost = pick(frag, "axis");
  const msgHost = pick(frag, "msg");
  const cutBadge = pick(frag, "cutBadge");
  const legend = pick(frag, "legend");

  /** @type {number[] | null} */
  let peaks = null;
  let durationS = 0;
  /** @type {import('../../types.js').CutSpan[] | null} */
  let cutSpans = null;
  /** @type {import('../../types.js').CutSpan[] | null} */
  let previewSpans = null;
  /** @type {number | null} */
  let previewFloorDb = null;
  /** Last playhead position written, so an unchanged frame writes no DOM.
   * `undefined` (not null) is the never-written sentinel. */
  /** @type {number | null | undefined} */
  let lastPct;

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

    // Cut overlays — #90 committed (solid) + #89 live preview (dashed).
    // The PREVIEW is the live thing being tuned: it owns the region shading
    // (kept tint + dropped dim), the dashed edge markers, and the
    // speech-floor guide. The COMMITTED cut always keeps its solid edge
    // ticks, but only dims dropped intervals when no preview is active —
    // two stacked shadings would be unreadable. An EMPTY preview spans
    // array is meaningful ("this cut drops everything") and dims it all.
    if (durationS > 0 && (cutSpans || previewSpans)) {
      /** @param {number} s */
      const xAt = (s) => Math.max(0, Math.min(cssW, (s / durationS) * cssW));
      /** @param {import('../../types.js').CutSpan[]} spans @param {number} alpha */
      const dimDropped = (spans, alpha) => {
        ctx.fillStyle = `rgba(0, 0, 0, ${alpha})`;
        let cursor = 0;
        for (const sp of spans) {
          const x0 = xAt(sp.start_s);
          const x1 = xAt(sp.end_s);
          if (x0 > cursor) ctx.fillRect(cursor, 0, x0 - cursor, cssH);
          cursor = Math.max(cursor, x1);
        }
        if (cursor < cssW) ctx.fillRect(cursor, 0, cssW - cursor, cssH);
      };
      /** @param {import('../../types.js').CutSpan[]} spans @param {string} color @param {boolean} dashed */
      const edgeLines = (spans, color, dashed) => {
        ctx.save();
        ctx.strokeStyle = color;
        ctx.lineWidth = 1;
        ctx.setLineDash(dashed ? [3, 3] : []);
        ctx.beginPath();
        for (const sp of spans) {
          for (const edge of [sp.start_s, sp.end_s]) {
            const x = Math.max(0.5, Math.min(cssW - 0.5, (edge / durationS) * cssW));
            ctx.moveTo(x, 0);
            ctx.lineTo(x, cssH);
          }
        }
        ctx.stroke();
        ctx.restore();
      };
      const cutColor = styles.getPropertyValue("--rec").trim() || "#d75d6a";
      const okColor = styles.getPropertyValue("--ok").trim() || "#69b76b";
      const infoColor = styles.getPropertyValue("--info").trim() || "#6ab0f3";
      if (previewSpans) {
        dimDropped(previewSpans, 0.45);
        ctx.save();
        ctx.globalAlpha = 0.16;
        ctx.fillStyle = okColor;
        for (const sp of previewSpans) {
          const x0 = xAt(sp.start_s);
          ctx.fillRect(x0, 0, xAt(sp.end_s) - x0, cssH);
        }
        ctx.restore();
        if (previewFloorDb != null) {
          // The floor knob as a horizontal guide: dBFS → normalised
          // amplitude, mirrored around the baseline like the bars.
          const dy = Math.max(1, Math.pow(10, previewFloorDb / 20) * (mid - 1));
          ctx.save();
          ctx.strokeStyle = infoColor;
          ctx.globalAlpha = 0.7;
          ctx.setLineDash([2, 4]);
          ctx.beginPath();
          ctx.moveTo(0, mid - dy);
          ctx.lineTo(cssW, mid - dy);
          ctx.moveTo(0, mid + dy);
          ctx.lineTo(cssW, mid + dy);
          ctx.stroke();
          ctx.restore();
        }
        edgeLines(previewSpans, cutColor, true);
      } else if (cutSpans && cutSpans.length) {
        dimDropped(cutSpans, 0.55);
      }
      if (cutSpans && cutSpans.length) edgeLines(cutSpans, cutColor, false);
    }
  };

  const renderAxis = () => {
    const out = document.createDocumentFragment();
    for (const label of axisTicks(durationS, AXIS_TICKS)) {
      const span = document.createElement("span");
      span.textContent = label;
      out.appendChild(span);
    }
    mount(axisHost, out);
  };

  /** Derive the overlay chrome — the data-cut-spans / data-previewSpans e2e
   * hooks, the ✂ badge, and the legend — from the current cutSpans /
   * previewSpans state. ONE owner, called by every mutation path below, so
   * the three can't fall out of agreement when a setter forgets a bit. */
  const syncChrome = () => {
    if (cutSpans) canvas.dataset.cutSpans = JSON.stringify(cutSpans);
    else delete canvas.dataset.cutSpans;
    if (previewSpans) canvas.dataset.previewSpans = JSON.stringify(previewSpans);
    else delete canvas.dataset.previewSpans;
    cutBadge.hidden = !cutSpans;
    legend.hidden = !(cutSpans || previewSpans);
  };

  /** Draw the waveform for one WAV's peaks + duration. `cut` (optional) is
   * the committed strip-silence cut to overlay — the kept {start_s, end_s}
   * spans; the canvas exposes it on data-cut-spans as a stable e2e hook. */
  /** @param {number[]} p @param {number} d @param {import('../../types.js').CutSpan[] | null} [cut] */
  const showWaveform = (p, d, cut) => {
    // A position measured against the OLD duration is void the moment the canvas
    // is asked to draw something else. The caller re-asserts a live position on
    // its next tick; while PAUSED no tick ever comes, so not clearing here
    // strands a playhead on a file the Player isn't holding (strict identity,
    // ADR-0017).
    setPlayhead(null);
    peaks = p;
    durationS = d;
    cutSpans = cut && cut.length ? cut : null;
    syncChrome();
    msgHost.textContent = "";
    msgHost.hidden = true;
    paint();
    renderAxis();
  };

  /** Clear the bars and show a centred message (empty / loading / error). The
   * canvas stays visible (baseline only) so the panel doesn't jump. */
  /** @param {string} text */
  const showMessage = (text) => {
    setPlayhead(null); // nothing drawn = no position to point at

    peaks = null;
    durationS = 0;
    cutSpans = null;
    previewSpans = null;
    previewFloorDb = null;
    syncChrome();
    msgHost.textContent = text;
    msgHost.hidden = false;
    axisHost.replaceChildren(); // static-render — one-shot clear under an error message
    paint();
  };

  /** Update (or clear) the live strip-preview overlay (#89) without
   * re-supplying peaks — the debounced knob path repaints in place. An
   * EMPTY spans array is meaningful ("this cut drops everything"); null
   * clears the preview entirely. */
  /** @param {{ spans: import('../../types.js').CutSpan[], speech_floor_db: number } | null} preview */
  const setPreview = (preview) => {
    previewSpans = preview ? preview.spans : null;
    previewFloorDb = preview ? preview.speech_floor_db : null;
    syncChrome();
    paint();
  };

  // Repaint on container resize so the bitmap tracks the CSS box — this also
  // covers the initial layout pass, where clientWidth is 0 in the microtask
  // the view mounts + first draws in.
  const ro = new ResizeObserver(() => paint());
  ro.observe(canvas);

  /** Draw (or erase) the playhead. `null` erases — the caller passes null
   * whenever the Player isn't holding the file this canvas is drawing, which is
   * the strict-identity rule: a position on another file's timeline would be a
   * confident lie (ADR-0017). Positioned by percentage on an overlay element, so
   * this never touches the canvas and never invalidates `lastWaveSig`.
   * @param {number | null} offsetS */
  const setPlayhead = (offsetS) => {
    const pct = offsetS == null ? null : playheadPercent(offsetS, durationS);
    // Called once per animation frame, including with an unchanged value — the
    // "playing a file this canvas isn't showing" case would otherwise re-assert
    // `hidden = true` 60 times a second. `null` is a real value here, so the
    // sentinel has to start as something else.
    if (pct === lastPct) return;
    lastPct = pct;
    if (pct == null) {
      playhead.hidden = true;
      return;
    }
    playhead.hidden = false;
    // translateX on a FULL-WIDTH element: a percentage translate resolves
    // against the element's own border box, so the playhead spans the canvas and
    // draws its 2px mark with a border (see .wave-playhead). That keeps the move
    // compositor-only — `left` would invalidate layout on every frame, which is
    // the cost this overlay exists to avoid.
    playhead.style.transform = `translateX(${pct}%)`;
  };

  /** Register the seek handler: a click on the waveform is a seek target on
   * whatever this canvas is currently showing. The component stays ignorant of
   * the Player and of sessions — it reports "this many seconds in" and the view
   * decides which file that is. */
  /** @param {(offsetS: number) => void} cb */
  const onSeek = (cb) => {
    canvas.addEventListener("click", (e) => {
      if (!peaks || durationS <= 0) return; // nothing drawn = nothing to seek
      const frac = seekFractionFromClick(e.offsetX, canvas.clientWidth);
      if (frac == null) return;
      cb(frac * durationS);
    });
  };

  return { node: frag, showWaveform, showMessage, setPreview, setPlayhead, onSeek };
}
