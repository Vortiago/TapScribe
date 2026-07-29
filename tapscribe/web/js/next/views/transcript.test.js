// Pure-logic tests for the Transcript view's cache helpers: recordingFor maps
// a selected file to its recording, and recordingVariants flattens a
// recording's original + stripped-region cached transcripts into one tagged
// list. Plus buildExportSegments, the subtitle-export clock: the merged
// schema's absolute ISO stamps -> the relative seconds toSRT/toVTT take.
// Node's built-in runner, no DOM — importing transcript.js is
// side-effect-free (it only references `document` inside functions).

import { test } from "node:test";
import assert from "node:assert/strict";

import { recordingFor, recordingVariants, buildExportSegments } from "./transcript.js";
import { toSRT } from "../subtitles.js";

const wav = (name, transcripts = [], regions = []) => ({
  name, size: 0, duration_s: 1, transcript: null, transcripts,
  wav_start: null, wav_end: null, speaker_name: "", regions,
});
const region = (name, transcripts = []) => ({
  name, size: 0, duration_s: 1, transcript: null, transcripts,
  wav_start: null, wav_end: null, speaker_name: "",
});
const variant = (backend, model, source, is_primary = false) => ({ backend, model, source, is_primary });

test("recordingFor returns the original when the original itself is selected", () => {
  const orig = wav("a.wav");
  assert.equal(recordingFor(orig, [orig, wav("b.wav")]), orig);
});

test("recordingFor maps a selected region clip back to its parent original", () => {
  const r = region("a-r0.wav");
  const orig = wav("a.wav", [], [r]);
  assert.equal(recordingFor(r, [wav("z.wav"), orig]), orig);
});

test("recordingFor falls back to the selection when no parent is found", () => {
  const orphan = region("orphan.wav");
  assert.equal(recordingFor(orphan, [wav("a.wav")]), orphan);
});

test("recordingFor returns null for a null selection", () => {
  assert.equal(recordingFor(null, [wav("a.wav")]), null);
});

test("recordingVariants unions original + region variants, each tagged with its file", () => {
  const r0 = region("a-r0.wav", [variant("parakeet", "v2", "stripped", true)]);
  const r1 = region("a-r1.wav", [variant("parakeet", "v2", "stripped", true)]);
  const orig = wav("a.wav", [variant("whisper", "small.en", "original", true)], [r0, r1]);

  const variants = recordingVariants(orig);
  assert.equal(variants.length, 3);
  // The original's own variant comes first, tagged "original" + carrying its name.
  assert.deepEqual(
    { file: variants[0].file, source: variants[0].source, backend: variants[0].backend },
    { file: "a.wav", source: "original", backend: "whisper" },
  );
  // Then each region clip's variants, tagged "stripped" + carrying the clip name.
  assert.deepEqual(variants.slice(1).map((v) => [v.file, v.source]), [
    ["a-r0.wav", "stripped"],
    ["a-r1.wav", "stripped"],
  ]);
});

test("recordingVariants is identical whether you select the original or a region", () => {
  // The operator's ask: the cache list must not change when you flip the
  // Original/Stripped toggle (which swaps the selected file original↔region).
  const r0 = region("a-r0.wav", [variant("parakeet", "v2", "stripped", true)]);
  const orig = wav("a.wav", [variant("whisper", "small.en", "original", true)], [r0]);
  const files = [orig];

  const fromOriginal = recordingVariants(recordingFor(orig, files));
  const fromRegion = recordingVariants(recordingFor(r0, files));
  assert.deepEqual(fromRegion, fromOriginal);
});

test("recordingVariants returns [] for null and for a region with no sub-regions", () => {
  assert.deepEqual(recordingVariants(null), []);
  assert.deepEqual(recordingVariants(region("solo.wav")), []);
});

// ---- buildExportSegments: the subtitle cue clock -------------------------
// The trap this slice is built around: the merged schema carries abs_start /
// abs_end as absolute ISO strings, while toSRT/toVTT take seconds as numbers.
// The e2e contract drives two well-formed segments through a browser; these
// pin the edges it cannot see.

const seg = (abs_start, abs_end, text, speaker = "Spk0") => ({
  abs_start, abs_end, speaker, text, source_wav: "a.wav", low_confidence: false,
});
const merged = (segments) => ({ segments, plain_text: "", suppressed: [] });
const meta = (aliases = {}) => ({ aliases });

test("buildExportSegments re-bases ISO stamps onto a relative clock and applies aliases", () => {
  const out = buildExportSegments(
    merged([
      seg("2025-02-01T09:00:00+00:00", "2025-02-01T09:00:03+00:00", "first", "Spk0"),
      seg("2025-02-01T09:00:05+00:00", "2025-02-01T09:00:08+00:00", "second", "Spk1"),
    ]),
    meta({ Spk0: "Ms. Smith", Spk1: "Mr. Jones" }),
  );
  assert.deepEqual(out, [
    { start: 0, end: 3, text: "first", speaker: "Ms. Smith" },
    { start: 5, end: 8, text: "second", speaker: "Mr. Jones" },
  ]);
});

test("t=0 is the first EMITTED segment: a leading empty-text segment does not anchor the clock", () => {
  // The skip-then-anchor ORDER is load-bearing — anchoring on the skipped
  // segment would shift every cue while the pinned inter-cue DELTAS still pass.
  const out = buildExportSegments(
    merged([
      seg("2025-02-01T09:00:00+00:00", "2025-02-01T09:00:02+00:00", ""),
      seg("2025-02-01T09:00:10+00:00", "2025-02-01T09:00:13+00:00", "first real line"),
      seg("2025-02-01T09:00:15+00:00", "2025-02-01T09:00:18+00:00", "second real line"),
    ]),
    meta(),
  );
  assert.deepEqual(out.map((s) => [s.start, s.end]), [[0, 3], [5, 8]]);
});

test("a single-segment transcript starts at 00:00:00,000", () => {
  const out = buildExportSegments(
    merged([seg("2025-02-01T09:17:42+00:00", "2025-02-01T09:17:44+00:00", "only line")]),
    meta(),
  );
  assert.deepEqual(out, [{ start: 0, end: 2, text: "only line", speaker: "Spk0" }]);
  assert.equal(toSRT(out), "1\n00:00:00,000 --> 00:00:02,000\nSpk0: only line");
});

test("sub-second offsets survive to the millisecond in the exported cue stamps", () => {
  const out = buildExportSegments(
    merged([
      seg("2025-02-01T09:00:00.250+00:00", "2025-02-01T09:00:00.999+00:00", "a"),
      seg("2025-02-01T09:00:01.750+00:00", "2025-02-01T09:00:03.001+00:00", "b"),
    ]),
    meta(),
  );
  // Subtracting epoch SECONDS is not exact in binary floating point (0.749
  // comes out as 0.7490000724…), so the pin is on the rendered stamps — the
  // bytes a player actually reads — not on the intermediate numbers.
  assert.equal(
    toSRT(out),
    "1\n00:00:00,000 --> 00:00:00,749\nSpk0: a\n\n2\n00:00:01,500 --> 00:00:02,751\nSpk0: b",
  );
});

test("an unparseable abs_start drops ITS cue instead of NaN-poisoning the whole file", () => {
  // NaN !== null, so a NaN start would latch as tZero and every later
  // `absStart - tZero` would be NaN too — one corrupt sidecar value garbling a
  // whole plausible-looking .srt.
  const out = buildExportSegments(
    merged([
      seg("", "2025-02-01T09:00:03+00:00", "corrupt start"),
      seg("2025-02-01T09:00:10+00:00", "2025-02-01T09:00:13+00:00", "good"),
      seg("2025-02-01T09:00:20+00:00", "not-a-date", "corrupt end"),
    ]),
    meta(),
  );
  assert.deepEqual(out.map((s) => [s.text, s.start, s.end]), [
    ["good", 0, 3],
    // A parseable start with a corrupt end keeps its line as a zero-length cue.
    ["corrupt end", 10, 10],
  ]);
  assert.ok(!toSRT(out).includes("NaN"), "a corrupt stamp must not reach the cue timings");
});

test("buildExportSegments tolerates a body with no segments at all", () => {
  assert.deepEqual(buildExportSegments({ plain_text: "", suppressed: [] }, meta()), []);
  assert.deepEqual(buildExportSegments(merged([]), {}), []);
});
