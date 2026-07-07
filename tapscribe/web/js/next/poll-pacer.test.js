// Unit tests for the adaptive poll pacer (run via `node --test`).
//
// The pacer decides the delay before the next /api/state poll (issue #247's
// "cheap adaptive win"): stay at 500ms while anything is changing or a job/tap
// is active, back off to 2s once the dashboard has been idle-and-unchanged for
// a few ticks, and snap straight back to 500ms on the first change or operator
// interaction. It's a pure, timer-free state machine so the cadence rule can be
// pinned without wall-clock — same no-deps `node:test` shape as api.test.js.

import { describe, it } from "node:test";
import assert from "node:assert/strict";

import { createPollPacer, FAST_MS, SLOW_MS, IDLE_STREAK } from "./poll-pacer.js";

// A poll that returned real (non-304) state — something changed.
const CHANGED = { changed: true, active: false };
// An idle 304 poll with no job/tap running — the only case that may back off.
const IDLE = { changed: false, active: false };
// An idle 304 poll but a job/tap is in-flight — must stay fast.
const BUSY = { changed: false, active: true };

describe("createPollPacer", () => {
  it("defaults to a slower idle cadence reached only after a multi-tick streak", () => {
    // The invariants the backoff relies on (not the arbitrary literals): the
    // idle interval must be strictly slower than the active one, and a single
    // stale poll must not trip the backoff — it takes a streak, so one 304
    // between changes doesn't make the dashboard flap to the slow cadence.
    assert.ok(SLOW_MS > FAST_MS, "idle cadence must be slower than the fast poll");
    assert.ok(IDLE_STREAK >= 2, "backoff must require more than a single idle tick");
  });

  it("stays fast for the first few idle ticks, then backs off", () => {
    const pacer = createPollPacer();
    // The streak has to build up: the first IDLE_STREAK-1 idle ticks stay fast.
    for (let i = 1; i < IDLE_STREAK; i++) {
      assert.equal(pacer.record(IDLE), FAST_MS, `idle tick #${i} should still be fast`);
    }
    // The IDLE_STREAK-th consecutive idle-unchanged tick trips the backoff...
    assert.equal(pacer.record(IDLE), SLOW_MS, "backs off on the IDLE_STREAK-th idle tick");
    // ...and stays backed off while idle continues.
    assert.equal(pacer.record(IDLE), SLOW_MS, "stays slow while idle persists");
  });

  it("snaps back to fast on the first changed poll, resetting the streak", () => {
    const pacer = createPollPacer();
    for (let i = 0; i < IDLE_STREAK + 2; i++) pacer.record(IDLE); // deep into backoff
    assert.equal(pacer.record(CHANGED), FAST_MS, "a real change snaps back to fast");
    // Streak was reset, so it takes a full fresh run of idle ticks to back off again.
    for (let i = 1; i < IDLE_STREAK; i++) {
      assert.equal(pacer.record(IDLE), FAST_MS, `post-change idle tick #${i} is fast again`);
    }
    assert.equal(pacer.record(IDLE), SLOW_MS, "backs off again after a fresh idle streak");
  });

  it("never backs off while a job or tap is active, even on unchanged polls", () => {
    const pacer = createPollPacer();
    for (let i = 0; i < IDLE_STREAK * 3; i++) {
      assert.equal(pacer.record(BUSY), FAST_MS, `active tick #${i} must stay fast`);
    }
  });

  it("wake() resets the streak so the next poll is fast", () => {
    const pacer = createPollPacer();
    for (let i = 0; i < IDLE_STREAK + 1; i++) pacer.record(IDLE); // now slow
    assert.equal(pacer.record(IDLE), SLOW_MS, "sanity: backed off before wake");
    pacer.wake();
    assert.equal(pacer.record(IDLE), FAST_MS, "wake() snaps the next poll back to fast");
    for (let i = 1; i < IDLE_STREAK; i++) pacer.record(IDLE);
    assert.equal(pacer.record(IDLE), SLOW_MS, "and a fresh idle streak still backs off");
  });

  it("honours overridden fast/slow/streak options", () => {
    const pacer = createPollPacer({ fast: 250, slow: 5000, streak: 2 });
    assert.equal(pacer.record(IDLE), 250, "first idle tick uses overridden fast");
    assert.equal(pacer.record(IDLE), 5000, "backs off after the overridden streak of 2");
    assert.equal(pacer.record(CHANGED), 250, "change snaps back to overridden fast");
  });
});
