// @ts-check
// Pure client-side subtitle exporters. toSRT / toVTT take an array of timed
// segments and return a SubRip / WebVTT document string. No DOM, no backend —
// wiring a download button into the Transcript view is a separate follow-on (#208).

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

/** @param {Seg} seg */
const line = (seg) => (seg.speaker ? `${seg.speaker}: ${seg.text}` : seg.text);

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
