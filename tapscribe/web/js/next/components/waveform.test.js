// Pure-logic tests for the waveform component's axis-tick helper. Node's
// built-in runner, no DOM — importing waveform.js is side-effect-free (it only
// touches the canvas / ResizeObserver / templates inside createWaveform).

import { test } from "node:test";
import assert from "node:assert/strict";

import { axisTicks } from "./waveform.js";

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
