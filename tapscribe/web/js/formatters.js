// @ts-check
// Pure formatters used across the dashboard — the APP layer next to the
// vendored toolkit formatters (lib/format.js): fmtClock renders viewer-zone
// wall clock via its own module-scope Intl formatter (see its docstring); the
// dense terminal-style number/duration forms (fmtDur, fmtMs, fmtMmSs,
// fmtBytes) are a deliberate Stages aesthetic and stay hand-rolled.
// No DOM dependency, no shared state — safe to unit-test in isolation.
// (escapeHtml/cssEscape/fmtElapsed* were removed with the classic dashboard —
// their only callers were classic main.js / ribbon.js / session-detail.js.)

// Unit choice happens on the ROUNDED value, not the raw one. Testing the raw
// value first and rounding afterwards renders an impossible carry at every unit
// boundary — 1048570 B is below 1 MB but rounds to "1024.0 KB", and 119.96 s
// rounded to one decimal is "1m 60.0s". Both windows are crossed on a live tap:
// active-taps.js renders a growing duration AND a growing byte count on every
// ~500 ms poll, so each row passes through [1048524, 1048576) B and
// [59.95, 60) s on its way up.

/** @param {number | null | undefined} b */
export function fmtBytes(b) {
  if (b == null || !isFinite(b)) return "0 B";
  if (b < 1024) return b + " B";
  const kb = b / 1024;
  if (Number(kb.toFixed(1)) < 1024) return kb.toFixed(1) + " KB";
  return (b / 1024 / 1024).toFixed(2) + " MB";
}

/** @param {number | null | undefined} s */
export function fmtDur(s) {
  if (s == null || !isFinite(s)) return "?";
  if (Number(s.toFixed(2)) < 60) return s.toFixed(2) + " s";
  const mins = Math.floor(s / 60);
  const rem = s % 60;
  // The seconds part rounding UP to a full minute carries into the minutes.
  if (Number(rem.toFixed(1)) >= 60) return `${mins + 1}m 0.0s`;
  return `${mins}m ${rem.toFixed(1)}s`;
}

/** @param {number | null | undefined} ms */
export function fmtMs(ms) {
  if (ms == null || !isFinite(ms)) return "?";
  // No carry guard needed here: the sub-1000 branch prints ms verbatim rather
  // than rounding it, so no value can round up INTO the next unit.
  return ms < 1000 ? ms + " ms" : (ms / 1000).toFixed(1) + " s";
}

/** Wall-clock hh:mm:ss for an absolute instant (ISO-with-offset), rendered in
 * the VIEWER's timezone (toolkit browser-timezone rule) — the old version
 * sliced the ISO string, which showed every viewer the origin-encoded (UTC)
 * wall time. The instant is unambiguous; the viewer's zone is the right
 * display zone. Uses a module-scope Intl formatter instead of lib/format.js's
 * `time()`: fmtClock runs once per merged-transcript segment (thousands per
 * rebuild), and dfmt() only reference-caches its own two singletons — a
 * custom options object pays a JSON.stringify cache-key per call.
 * @param {string | null | undefined} iso */
export function fmtClock(iso) {
  if (!iso) return "?";
  // Guard unparseable timestamps: Intl's format() throws RangeError on an
  // Invalid Date, and fmtClock runs inside whole-transcript row loops — a
  // single corrupt sidecar value must garble one cell ("?"), never abort the
  // entire render/copy (the old slice(11,19) form never threw either).
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "?";
  return CLOCK_FMT.format(d);
}
const CLOCK_FMT = new Intl.DateTimeFormat(undefined, {
  hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
});

/**
 * Elapsed seconds → compact "m:ss" (90 → "1:30"). Distinct from `fmtDur`
 * (human "1m 30.0s") and `fmtClock` (ISO wall-clock slice): this is the
 * transcript-timestamp / waveform-axis form. Floors to whole seconds and
 * treats a non-positive or non-finite input as 0 so callers needn't guard.
 * @param {number} seconds
 */
export function fmtMmSs(seconds) {
  const s = seconds > 0 && isFinite(seconds) ? seconds : 0;
  return `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
}

/**
 * Elide the MIDDLE of an over-long string, keeping both ends: the filename form
 * (`averylongfilename.wav` → `avery…e.wav`). Never returns more than `max`
 * characters.
 * @param {string | null | undefined} s
 * @param {number} max
 */
export function truncMid(s, max) {
  if (!s) return "";
  if (s.length <= max) return s;
  if (max <= 1) return "…";
  const half = Math.floor((max - 1) / 2);
  // At max = 2 the tail slice is `s.slice(-0)` — which is the WHOLE string, so
  // the "truncation" came back LONGER than the input. Keep a head + the ellipsis.
  if (half === 0) return s.slice(0, max - 1) + "…";
  return s.slice(0, half) + "…" + s.slice(-half);
}

// "2026-05-12T09-19-55Z" → "05-12 09:19"
/** @param {string | null | undefined} s */
export function fmtSessionLabel(s) {
  if (!s || s.length < 16) return s || "";
  return s.slice(5, 10) + " " + s.slice(11, 13) + ":" + s.slice(14, 16);
}
