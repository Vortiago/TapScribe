// Tests for the test harness itself.
//
// Only ONE property is worth pinning here, and it's the one that was
// silently missing: the virtual clock and the chrome.storage.onChanged
// fan-out used to `catch (e) { /* surface in test if needed */ }` with
// nothing anywhere collecting or re-throwing. That covered the bridge's
// least-asserted paths — scheduleReconnect's timer body, restartDrainTimer,
// and the whole endMeeting → publishStatus → updateIndicator chain — so a
// regression that THREW there vanished and the suite stayed green.
//
// If this file goes red, don't "fix" it by re-swallowing.

const test = require("node:test");
const assert = require("node:assert/strict");

const { createBridge } = require("./harness");

test("a throw from a timer body fails the test instead of vanishing", async () => {
  const b = createBridge();
  await b.flushMicrotasks();

  b.clock.setTimeout(() => {
    throw new Error("regression in a timer body");
  }, 5);

  assert.throws(
    () => b.clock.tick(10),
    /uncaught throw inside a scheduled timer body: regression in a timer body/,
  );
});

test("every due timer still fires before the collected throw is surfaced", async () => {
  // Ordering matters: surfacing the failure mid-pass would leave later
  // timers unfired and silently change what the test under diagnosis ran.
  const b = createBridge();
  await b.flushMicrotasks();

  const fired = [];
  b.clock.setTimeout(() => { fired.push("first"); throw new Error("boom"); }, 1);
  b.clock.setTimeout(() => { fired.push("second"); }, 2);

  assert.throws(() => b.clock.tick(10), /boom/);
  assert.deepEqual(fired, ["first", "second"], "the later timer still ran");
});

test("a throw from a storage onChanged listener fails the test", async () => {
  // Driven through the public surface: content.js's own onChanged listener
  // runs here, and a stub listener registered alongside it stands in for a
  // regression inside that chain.
  const b = createBridge();
  await b.flushMicrotasks();
  b.addStorageChangeListener(() => {
    throw new Error("regression in the onChanged chain");
  });

  assert.throws(
    () => b.flipUseTls(true),
    /uncaught throw inside a chrome.storage.onChanged listener: regression in the onChanged chain/,
  );
});
