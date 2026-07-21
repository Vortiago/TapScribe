// Unit tests for the pure dashboard formatters (run via `node --test`, no DOM).
//
// formatters.js is pure and DOM-free — this repo's own criterion for
// `node --test` coverage — and had none at all, so the unit-boundary carry bugs
// ("1m 60.0s", "1024.0 KB") shipped unnoticed even though active-taps.js renders
// both formatters on every ~500 ms poll of a live tap and therefore crosses
// every boundary window on the way up.
//
// Every expectation is a LITERAL string, never a value recomputed the way the
// implementation computes it — a test that re-derives `(b / 1024).toFixed(1)`
// would agree with the bug it is supposed to catch.
//
// fmtClock is deliberately exercised only for its NON-locale paths (the null
// and Invalid-Date guards): the formatted output is Intl- and timezone-
// dependent, so pinning a literal clock string would fail on a differently-
// zoned runner. The guard is what a regression would silently remove — it was
// added because Intl throws RangeError on an Invalid Date, mid-transcript-render.

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  fmtBytes,
  fmtDur,
  fmtMs,
  fmtClock,
  fmtMmSs,
  truncMid,
  fmtSessionLabel,
} from "./formatters.js";

// ---- fmtBytes ------------------------------------------------------------

test("fmtBytes renders bytes, KB and MB with their own precisions", () => {
  assert.equal(fmtBytes(0), "0 B");
  assert.equal(fmtBytes(1), "1 B");
  assert.equal(fmtBytes(1023), "1023 B");
  assert.equal(fmtBytes(1024), "1.0 KB");
  assert.equal(fmtBytes(1536), "1.5 KB");
  assert.equal(fmtBytes(1048576), "1.00 MB");
  assert.equal(fmtBytes(5 * 1048576), "5.00 MB");
});

test("fmtBytes carries a value that ROUNDS UP into the next unit", () => {
  // 1048570 B is below 1 MB, but rounds to 1024.0 KB — an impossible reading
  // (1024 KB IS a megabyte). A live tap's byte counter passes through this
  // window on every recording.
  assert.equal(fmtBytes(1048570), "1.00 MB");
  assert.equal(fmtBytes(1048575), "1.00 MB");
  // Just below the window, the KB form is still the right one.
  assert.equal(fmtBytes(1048000), "1023.4 KB");
});

test("fmtBytes treats null/undefined/non-finite as zero", () => {
  assert.equal(fmtBytes(null), "0 B");
  assert.equal(fmtBytes(undefined), "0 B");
  assert.equal(fmtBytes(NaN), "0 B");
  assert.equal(fmtBytes(Infinity), "0 B");
});

// ---- fmtDur --------------------------------------------------------------

test("fmtDur renders seconds below a minute and m/s above it", () => {
  assert.equal(fmtDur(0), "0.00 s");
  assert.equal(fmtDur(0.5), "0.50 s");
  assert.equal(fmtDur(59), "59.00 s");
  assert.equal(fmtDur(59.99), "59.99 s");
  assert.equal(fmtDur(60), "1m 0.0s");
  assert.equal(fmtDur(90), "1m 30.0s");
  assert.equal(fmtDur(3600), "60m 0.0s");
});

test("fmtDur carries a seconds part that ROUNDS UP to a full minute", () => {
  assert.equal(fmtDur(59.999), "1m 0.0s"); // was "60.00 s"
  assert.equal(fmtDur(119.96), "2m 0.0s"); // was "1m 60.0s"
  assert.equal(fmtDur(179.97), "3m 0.0s"); // was "2m 60.0s"
  // Just below the rounding window the minute must NOT advance.
  assert.equal(fmtDur(119.9), "1m 59.9s");
});

test("fmtDur treats null/undefined/non-finite as unknown", () => {
  assert.equal(fmtDur(null), "?");
  assert.equal(fmtDur(undefined), "?");
  assert.equal(fmtDur(NaN), "?");
  assert.equal(fmtDur(Infinity), "?"); // was "Infinitym NaNs"
  assert.equal(fmtDur(-Infinity), "?");
});

// ---- fmtMs ---------------------------------------------------------------

test("fmtMs renders milliseconds below a second and seconds above it", () => {
  assert.equal(fmtMs(0), "0 ms");
  assert.equal(fmtMs(999), "999 ms");
  assert.equal(fmtMs(1000), "1.0 s");
  assert.equal(fmtMs(1500), "1.5 s");
  assert.equal(fmtMs(90000), "90.0 s");
});

test("fmtMs treats null/undefined/non-finite as unknown", () => {
  assert.equal(fmtMs(null), "?");
  assert.equal(fmtMs(undefined), "?");
  assert.equal(fmtMs(NaN), "?");
  assert.equal(fmtMs(Infinity), "?");
});

// ---- fmtClock ------------------------------------------------------------

test("fmtClock returns the ? placeholder rather than throwing on bad input", () => {
  assert.equal(fmtClock(null), "?");
  assert.equal(fmtClock(undefined), "?");
  assert.equal(fmtClock(""), "?");
  // An unparseable stamp makes an Invalid Date, which Intl's format() throws
  // RangeError on. fmtClock runs once per merged-transcript segment, so a single
  // corrupt sidecar value must garble ONE cell, never abort the whole render.
  assert.equal(fmtClock("not-a-timestamp"), "?");
  assert.equal(fmtClock("2026-13-45T99:99:99Z"), "?");
});

test("fmtClock renders a parseable instant as hh:mm:ss", () => {
  // Zone-dependent (viewer's timezone, by design), so pin the SHAPE, not the
  // digits — the guard cases above are what carry the literal expectations.
  assert.match(fmtClock("2026-05-12T09:19:55Z"), /^\d{2}:\d{2}:55$/);
});

// ---- fmtMmSs -------------------------------------------------------------

test("fmtMmSs renders floored m:ss with a zero-padded seconds field", () => {
  assert.equal(fmtMmSs(0), "0:00");
  assert.equal(fmtMmSs(5), "0:05");
  assert.equal(fmtMmSs(59.9), "0:59");
  assert.equal(fmtMmSs(60), "1:00");
  assert.equal(fmtMmSs(90), "1:30");
  assert.equal(fmtMmSs(3661), "61:01");
});

test("fmtMmSs clamps negative and non-finite input to zero", () => {
  assert.equal(fmtMmSs(-1), "0:00");
  assert.equal(fmtMmSs(Infinity), "0:00");
  assert.equal(fmtMmSs(NaN), "0:00");
});

// ---- truncMid ------------------------------------------------------------

test("truncMid keeps both ends and never exceeds max", () => {
  const name = "averylongfilename.wav"; // 21 chars
  assert.equal(truncMid(name, name.length), name); // exactly max — untouched
  assert.equal(truncMid(name, 100), name);
  assert.equal(truncMid(name, 11), "avery…e.wav");
  assert.equal(truncMid(name, 3), "a…v");
  // max 2 makes the tail slice `s.slice(-0)` — the WHOLE string — so this used
  // to return "a…averylongfilename.wav": LONGER than the input it truncated.
  assert.equal(truncMid(name, 2), "a…");
  assert.equal(truncMid(name, 1), "…");
});

test("truncMid returns the empty string for empty input", () => {
  assert.equal(truncMid("", 10), "");
  assert.equal(truncMid(null, 10), "");
  assert.equal(truncMid(undefined, 10), "");
});

// ---- fmtSessionLabel -----------------------------------------------------

test("fmtSessionLabel renders a session id as a short date + time", () => {
  assert.equal(fmtSessionLabel("2026-05-12T09-19-55Z"), "05-12 09:19");
});

test("fmtSessionLabel passes through anything too short to be a session id", () => {
  assert.equal(fmtSessionLabel("short"), "short");
  assert.equal(fmtSessionLabel(""), "");
  assert.equal(fmtSessionLabel(null), "");
  assert.equal(fmtSessionLabel(undefined), "");
});
