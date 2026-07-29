// @ts-check
// Pure client-side subtitle exporters. toSRT / toVTT take an array of timed
// segments and return a SubRip / WebVTT document string. No DOM, no backend.
// The Transcript view's srt/vtt download buttons are the consumers
// (js/next/views/transcript.js — buildExportSegments converts the merged
// schema's absolute ISO stamps to this module's relative seconds).

/** @typedef {{ start: number, end: number, text: string, speaker?: string }} Seg */

/** @param {number} v @param {number} [width] */
const pad = (v, width = 2) => String(v).padStart(width, "0");

/**
 * Format `seconds` as `HH:MM:SS<sep>mmm`. Rounds to whole milliseconds ONCE, so a
 * fractional part that rounds up to 1000 ms carries into the seconds field
 * (0.9999s -> 00:00:01,000, never 00:00:00,1000). `sep` is "," for SubRip, "." for WebVTT.
 * @param {number} seconds
 * @param {"," | "."} sep
 */
function fmtTime(seconds, sep) {
  const totalMs = Math.round(seconds * 1000);
  const ms = totalMs % 1000;
  const totalS = (totalMs - ms) / 1000;
  const h = Math.floor(totalS / 3600);
  const m = Math.floor((totalS % 3600) / 60);
  const s = totalS % 60;
  return `${pad(h)}:${pad(m)}:${pad(s)}${sep}${pad(ms, 3)}`;
}

/** Escape what a cue payload may not carry literally. WebVTT forbids a bare
 * `&` or `<` in a payload (character references are required), and an unknown
 * pseudo-tag is DROPPED by the parser — so a speaker rendered `<anon>` (the
 * merge layer's default key) or an operator alias like `R&D <lead>` silently
 * loses its attribution. Escaping `>` as well neutralises a literal `-->` in
 * transcript text, which would otherwise read as a cue timing line. SubRip has
 * no spec but its HTML-ish renderers (ffmpeg's srtdec, VLC) decode the same
 * three entities, so one escape serves both formats. `&` goes first or the
 * later replacements double-escape (`<` -> `&lt;` -> `&amp;lt;`).
 * @param {string} s */
const esc = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

/** @param {Seg} seg */
const line = (seg) => (seg.speaker ? `${esc(seg.speaker)}: ${esc(seg.text)}` : esc(seg.text));

/**
 * Render segments as a SubRip (.srt) document. Empty input -> "".
 * @param {Seg[]} segments
 */
export function toSRT(segments) {
  return segments
    .map((seg, i) => `${i + 1}\n${fmtTime(seg.start, ",")} --> ${fmtTime(seg.end, ",")}\n${line(seg)}`)
    .join("\n\n");
}

/**
 * Render segments as a WebVTT (.vtt) document. Empty input -> "WEBVTT\n".
 * @param {Seg[]} segments
 */
export function toVTT(segments) {
  const cues = segments
    .map((seg) => `${fmtTime(seg.start, ".")} --> ${fmtTime(seg.end, ".")}\n${line(seg)}`)
    .join("\n\n");
  return cues ? `WEBVTT\n\n${cues}` : "WEBVTT\n";
}
