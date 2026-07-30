// Unit tests for the client-side subtitle exporters (#208), run via
// `node --test`, no DOM. toSRT/toVTT are pure: an array of timed segments
// -> a SubRip / WebVTT document string. Wiring a download button is a
// separate follow-on; this pins the format only.
import { test } from "node:test";
import assert from "node:assert/strict";

import { toSRT, toVTT } from "./subtitles.js";

/** @typedef {{start:number,end:number,text:string,speaker?:string}} Seg */

/** @type {Seg[]} */
const two = [
  { start: 0, end: 2.5, speaker: "Alice", text: "Hello there" },
  { start: 2.5, end: 5, speaker: "Bob", text: "General Kenobi" },
];

test("toSRT emits numbered cues with comma-ms timestamps and 'Speaker: text' lines", () => {
  assert.equal(
    toSRT(two),
    "1\n00:00:00,000 --> 00:00:02,500\nAlice: Hello there\n\n" +
      "2\n00:00:02,500 --> 00:00:05,000\nBob: General Kenobi",
  );
});

test("toVTT emits a WEBVTT header then dot-ms cues", () => {
  assert.equal(
    toVTT(two),
    "WEBVTT\n\n" +
      "00:00:00.000 --> 00:00:02.500\nAlice: Hello there\n\n" +
      "00:00:02.500 --> 00:00:05.000\nBob: General Kenobi",
  );
});

test("timestamps roll over into hours, zero-padded", () => {
  const seg = [{ start: 3661.5, end: 3662, text: "late" }];
  assert.equal(toSRT(seg), "1\n01:01:01,500 --> 01:01:02,000\nlate");
  assert.equal(toVTT(seg), "WEBVTT\n\n01:01:01.500 --> 01:01:02.000\nlate");
});

test("a segment without a speaker has no prefix", () => {
  assert.equal(toSRT([{ start: 0, end: 1, text: "Hi" }]), "1\n00:00:00,000 --> 00:00:01,000\nHi");
});

test("empty input: SRT is empty, VTT is just the header line", () => {
  assert.equal(toSRT([]), "");
  assert.equal(toVTT([]), "WEBVTT\n");
});

test("a cue payload escapes &, < and > in both the speaker and the text", () => {
  // `<anon>` is the merge layer's default speaker key and an operator alias is
  // free text: unescaped, a WebVTT parser reads `<lead>` as an unknown cue tag
  // and DROPS it, taking the speaker attribution with it. Escaping `>` also
  // means a literal `-->` in transcript text can never read as a timing line.
  const seg = [{ start: 0, end: 1, speaker: "R&D <lead>", text: "a --> b & <c>" }];
  assert.equal(
    toSRT(seg),
    "1\n00:00:00,000 --> 00:00:01,000\nR&amp;D &lt;lead&gt;: a --&gt; b &amp; &lt;c&gt;",
  );
  assert.equal(
    toVTT(seg),
    "WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nR&amp;D &lt;lead&gt;: a --&gt; b &amp; &lt;c&gt;",
  );
});

test("millisecond rounding carries into the seconds field (no 4-digit ms)", () => {
  // 0.9999s = 999.9ms -> 1000ms must carry: 00:00:01,000, not 00:00:00,1000
  assert.equal(toSRT([{ start: 0.9999, end: 1, text: "x" }]), "1\n00:00:01,000 --> 00:00:01,000\nx");
  assert.equal(toVTT([{ start: 0.9999, end: 1, text: "x" }]), "WEBVTT\n\n00:00:01.000 --> 00:00:01.000\nx");
});
