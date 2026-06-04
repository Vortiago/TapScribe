// Pure-logic tests for the Transcript view's cache helpers: recordingFor maps
// a selected file to its recording, and recordingVariants flattens a
// recording's original + stripped-region cached transcripts into one tagged
// list. Node's built-in runner, no DOM — importing transcript.js is
// side-effect-free (it only references `document` inside functions).

import { test } from "node:test";
import assert from "node:assert/strict";

import { recordingFor, recordingVariants } from "./transcript.js";

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
