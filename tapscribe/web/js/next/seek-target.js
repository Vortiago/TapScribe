// @ts-check
// Seek-target resolution — the one place a merged transcript segment becomes
// "play THIS file, THIS many seconds in".
//
// A **seek target** names an exact file (CONTEXT.md · Player · seek target).
// Every merged segment carries `source_wav`, the file its words actually came
// out of, so a line from a stripped-source transcript plays the clip and lands
// on the right syllable. A segment is never projected onto a different file's
// timeline: clips ARE nested under an owner original in the listing, but that
// nesting is a display fallback (an orphaned clip is deliberately shown under a
// non-owner), so owner arithmetic would be silently wrong for orphans.
// ADR-0017 records the rejected alternative.
//
// DOM-free and pure so it's unit-testable under `node --test`.

/**
 * @typedef {{ name: string, source: "original" | "stripped", offsetS: number }} SeekTarget
 */

/**
 * Resolve a merged segment to a seek target, or null when `sourceWav` isn't in
 * the listing at all (a deleted WAV, or a stripped-source transcript after
 * `clear stripped`). Callers resolve at CLICK time and report the null case;
 * pre-disabling would mean gating on `files_sig`, and a hand-maintained render
 * signature with a forgotten dependency goes stale invisibly.
 *
 * @param {string} sourceWav filename from the segment's `source_wav`
 * @param {string} absStartIso the segment's wall-clock `abs_start`
 * @param {any[]} files the session listing (originals, each with `regions`)
 * @returns {SeekTarget | null}
 */
export function resolveSeekTarget(sourceWav, absStartIso, files) {
  const originals = files || [];
  const original = originals.find((f) => f.name === sourceWav);
  if (original) {
    return { name: original.name, source: "original", offsetS: offsetWithin(original, absStartIso) };
  }
  // Which ARRAY the name was found in is what decides `source`, so the caller
  // needs no hint from the transcript about which audio it was run against.
  for (const f of originals) {
    const clip = (f.regions || []).find((/** @type {any} */ r) => r.name === sourceWav);
    if (clip) {
      return { name: clip.name, source: "stripped", offsetS: offsetWithin(clip, absStartIso) };
    }
  }
  return null;
}

/**
 * Seconds from a file's start to `absStartIso`. Clamped at zero: a segment can
 * never begin before the file it was transcribed from, so a negative result is
 * clock noise, not a position.
 * @param {any} file
 * @param {string} absStartIso
 */
function offsetWithin(file, absStartIso) {
  const start = Date.parse(file.wav_start || "");
  const at = Date.parse(absStartIso || "");
  if (!Number.isFinite(start) || !Number.isFinite(at)) return 0;
  return Math.max(0, (at - start) / 1000);
}
