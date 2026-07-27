// @ts-check
// Unit tests for seek-target resolution (next/seek-target.js).
// DOM-free: turning a merged segment into "which file, which source, how many
// seconds in" is pure arithmetic over the WAV listing.

import test from "node:test";
import assert from "node:assert/strict";

import { resolveSeekTarget } from "./seek-target.js";

/** @returns {any[]} a listing with one original and no stripped clips */
const listing = () => [
  {
    name: "2026-07-26T09-30-15Z_alice_aaaa1111_deadbeef.wav",
    wav_start: "2026-07-26T09:30:15+00:00",
    wav_end: "2026-07-26T09:31:15+00:00",
    duration_s: 60,
    regions: [],
  },
];

/** The same listing after a strip run: one clip cut 20 s into the original. */
const listingWithClip = () => {
  const files = listing();
  files[0].regions = [
    {
      name: "2026-07-26T09-30-35Z_alice_aaaa1111_c0ffee11.wav",
      wav_start: "2026-07-26T09:30:35+00:00",
      wav_end: "2026-07-26T09:30:45+00:00",
      duration_s: 10,
    },
  ];
  return files;
};

test("an original resolves to its own timeline, offset from the file's start", () => {
  const got = resolveSeekTarget(
    "2026-07-26T09-30-15Z_alice_aaaa1111_deadbeef.wav",
    "2026-07-26T09:30:27+00:00",
    listing(),
  );

  assert.deepEqual(got, {
    name: "2026-07-26T09-30-15Z_alice_aaaa1111_deadbeef.wav",
    source: "original",
    offsetS: 12,
  });
});

test("a source_wav that isn't in the listing resolves to null", () => {
  // The click-time answer for a deleted WAV, or a stripped-source transcript
  // after `clear stripped`. Pre-disabling these lines would put `files_sig`
  // into the merged pane's render signature — see ADR-0017.
  assert.equal(resolveSeekTarget("gone.wav", "2026-07-26T09:30:27+00:00", listing()), null);
  assert.equal(resolveSeekTarget("gone.wav", "2026-07-26T09:30:27+00:00", []), null);
});

test("an ORPHANED clip resolves by name, under whichever original displays it", () => {
  // `build_session_files` shows a clip whose owner was deleted under a
  // same-participant sibling. Resolution is by name, so the display fallback
  // can't misdirect the seek the way owner arithmetic would.
  const files = listingWithClip();
  files[0].name = "2026-07-26T09-40-00Z_alice_aaaa1111_notowner.wav";
  files[0].wav_start = "2026-07-26T09:40:00+00:00";

  const got = resolveSeekTarget(
    "2026-07-26T09-30-35Z_alice_aaaa1111_c0ffee11.wav",
    "2026-07-26T09:30:40+00:00",
    files,
  );

  assert.deepEqual(got?.source, "stripped");
  assert.equal(got?.offsetS, 5, "offset still measured from the clip itself");
});

test("an unreadable timestamp degrades to the file's start, not to unplayable", () => {
  // A WAV whose name won't parse has wav_start null. Merge selection skips such
  // files so this shouldn't reach us, but "play from the top" is a useful
  // answer and refusing to play at all is not.
  const files = listing();
  files[0].wav_start = null;

  const got = resolveSeekTarget(files[0].name, "2026-07-26T09:30:27+00:00", files);

  assert.equal(got?.offsetS, 0);
});

test("a segment stamped before its file's start clamps to zero", () => {
  const got = resolveSeekTarget(listing()[0].name, "2026-07-26T09:30:00+00:00", listing());

  assert.equal(got?.offsetS, 0, "clock noise is not a negative position");
});

test("a stripped clip resolves against the CLIP, not its owner original", () => {
  // The words came out of the clip, so the clip is what plays and the offset is
  // measured from the clip's own start (5 s into a clip that begins 20 s into
  // the original) — NOT 25 s into the original.
  const got = resolveSeekTarget(
    "2026-07-26T09-30-35Z_alice_aaaa1111_c0ffee11.wav",
    "2026-07-26T09:30:40+00:00",
    listingWithClip(),
  );

  assert.deepEqual(got, {
    name: "2026-07-26T09-30-35Z_alice_aaaa1111_c0ffee11.wav",
    source: "stripped",
    offsetS: 5,
  });
});
