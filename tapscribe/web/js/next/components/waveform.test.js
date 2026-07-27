// Pure-logic tests for the waveform component's axis-tick helper. Node's
// built-in runner, no DOM — importing waveform.js is side-effect-free (it only
// touches the canvas / ResizeObserver / templates inside createWaveform).

import { test } from "node:test";
import assert from "node:assert/strict";

import { axisTicks, playheadPercent, seekFractionFromClick } from "./waveform.js";

test("axisTicks spans 0 to duration inclusive, formatted mm:ss", () => {
  // 120 s over 5 ticks → 0, 30, 60, 90, 120 s.
  assert.deepEqual(axisTicks(120, 5), ["0:00", "0:30", "1:00", "1:30", "2:00"]);
});

test("axisTicks pads seconds to two digits", () => {
  assert.deepEqual(axisTicks(20, 5), ["0:00", "0:05", "0:10", "0:15", "0:20"]);
});

test("axisTicks always emits a start and end label (count clamped to >= 2)", () => {
  assert.deepEqual(axisTicks(60, 1), ["0:00", "1:00"]);
  assert.deepEqual(axisTicks(60, 0), ["0:00", "1:00"]);
});

test("axisTicks degrades a zero / non-finite duration to all 0:00", () => {
  assert.deepEqual(axisTicks(0, 3), ["0:00", "0:00", "0:00"]);
  assert.deepEqual(axisTicks(NaN, 3), ["0:00", "0:00", "0:00"]);
  assert.deepEqual(axisTicks(-5, 3), ["0:00", "0:00", "0:00"]);
});

// ── Playhead geometry (#191) ───────────────────────────────────────────────
// Pure so the position maths is testable without a canvas: the playhead is a
// transform-driven overlay element, never a canvas repaint (ADR-0017).

test("playheadPercent maps a position onto the drawn width", () => {
  assert.equal(playheadPercent(0, 60), 0);
  assert.equal(playheadPercent(30, 60), 50);
  assert.equal(playheadPercent(60, 60), 100);
});

test("playheadPercent has no answer without a real duration", () => {
  // A file whose metadata hasn't landed has duration 0/NaN. Drawing at 0% would
  // claim a position we don't have.
  assert.equal(playheadPercent(5, 0), null);
  assert.equal(playheadPercent(5, NaN), null);
  assert.equal(playheadPercent(NaN, 60), null);
});

test("playheadPercent clamps rather than drawing off the waveform", () => {
  // A seek past the end (a segment stamped beyond its file, a clip shorter than
  // the transcript claims) must sit at the edge, not outside the box.
  assert.equal(playheadPercent(90, 60), 100);
  assert.equal(playheadPercent(-5, 60), 0);
});

test("seekFractionFromClick converts a click x into a fraction of the width", () => {
  assert.equal(seekFractionFromClick(0, 200), 0);
  assert.equal(seekFractionFromClick(100, 200), 0.5);
  assert.equal(seekFractionFromClick(200, 200), 1);
  // Clamped: a click on the border can report a hair outside.
  assert.equal(seekFractionFromClick(-3, 200), 0);
  assert.equal(seekFractionFromClick(999, 200), 1);
  // A zero-width box (never laid out) has no meaningful fraction.
  assert.equal(seekFractionFromClick(10, 0), null);
});
